import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AGENT = "claude-code"

METADATA_SCAN_LINES = 200


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


def home_dir() -> str:
    return str(Path.home())


def iso_timestamp(seconds: float) -> str:
    """Millisecond-precision UTC with a Z suffix, matching what the
    TypeScript implementation wrote into bundles."""
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


def _extract_summary(record: dict[str, Any]) -> str | None:
    if record.get("type") != "user":
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content[:120]
    if not isinstance(content, list):
        return None
    for block in content:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str) and text:
            return text[:120]
    return None


def read_session_meta(path: str) -> SessionMeta | None:
    try:
        stat = os.stat(path)
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None

    lines = raw.split("\n")
    session_id = re.sub(r"\.jsonl$", "", path.split("/")[-1])
    cwd: str | None = None
    git_branch: str | None = None
    claude_version: str | None = None
    summary: str | None = None

    for line in lines[:METADATA_SCAN_LINES]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        cwd = cwd if cwd is not None else _first_string(record.get("cwd"))
        git_branch = (
            git_branch
            if git_branch is not None
            else _first_string(record.get("gitBranch"))
        )
        claude_version = (
            claude_version
            if claude_version is not None
            else _first_string(record.get("version"))
        )
        summary = summary if summary is not None else _extract_summary(record)
        if cwd and git_branch and claude_version and summary:
            break

    if not cwd:
        return None

    return SessionMeta(
        session_id=session_id,
        path=path,
        cwd=cwd,
        git_branch=git_branch,
        claude_version=claude_version,
        bytes=stat.st_size,
        modified_at=iso_timestamp(stat.st_mtime),
        summary=summary,
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
