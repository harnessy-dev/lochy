import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redact import RedactionError, redact

AGENT = "claude-code"

SUMMARY_LIMIT = 120


@dataclass(frozen=True)
class SessionMeta:
    session_id: str
    path: str
    cwd: str
    git_branch: str | None
    claude_version: str | None
    bytes: int
    modified_at: str
    summary: str | None
    # Directories the session also worked in before it moved. A restore has one
    # target cwd, so records naming these cannot be made correct on the far
    # side; surfacing them here is the only honest option.
    other_cwds: tuple[str, ...] = ()


def home_dir() -> str:
    return str(Path.home())


def iso_timestamp(seconds: float) -> str:
    """Millisecond-precision UTC with a Z suffix, as the bundle format
    stores timestamps."""
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"


def projects_dir(home: str | None = None) -> str:
    return os.path.join(home if home is not None else home_dir(), ".claude", "projects")


def resolve_cwd(cwd: str) -> str:
    """Claude resolves symlinks before deriving the directory name, so /tmp
    becomes /private/tmp on macOS. Resolving here keeps lookups matching."""
    try:
        return os.path.realpath(cwd, strict=True)
    except OSError:
        return cwd


def encode_path(path: str) -> str:
    """Pure form of the encoding, for paths that don't exist locally (an
    origin machine's cwd) and so can't be passed through realpath."""
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def encode_cwd(cwd: str) -> str:
    return encode_path(resolve_cwd(cwd))


def session_dir_for(cwd: str, home: str | None = None) -> str:
    return os.path.join(projects_dir(home), encode_cwd(cwd))


def transcript_path_for(cwd: str, session_id: str, home: str | None = None) -> str:
    return os.path.join(session_dir_for(cwd, home), f"{session_id}.jsonl")


def _first_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value and value != "-" else None


def _records(raw: str) -> Iterator[dict[str, Any]]:
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def transcript_cwds(raw: str) -> dict[str, int]:
    """Every cwd a transcript's records name, with how many name it, in order
    of first appearance. Only the top-level field counts: `"cwd"` also occurs
    inside tool inputs and results — lochy's own --json output among them —
    where it names some other process's directory."""
    counts: dict[str, int] = {}
    for record in _records(raw):
        cwd = _first_string(record.get("cwd"))
        if cwd:
            counts[cwd] = counts.get(cwd, 0) + 1
    return counts


def cwd_for_directory(cwds: list[str], directory: str) -> str:
    """Which of a session's cwd values the transcript is actually filed under.

    A session holds more than one whenever the agent moves mid-session — into a
    worktree, say — because the records from before the move stay in the file.
    The first one can be hundreds of lines stale, and it is the one a restore
    would otherwise rewrite from. Claude names the containing directory after
    the *current* cwd, which makes the directory the disambiguator.

    The encoding is only ever compared here, never decoded. encode_path maps
    every non-alphanumeric to `-`, so `<repo>/sub` and `<repo>-sub` produce the
    same slug: a match is strong evidence, a mismatch is conclusive. That
    asymmetry is what makes this safe. Nothing matching — a hand-copied
    transcript, a renamed directory — falls back to the first cwd, which is the
    behaviour this replaced.
    """
    for cwd in cwds:
        if encode_path(cwd) == directory:
            return cwd
    return cwds[0]


def _extract_summary(record: dict[str, Any]) -> str | None:
    if record.get("type") != "user":
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    for block in content:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str) and text:
            return text
    return None


def _display_summary(text: str | None) -> str | None:
    """The one piece of transcript content that travels as metadata — onto a
    picker row, into a pasted bug report — so it is scrubbed here rather than
    at each renderer, and before truncation, since a cut can leave a secret's
    tail matching no rule. A summary that won't come clean is dropped whole:
    failing a listing over a label would be the wrong trade."""
    if text is None:
        return None
    try:
        return redact(text).text[:SUMMARY_LIMIT]
    except RedactionError:
        return "[REDACTED]"


def read_session_meta(path: str) -> SessionMeta | None:
    try:
        stat = os.stat(path)
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None

    session_id = re.sub(r"\.jsonl$", "", path.split("/")[-1])
    counts: dict[str, int] = {}
    branches: dict[str, str] = {}
    versions: dict[str, str] = {}
    summary: str | None = None

    # Whole file rather than a bounded window: the cwd the session is filed
    # under can first appear hundreds of records in, so a window deep enough to
    # be correct is no window at all. Only the branch and version are read per
    # cwd, which costs one dict lookup on a line already being parsed.
    for record in _records(raw):
        cwd = _first_string(record.get("cwd"))
        if cwd:
            counts[cwd] = counts.get(cwd, 0) + 1
            branch = _first_string(record.get("gitBranch"))
            if branch and cwd not in branches:
                branches[cwd] = branch
            version = _first_string(record.get("version"))
            if version and cwd not in versions:
                versions[cwd] = version
        if summary is None:
            summary = _extract_summary(record)

    if not counts:
        return None

    ordered = list(counts)
    cwd = cwd_for_directory(ordered, os.path.basename(os.path.dirname(path)))

    return SessionMeta(
        session_id=session_id,
        path=path,
        cwd=cwd,
        # Read alongside the chosen cwd. A branch from a record the session has
        # since moved away from is the same defect as the stale cwd, and it
        # decides which index entry the bundle gets filed under.
        git_branch=branches.get(cwd),
        claude_version=versions.get(cwd) or next(iter(versions.values()), None),
        bytes=stat.st_size,
        modified_at=iso_timestamp(stat.st_mtime),
        summary=_display_summary(summary),
        other_cwds=tuple(other for other in ordered if other != cwd),
    )


def list_sessions(
    cwd: str,
    branch: str | None = None,
    session_id: str | None = None,
    home: str | None = None,
) -> list[SessionMeta]:
    directory = session_dir_for(cwd, home)
    if not os.path.exists(directory):
        return []

    metas: list[SessionMeta] = []
    for file in os.listdir(directory):
        if not file.endswith(".jsonl"):
            continue
        if session_id and file != f"{session_id}.jsonl":
            continue
        meta = read_session_meta(os.path.join(directory, file))
        if not meta:
            continue
        if branch and meta.git_branch != branch:
            continue
        metas.append(meta)

    return sorted(metas, key=lambda meta: meta.modified_at, reverse=True)
