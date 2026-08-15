import argparse
import os
import platform
import re
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import NoReturn

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
    load_entries,
    reindex,
    value_prefix,
)
from .redact import RedactionError, redact, summarize
from .rewrite import RewriteSpec, residual_origin_paths, rewrite_transcript
from .store import create_store, resolve_store_uri

USAGE = """lochy — save and restore agent coding sessions across machines

Usage:
  lochy list    [--cwd <path>] [--branch <name>]
  lochy list    --remote [--branch <name>] [--all] [--store <uri>]
  lochy save    [--cwd <path>] [--branch <name>] [--session <id>] [--store <uri>]
  lochy restore <ref> [--into <path>] [--store <uri>] [--force] [--new-id]
  lochy delete  <ref> [--store <uri>]
  lochy reindex [--store <uri>]

Stores:
  <path>              local directory
  s3://bucket/prefix  any S3-compatible endpoint

Environment:
  LOCHY_STORE        default store URI
  LOCHY_S3_ENDPOINT  custom endpoint (R2, MinIO, ...)
  LOCHY_S3_REGION    region override
"""


def fail(message: str) -> NoReturn:
    sys.stderr.write(f"lochy: {message}\n")
    raise SystemExit(1)


def format_bytes(count: int) -> str:
    if count < 1024:
        return f"{count}B"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f}K"
    return f"{count / (1024 * 1024):.1f}M"


def describe(meta: SessionMeta) -> str:
    branch = f"[{meta.git_branch}]" if meta.git_branch else "[no branch]"
    summary = f" {re.sub(r'\s+', ' ', meta.summary)}" if meta.summary else ""
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


def _parser(command: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=f"lochy {command}")


def command_list(argv: list[str]) -> None:
    parser = _parser("list")
    parser.add_argument("--cwd")
    parser.add_argument("--branch")
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--all", action="store_true", dest="every")
    parser.add_argument("--store")
    args = parser.parse_args(argv)

    if args.remote or args.every or args.store is not None:
        list_remote(args)
        return

    cwd, sessions = collect(args.cwd, args.branch)
    if not sessions:
        sys.stdout.write(f"no {AGENT} sessions found for {cwd}\n")
        return

    sys.stdout.write(f"{len(sessions)} session(s) for {cwd}\n")
    for meta in sessions:
        sys.stdout.write(f"  {describe(meta)}\n")


def list_remote(args: argparse.Namespace) -> None:
    store = create_store(resolve_store_uri(args.store))
    cwd = resolve_cwd(args.cwd if args.cwd is not None else os.getcwd())
    branch = args.branch if args.branch is not None else current_branch(cwd)
    scope = "" if args.every else f" for [{branch or 'no branch'}]"

    prefix = dimension_prefix(BRANCH) if args.every else value_prefix(BRANCH, branch)
    entries = load_entries(store, prefix)
    if not entries:
        sys.stdout.write(f"no bundles{scope} in {store.describe()}\n")
        return

    refs = {entry.ref for entry in entries}
    sys.stdout.write(f"{len(refs)} bundle(s){scope} in {store.describe()}\n")
    for entry in entries:
        sys.stdout.write(f"  {entry.ref}\n    {describe_entry(entry)}\n")


def command_save(argv: list[str]) -> None:
    parser = _parser("save")
    parser.add_argument("--cwd")
    parser.add_argument("--branch")
    parser.add_argument("--session")
    parser.add_argument("--store")
    args = parser.parse_args(argv)

    cwd, sessions = collect(args.cwd, args.branch, args.session)
    if not sessions:
        fail(f"no {AGENT} sessions found for {cwd}")

    packed_sessions: list[BundleSession] = []
    redaction_notes: list[str] = []
    for meta in sessions:
        # In memory only — the agent's own transcript on disk stays verbatim.
        try:
            scrubbed = redact(Path(meta.path).read_text(encoding="utf-8"))
        except RedactionError as error:
            fail(f"refusing to pack {meta.session_id}: {error}")
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
        redaction_notes.append(summarize(scrubbed.counts))

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
    # Bundle first: an index entry is derived from it, so a pointer to a
    # bundle that isn't there yet is the worse half of a failed save.
    store.put(bundle_key(ref), packed)
    entries = index_bundle(store, bundle, ref, len(packed))

    for session, redacted in zip(bundle.sessions, redaction_notes):
        note = f" — {redacted}" if redacted else ""
        sys.stdout.write(
            f"  packed {session.session_id} [{session.git_branch or 'no branch'}]{note}\n"
        )
    indexed = ", ".join(f"[{entry.value or 'no branch'}]" for entry in entries)
    sys.stdout.write(f"\nstored {format_bytes(len(packed))} in {store.describe()}\n")
    sys.stdout.write(f"indexed under {indexed}\n")
    sys.stdout.write(f"ref {ref}\n")


def command_restore(argv: list[str]) -> None:
    parser = _parser("restore")
    parser.add_argument("ref", nargs="?")
    parser.add_argument("--into")
    parser.add_argument("--store")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--new-id", action="store_true", dest="new_id")
    args = parser.parse_args(argv)

    if not args.ref:
        fail("restore requires a bundle ref")

    store = create_store(resolve_store_uri(args.store))
    try:
        bundle = unpack_bundle(store.get(bundle_key(args.ref)))
    except Exception as error:
        fail(f"could not read {args.ref} from {store.describe()}: {error}")

    target_cwd = resolve_cwd(args.into if args.into is not None else os.getcwd())
    target_home = home_dir()
    resume_commands: list[str] = []

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
            sys.stderr.write(
                f"  skipped {target_session_id} (already exists; --force to overwrite)\n"
            )
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(transcript, encoding="utf-8")

        residual = residual_origin_paths(transcript, spec)
        note = f"  (residual origin paths: {', '.join(residual)})" if residual else ""
        sys.stdout.write(
            f"  restored {target_session_id} [{session.git_branch or 'no branch'}]{note}\n"
        )
        resume_commands.append(
            f"  cd {target_cwd} && claude --resume {target_session_id}"
        )

    if not resume_commands:
        fail("nothing restored")

    sys.stdout.write(
        f"\nfrom {bundle.origin.hostname} ({bundle.origin.home})\nresume with:\n"
    )
    for command in resume_commands:
        sys.stdout.write(f"{command}\n")


def command_delete(argv: list[str]) -> None:
    parser = _parser("delete")
    parser.add_argument("ref", nargs="?")
    parser.add_argument("--store")
    args = parser.parse_args(argv)

    if not args.ref:
        fail("delete requires a bundle ref")

    store = create_store(resolve_store_uri(args.store))
    try:
        removed = delete_bundle(store, args.ref)
    except Exception as error:
        fail(f"could not delete {args.ref} from {store.describe()}: {error}")

    for key in removed:
        sys.stdout.write(f"  removed {key}\n")
    sys.stdout.write(f"\ndeleted {args.ref} from {store.describe()}\n")


def command_reindex(argv: list[str]) -> None:
    parser = _parser("reindex")
    parser.add_argument("--store")
    args = parser.parse_args(argv)

    store = create_store(resolve_store_uri(args.store))
    result = reindex(store)

    sys.stdout.write(f"scanned {result.bundles} bundle(s) in {store.describe()}\n")
    sys.stdout.write(
        f"wrote {result.entries} index entries, removed {result.removed} stale\n"
    )


def _platform_name() -> str:
    system = platform.system().lower()
    return {"darwin": "darwin", "linux": "linux", "windows": "win32"}.get(
        system, system
    )


def main() -> None:
    argv = sys.argv[1:]
    command = argv[0] if argv else None
    rest = argv[1:]

    if command == "list":
        command_list(rest)
    elif command == "save":
        command_save(rest)
    elif command == "restore":
        command_restore(rest)
    elif command == "delete":
        command_delete(rest)
    elif command == "reindex":
        command_reindex(rest)
    elif command in ("help", "--help", "-h", None):
        sys.stdout.write(USAGE)
    else:
        fail(f"unknown command '{command}' (try: lochy help)")


if __name__ == "__main__":
    main()
