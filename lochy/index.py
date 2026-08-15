import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

from .bundle import Bundle, BundleSession, unpack_bundle
from .store import Store

BUNDLES_PREFIX = "bundles/"
INDEX_PREFIX = "index/"

BRANCH = "branch"

# Percent-encoding rather than Claude's [^a-zA-Z0-9] -> - scheme: that one is
# lossy and would file feature/foo and feature-foo under the same segment.
# A value can't contain a control character, so this can't collide with one.
UNSET_SEGMENT = "%00"


@dataclass(frozen=True)
class IndexEntry:
    ref: str
    dimension: str
    value: str | None
    agent: str
    created_at: str
    bytes: int
    sessions: int
    cwd: str
    claude_version: str | None


@dataclass(frozen=True)
class ReindexResult:
    bundles: int
    entries: int
    removed: int


def bundle_key(ref: str) -> str:
    return f"{BUNDLES_PREFIX}{ref}.loch"


def ref_from_key(key: str) -> str:
    return key[len(BUNDLES_PREFIX) : -len(".loch")]


def encode_segment(value: str | None) -> str:
    return UNSET_SEGMENT if value is None else quote(value, safe="")


def decode_segment(segment: str) -> str | None:
    return None if segment == UNSET_SEGMENT else unquote(segment)


def dimension_prefix(dimension: str) -> str:
    return f"{INDEX_PREFIX}{dimension}/"


def value_prefix(dimension: str, value: str | None) -> str:
    return f"{dimension_prefix(dimension)}{encode_segment(value)}/"


def index_key(dimension: str, value: str | None, ref: str) -> str:
    return f"{value_prefix(dimension, value)}{ref}"


def _entry_to_json(entry: IndexEntry) -> dict[str, Any]:
    return {
        "ref": entry.ref,
        "dimension": entry.dimension,
        "value": entry.value,
        "agent": entry.agent,
        "createdAt": entry.created_at,
        "bytes": entry.bytes,
        "sessions": entry.sessions,
        "cwd": entry.cwd,
        "claudeVersion": entry.claude_version,
    }


def _entry_from_json(raw: dict[str, Any]) -> IndexEntry:
    return IndexEntry(
        ref=raw["ref"],
        dimension=raw["dimension"],
        value=raw.get("value"),
        agent=raw.get("agent", ""),
        created_at=raw.get("createdAt", ""),
        bytes=raw.get("bytes", 0),
        sessions=raw.get("sessions", 0),
        cwd=raw.get("cwd", ""),
        claude_version=raw.get("claudeVersion"),
    )


def _first_version(sessions: list[BundleSession]) -> str | None:
    for session in sessions:
        if session.claude_version:
            return session.claude_version
    return None


def entries_for(bundle: Bundle, ref: str, size: int) -> list[IndexEntry]:
    """One entry per (branch, ref): a save without --branch can pack sessions
    from several branches, so a bundle may be indexed under each of them."""
    grouped: dict[str | None, list[BundleSession]] = {}
    for session in bundle.sessions:
        grouped.setdefault(session.git_branch, []).append(session)

    return [
        IndexEntry(
            ref=ref,
            dimension=BRANCH,
            value=branch,
            agent=bundle.agent,
            created_at=bundle.created_at,
            bytes=size,
            sessions=len(sessions),
            cwd=sessions[0].cwd,
            claude_version=_first_version(sessions),
        )
        for branch, sessions in sorted(
            grouped.items(), key=lambda item: encode_segment(item[0])
        )
    ]


def write_entries(store: Store, entries: list[IndexEntry]) -> list[str]:
    keys = []
    for entry in entries:
        key = index_key(entry.dimension, entry.value, entry.ref)
        store.put(key, json.dumps(_entry_to_json(entry)).encode("utf-8"))
        keys.append(key)
    return keys


def index_bundle(store: Store, bundle: Bundle, ref: str, size: int) -> list[IndexEntry]:
    entries = entries_for(bundle, ref, size)
    write_entries(store, entries)
    return entries


def load_entries(store: Store, prefix: str) -> list[IndexEntry]:
    entries = [
        _entry_from_json(json.loads(store.get(key).decode("utf-8")))
        for key in store.list(prefix)
    ]
    return sorted(entries, key=lambda entry: entry.created_at, reverse=True)


def delete_bundle(store: Store, ref: str) -> list[str]:
    """Entries first: they are derived from the bundle, so removing it before
    them would strand any that a failure here left behind."""
    packed = store.get(bundle_key(ref))
    bundle = unpack_bundle(packed)

    removed = []
    for entry in entries_for(bundle, ref, len(packed)):
        key = index_key(entry.dimension, entry.value, entry.ref)
        store.delete(key)
        removed.append(key)

    store.delete(bundle_key(ref))
    removed.append(bundle_key(ref))
    return removed


def reindex(store: Store) -> ReindexResult:
    stale = set(store.list(INDEX_PREFIX))
    bundles = 0
    written: set[str] = set()

    for key in store.list(BUNDLES_PREFIX):
        if not key.endswith(".loch"):
            continue
        packed = store.get(key)
        bundle = unpack_bundle(packed)
        entries = entries_for(bundle, ref_from_key(key), len(packed))
        written.update(write_entries(store, entries))
        bundles += 1

    for key in sorted(stale - written):
        store.delete(key)

    return ReindexResult(
        bundles=bundles, entries=len(written), removed=len(stale - written)
    )
