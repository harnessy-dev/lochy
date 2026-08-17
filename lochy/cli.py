import argparse
import importlib.metadata
import os
import platform
import re
import socket
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NoReturn

from .bundle import (
    BUNDLE_VERSION,
    Bundle,
    BundleOrigin,
    BundleSession,
    bundle_ref,
    pack_bundle,
    unpack_bundle,
)
from .claude import (
    AGENT,
    SessionMeta,
    home_dir,
    iso_timestamp,
    list_sessions,
    resolve_cwd,
    transcript_path_for,
)
from .git import current_branch
from .index import (
    BRANCH,
    IndexEntry,
    bundle_key,
    delete_bundle,
    dimension_prefix,
    index_bundle,
    index_key,
    load_entries,
    reindex,
    value_prefix,
)
from .output import JSON_FLAG, CommandError, Result, emit, emit_error
from .redact import RedactionError, redact, summarize
from .rewrite import (
    ForeignCwd,
    RewriteSpec,
    foreign_cwds,
    residual_origin_paths,
    rewrite_transcript,
)
from .store import MissingDependency, MissingObject, create_store, resolve_store_uri

USAGE = """lochy — save and restore agent coding sessions across machines

Usage:
  lochy list    [--cwd <path>] [--branch <name>]
  lochy list    --remote [--branch <name>] [--all] [--store <uri>]
  lochy save    [--cwd <path>] [--branch <name>] [--session <id>] [--store <uri>]
  lochy restore <ref> [--into <path>] [--store <uri>] [--force] [--new-id]
  lochy delete  <ref> [--store <uri>]
  lochy reindex [--store <uri>]
  lochy --version

Every command takes --json, which replaces the text below with one machine-
readable document on stdout.

Stores:
  <path>              local directory
  s3://bucket/prefix  any S3-compatible endpoint

Environment:
  LOCHY_STORE        default store URI
  LOCHY_S3_ENDPOINT  custom endpoint (R2, MinIO, ...)
  LOCHY_S3_REGION    region override
"""


def fail(code: str, message: str) -> NoReturn:
    raise CommandError(code, message)


@contextmanager
def store_errors(description: str) -> Iterator[None]:
    """The three failures a caller can actually act on: an object that isn't
    there is terminal, a store that didn't answer is worth retrying, and a
    backend whose SDK is absent is fixed by an install and by nothing else.
    Keep the block around the store call alone — anything wider mislabels a bug
    in this process as a failure of the store."""
    try:
        yield
    except MissingObject as error:
        fail("bundle-not-found", f"{description}: {error}")
    except MissingDependency as error:
        fail("s3-extra-missing", f"{description}: {error}")
    except Exception as error:
        fail("store-unreachable", f"{description}: {error}")


def format_bytes(count: int) -> str:
    if count < 1024:
        return f"{count}B"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f}K"
    return f"{count / (1024 * 1024):.1f}M"


def describe(meta: SessionMeta) -> str:
    branch = f"[{meta.git_branch}]" if meta.git_branch else "[no branch]"
    # Hoisted out of the f-string: a backslash inside an interpolation is
    # PEP 701 syntax and won't parse below 3.12.
    collapsed = re.sub(r"\s+", " ", meta.summary) if meta.summary else ""
    summary = f" {collapsed}" if collapsed else ""
    when = meta.modified_at[:16].replace("T", " ")
    return (
        f"{meta.session_id}  {when}  {format_bytes(meta.bytes):>5}  {branch}{summary}"
    )


def describe_entry(entry: IndexEntry) -> str:
    branch = f"[{entry.value}]" if entry.value else "[no branch]"
    when = entry.created_at[:16].replace("T", " ")
    version = f"  claude {entry.claude_version}" if entry.claude_version else ""
    return (
        f"{when}  {format_bytes(entry.bytes):>5}  {entry.sessions} session(s)  "
        f"{branch}{version}  {entry.cwd}"
    )


def collect(
    cwd: str | None, branch: str | None, session: str | None = None
) -> tuple[str, list[SessionMeta]]:
    resolved = resolve_cwd(cwd if cwd is not None else os.getcwd())
    return resolved, list_sessions(cwd=resolved, branch=branch, session_id=session)


class HelpRequested(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class _Parser(argparse.ArgumentParser):
    # argparse answers -h and a bad flag itself, writing prose and exiting
    # before a command ever runs. Both are routed back to main() so that
    # "one document per invocation" holds in JSON mode; the text each carries
    # is argparse's own, so nothing changes without --json.
    def error(self, message: str) -> NoReturn:
        raise CommandError(
            "usage",
            message,
            prose=f"{self.format_usage()}{self.prog}: error: {message}\n",
            exit_status=2,
        )

    def print_help(self, file: Any = None) -> NoReturn:
        raise HelpRequested(self.format_help())


def _parser(command: str) -> argparse.ArgumentParser:
    parser = _Parser(prog=f"lochy {command}")
    # Registered so the flag parses after the subcommand; main() reads the mode
    # off argv itself, because a failure before or during parsing has to render
    # in the mode the caller asked for.
    parser.add_argument(JSON_FLAG, action="store_true")
    return parser


def _session_payload(meta: SessionMeta) -> dict[str, Any]:
    return {
        "sessionId": meta.session_id,
        "path": meta.path,
        "cwd": meta.cwd,
        "otherCwds": list(meta.other_cwds),
        "branch": meta.git_branch,
        "claudeVersion": meta.claude_version,
        "bytes": meta.bytes,
        "modifiedAt": meta.modified_at,
        "summary": meta.summary,
    }


def _entry_payload(entry: IndexEntry) -> dict[str, Any]:
    """Deliberately not index.py's stored shape: that one is a storage format,
    this one is a versioned wire contract, and they change for different
    reasons."""
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


def command_list(argv: list[str]) -> Result:
    parser = _parser("list")
    parser.add_argument("--cwd")
    parser.add_argument("--branch")
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--all", action="store_true", dest="every")
    parser.add_argument("--store")
    args = parser.parse_args(argv)

    if args.remote or args.every or args.store is not None:
        return list_remote(args)

    cwd, sessions = collect(args.cwd, args.branch)
    return Result(
        command="list",
        payload={
            "remote": False,
            "cwd": cwd,
            "branch": args.branch,
            "sessions": [_session_payload(meta) for meta in sessions],
        },
        text=_list_text(cwd, sessions),
    )


def _list_text(cwd: str, sessions: list[SessionMeta]) -> str:
    if not sessions:
        return f"no {AGENT} sessions found for {cwd}\n"
    lines = [f"{len(sessions)} session(s) for {cwd}\n"]
    lines += [f"  {describe(meta)}\n" for meta in sessions]
    return "".join(lines)


def list_remote(args: argparse.Namespace) -> Result:
    store = create_store(resolve_store_uri(args.store))
    cwd = resolve_cwd(args.cwd if args.cwd is not None else os.getcwd())
    branch = args.branch if args.branch is not None else current_branch(cwd)

    prefix = dimension_prefix(BRANCH) if args.every else value_prefix(BRANCH, branch)
    with store_errors(f"could not list {store.describe()}"):
        entries = load_entries(store, prefix)
    refs = {entry.ref for entry in entries}

    return Result(
        command="list",
        payload={
            "remote": True,
            "store": store.describe(),
            "all": bool(args.every),
            "branch": None if args.every else branch,
            "bundles": len(refs),
            "entries": [_entry_payload(entry) for entry in entries],
        },
        text=_list_remote_text(store.describe(), args.every, branch, entries, refs),
    )


def _list_remote_text(
    store: str,
    every: bool,
    branch: str | None,
    entries: list[IndexEntry],
    refs: set[str],
) -> str:
    scope = "" if every else f" for [{branch or 'no branch'}]"
    if not entries:
        return f"no bundles{scope} in {store}\n"
    lines = [f"{len(refs)} bundle(s){scope} in {store}\n"]
    lines += [f"  {entry.ref}\n    {describe_entry(entry)}\n" for entry in entries]
    return "".join(lines)


def command_save(argv: list[str]) -> Result:
    parser = _parser("save")
    parser.add_argument("--cwd")
    parser.add_argument("--branch")
    parser.add_argument("--session")
    parser.add_argument("--store")
    args = parser.parse_args(argv)

    cwd, sessions = collect(args.cwd, args.branch, args.session)
    if not sessions:
        fail("no-sessions", f"no {AGENT} sessions found for {cwd}")

    packed_sessions: list[BundleSession] = []
    redactions: list[dict[str, int]] = []
    for meta in sessions:
        # In memory only — the agent's own transcript on disk stays verbatim.
        try:
            scrubbed = redact(Path(meta.path).read_text(encoding="utf-8"))
        except RedactionError as error:
            fail("redaction-failed", f"refusing to pack {meta.session_id}: {error}")
        packed_sessions.append(
            BundleSession(
                session_id=meta.session_id,
                cwd=meta.cwd,
                git_branch=meta.git_branch,
                claude_version=meta.claude_version,
                modified_at=meta.modified_at,
                transcript=scrubbed.text,
            )
        )
        redactions.append(scrubbed.counts)

    bundle = Bundle(
        version=BUNDLE_VERSION,
        agent=AGENT,
        created_at=iso_timestamp(time.time()),
        origin=BundleOrigin(
            home=home_dir(), platform=_platform_name(), hostname=socket.gethostname()
        ),
        sessions=packed_sessions,
    )

    packed = pack_bundle(bundle)
    ref = bundle_ref(packed)
    store = create_store(resolve_store_uri(args.store))
    with store_errors(f"could not write to {store.describe()}"):
        # Bundle first: an index entry is derived from it, so a pointer to a
        # bundle that isn't there yet is the worse half of a failed save.
        store.put(bundle_key(ref), packed)
        entries = index_bundle(store, bundle, ref, len(packed))

    return Result(
        command="save",
        payload={
            "ref": ref,
            "bytes": len(packed),
            "store": store.describe(),
            "cwd": cwd,
            "createdAt": bundle.created_at,
            # Counts by rule name only. The matched text never leaves this
            # process: save's stdout lands in the next agent's transcript.
            "redacted": sum(sum(counts.values()) for counts in redactions),
            "sessions": [
                {
                    "sessionId": session.session_id,
                    "branch": session.git_branch,
                    "cwd": session.cwd,
                    "claudeVersion": session.claude_version,
                    "modifiedAt": session.modified_at,
                    "redactions": counts,
                }
                for session, counts in zip(bundle.sessions, redactions)
            ],
            "indexed": [
                {
                    "dimension": entry.dimension,
                    "value": entry.value,
                    "key": index_key(entry.dimension, entry.value, entry.ref),
                }
                for entry in entries
            ],
        },
        text=_save_text(
            bundle, redactions, entries, store.describe(), len(packed), ref
        ),
    )


def _save_text(
    bundle: Bundle,
    redactions: list[dict[str, int]],
    entries: list[IndexEntry],
    store: str,
    size: int,
    ref: str,
) -> str:
    lines = []
    for session, counts in zip(bundle.sessions, redactions):
        note = f" — {summarize(counts)}" if counts else ""
        lines.append(
            f"  packed {session.session_id} [{session.git_branch or 'no branch'}]{note}\n"
        )
    indexed = ", ".join(f"[{entry.value or 'no branch'}]" for entry in entries)
    lines.append(f"\nstored {format_bytes(size)} in {store}\n")
    lines.append(f"indexed under {indexed}\n")
    lines.append(f"ref {ref}\n")
    return "".join(lines)


@dataclass(frozen=True)
class RestoredSession:
    session_id: str
    origin_session_id: str
    branch: str | None
    status: str
    path: str
    residual_origin_paths: tuple[str, ...]
    resume_command: str | None
    foreign_cwds: dict[str, ForeignCwd] = field(default_factory=dict)


def command_restore(argv: list[str]) -> Result:
    parser = _parser("restore")
    parser.add_argument("ref", nargs="?")
    parser.add_argument("--into")
    parser.add_argument("--store")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--new-id", action="store_true", dest="new_id")
    args = parser.parse_args(argv)

    if not args.ref:
        fail("missing-ref", "restore requires a bundle ref")

    store = create_store(resolve_store_uri(args.store))
    described = f"could not read {args.ref} from {store.describe()}"
    with store_errors(described):
        packed = store.get(bundle_key(args.ref))
    try:
        bundle = unpack_bundle(packed)
    except Exception as error:
        # Fetched fine and still unusable: neither absent nor unreachable.
        fail("bundle-unreadable", f"{described}: {error}")

    target_cwd = resolve_cwd(args.into if args.into is not None else os.getcwd())
    target_home = home_dir()
    restored: list[RestoredSession] = []

    for session in bundle.sessions:
        target_session_id = str(uuid.uuid4()) if args.new_id else session.session_id
        spec = RewriteSpec(
            origin_cwd=session.cwd,
            origin_home=bundle.origin.home,
            target_cwd=target_cwd,
            target_home=target_home,
            origin_session_id=session.session_id,
            target_session_id=target_session_id,
        )

        transcript = rewrite_transcript(session.transcript, spec)
        destination = Path(transcript_path_for(target_cwd, target_session_id))

        if destination.exists() and not args.force:
            restored.append(
                RestoredSession(
                    session_id=target_session_id,
                    origin_session_id=session.session_id,
                    branch=session.git_branch,
                    status="skipped",
                    path=str(destination),
                    residual_origin_paths=(),
                    resume_command=None,
                )
            )
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(transcript, encoding="utf-8")

        restored.append(
            RestoredSession(
                session_id=target_session_id,
                origin_session_id=session.session_id,
                branch=session.git_branch,
                status="restored",
                path=str(destination),
                residual_origin_paths=tuple(residual_origin_paths(transcript, spec)),
                resume_command=(
                    f"cd {target_cwd} && claude --resume {target_session_id}"
                ),
                # Pre-rewrite: the origin path only exists losslessly here.
                foreign_cwds=foreign_cwds(session.transcript, spec),
            )
        )

    payload = {
        "ref": args.ref,
        "store": store.describe(),
        "cwd": target_cwd,
        "origin": {
            "hostname": bundle.origin.hostname,
            "home": bundle.origin.home,
            "platform": bundle.origin.platform,
        },
        "sessions": [_restored_payload(session) for session in restored],
    }
    warnings = tuple(
        f"  skipped {session.session_id} (already exists; --force to overwrite)\n"
        for session in restored
        if session.status == "skipped"
    ) + tuple(
        f"  {session.session_id} worked in {origin} for {foreign.count} record(s), "
        f"which no rewrite into {target_cwd} can satisfy\n"
        for session in restored
        for origin, foreign in session.foreign_cwds.items()
    )

    if not any(session.status == "restored" for session in restored):
        raise CommandError("nothing-restored", "nothing restored", payload, warnings)

    return Result(
        command="restore",
        payload=payload,
        text=_restore_text(bundle, restored),
        warnings=warnings,
    )


def _restored_payload(session: RestoredSession) -> dict[str, Any]:
    return {
        "sessionId": session.session_id,
        "originSessionId": session.origin_session_id,
        "branch": session.branch,
        "status": session.status,
        "path": session.path,
        "residualOriginPaths": list(session.residual_origin_paths),
        # Keyed by the origin path, which is the left-hand side a caller states
        # a mapping with; `restored` is what the file on disk actually says.
        "foreignCwds": {
            origin: {"count": foreign.count, "restored": foreign.restored}
            for origin, foreign in session.foreign_cwds.items()
        },
        "resumeCommand": session.resume_command,
    }


def _restore_text(bundle: Bundle, restored: list[RestoredSession]) -> str:
    lines = []
    for session in restored:
        if session.status != "restored":
            continue
        residual = session.residual_origin_paths
        note = f"  (residual origin paths: {', '.join(residual)})" if residual else ""
        lines.append(
            f"  restored {session.session_id} [{session.branch or 'no branch'}]{note}\n"
        )
        for origin, foreign in session.foreign_cwds.items():
            lines.append(
                f"    {foreign.count} record(s) worked in {origin}, "
                f"now {foreign.restored}\n"
            )
    lines.append(
        f"\nfrom {bundle.origin.hostname} ({bundle.origin.home})\nresume with:\n"
    )
    lines += [
        f"  {session.resume_command}\n"
        for session in restored
        if session.resume_command
    ]
    return "".join(lines)


def command_delete(argv: list[str]) -> Result:
    parser = _parser("delete")
    parser.add_argument("ref", nargs="?")
    parser.add_argument("--store")
    args = parser.parse_args(argv)

    if not args.ref:
        fail("missing-ref", "delete requires a bundle ref")

    store = create_store(resolve_store_uri(args.store))
    with store_errors(f"could not delete {args.ref} from {store.describe()}"):
        removed = delete_bundle(store, args.ref)

    text = "".join(f"  removed {key}\n" for key in removed)
    text += f"\ndeleted {args.ref} from {store.describe()}\n"
    return Result(
        command="delete",
        payload={"ref": args.ref, "store": store.describe(), "removed": removed},
        text=text,
    )


def command_reindex(argv: list[str]) -> Result:
    parser = _parser("reindex")
    parser.add_argument("--store")
    args = parser.parse_args(argv)

    store = create_store(resolve_store_uri(args.store))
    with store_errors(f"could not reindex {store.describe()}"):
        result = reindex(store)

    return Result(
        command="reindex",
        payload={
            "store": store.describe(),
            "bundles": result.bundles,
            "entries": result.entries,
            "removed": result.removed,
        },
        text=(
            f"scanned {result.bundles} bundle(s) in {store.describe()}\n"
            f"wrote {result.entries} index entries, removed {result.removed} stale\n"
        ),
    )


def command_help(argv: list[str]) -> Result:
    return Result(command="help", payload={"usage": USAGE}, text=USAGE)


def installed_version() -> str:
    """Read from package metadata rather than held here, so there is one copy
    of the number and pyproject stays the one that sets it. A source checkout
    that was never installed has no metadata to read, and pyproject can't be
    parsed for it either — tomllib arrived in 3.11 and the floor is 3.10."""
    try:
        return importlib.metadata.version("lochy")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def command_version(argv: list[str]) -> Result:
    version = installed_version()
    return Result(
        command="version", payload={"version": version}, text=f"lochy {version}\n"
    )


def _platform_name() -> str:
    system = platform.system().lower()
    return {"darwin": "darwin", "linux": "linux", "windows": "win32"}.get(
        system, system
    )


COMMANDS: dict[str, Callable[[list[str]], Result]] = {
    "list": command_list,
    "save": command_save,
    "restore": command_restore,
    "delete": command_delete,
    "reindex": command_reindex,
    "help": command_help,
    "version": command_version,
}


def main() -> None:
    argv = sys.argv[1:]
    # Read off argv rather than from a parsed namespace: an unknown command, or
    # a subparser rejecting a flag, has to render in the mode the caller asked
    # for, and neither of those has a namespace by the time it fails.
    as_json = JSON_FLAG in argv
    if argv and argv[0] == JSON_FLAG:
        argv = argv[1:]

    command = argv[0] if argv else "help"
    if command in ("--help", "-h"):
        command = "help"
    if command in ("--version", "-V"):
        command = "version"

    try:
        handler = COMMANDS.get(command)
        if handler is None:
            fail("unknown-command", f"unknown command '{command}' (try: lochy help)")
        result = handler(argv[1:])
    except HelpRequested as help_text:
        result = Result(command, {"usage": help_text.text}, text=help_text.text)
    except CommandError as error:
        emit_error(command, error, as_json)
    except Exception as error:
        # A traceback is the most useful thing a human can get, and the one
        # thing a JSON consumer can't parse.
        if not as_json:
            raise
        emit_error(command, CommandError("internal", str(error)), True)

    emit(result, as_json)


if __name__ == "__main__":
    main()
