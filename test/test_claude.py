import json
import os
from pathlib import Path

import pytest

from lochy.claude import (
    SUMMARY_LIMIT,
    encode_cwd,
    encode_path,
    list_sessions,
    read_session_meta,
    resolve_cwd,
    session_dir_for,
    transcript_cwds,
    transcript_path_for,
)
from lochy.redact import Redaction, RedactionError

SECRET = "ghp_" + "a1B2c3D4e5" * 4


def write_session(
    home: Path,
    cwd: str,
    session_id: str,
    git_branch: str | None = "main",
    summary: str = "hello",
) -> Path:
    directory = Path(session_dir_for(cwd, str(home)))
    directory.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {
        "type": "user",
        "sessionId": session_id,
        "cwd": cwd,
        "version": "1.0.99",
        "message": {"role": "user", "content": summary},
    }
    if git_branch is not None:
        record["gitBranch"] = git_branch
    path = directory / f"{session_id}.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def test_encodes_every_non_alphanumeric_character() -> None:
    assert encode_path("/Users/mike/my_proj.v2") == "-Users-mike-my-proj-v2"


def test_resolves_symlinks_before_encoding(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    assert resolve_cwd(str(link)) == os.path.realpath(str(real))
    assert encode_cwd(str(link)) == encode_path(os.path.realpath(str(real)))


def test_keeps_a_path_that_does_not_exist(tmp_path: Path) -> None:
    missing = str(tmp_path / "nope")
    assert resolve_cwd(missing) == missing


def test_transcript_path_lives_under_the_encoded_project_dir(tmp_path: Path) -> None:
    path = transcript_path_for("/Users/mike/proj", "abc", str(tmp_path))
    assert path == str(
        tmp_path / ".claude" / "projects" / "-Users-mike-proj" / "abc.jsonl"
    )


def test_reads_metadata_from_a_transcript(tmp_path: Path) -> None:
    path = write_session(tmp_path, "/Users/mike/proj", "abc", summary="do the thing")

    meta = read_session_meta(str(path))

    assert meta is not None
    assert meta.session_id == "abc"
    assert meta.cwd == "/Users/mike/proj"
    assert meta.git_branch == "main"
    assert meta.claude_version == "1.0.99"
    assert meta.summary == "do the thing"
    assert meta.modified_at.endswith("Z")


def test_a_secret_in_the_first_message_never_reaches_the_summary(
    tmp_path: Path,
) -> None:
    path = write_session(
        tmp_path, "/Users/mike/proj", "abc", summary=f"deploy with {SECRET}"
    )

    meta = read_session_meta(str(path))

    assert meta is not None
    assert meta.summary == "deploy with [REDACTED:github-token]"


def test_a_secret_is_scrubbed_before_the_summary_is_cut_to_length(
    tmp_path: Path,
) -> None:
    """Truncating first would leave a fragment too short for any rule to match,
    so the tail of a secret would ride along unredacted."""
    padding = "x" * (SUMMARY_LIMIT - 11) + " "
    path = write_session(
        tmp_path, "/Users/mike/proj", "abc", summary=f"{padding}{SECRET}"
    )

    meta = read_session_meta(str(path))

    assert meta is not None
    assert len(meta.summary or "") == SUMMARY_LIMIT
    assert "ghp_" not in (meta.summary or "")


def test_a_summary_that_will_not_come_clean_is_dropped_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(text: str) -> Redaction:
        raise RedactionError("still matching")

    monkeypatch.setattr("lochy.claude.redact", refuse)
    path = write_session(tmp_path, "/Users/mike/proj", "abc", summary="anything")

    meta = read_session_meta(str(path))

    assert meta is not None
    assert meta.summary == "[REDACTED]"


def test_ignores_a_transcript_with_no_cwd(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text('{"type":"user"}\n', encoding="utf-8")
    assert read_session_meta(str(path)) is None


# A session that moved: it began in the main repo and continued in a worktree
# after the agent switched, so the records from before the move stay in the file
# and the first several hundred name a directory the session has left. Claude
# files the transcript under the cwd it ended in.
MAIN = "/Users/mike/apps/harness"
WORKTREE = "/Users/mike/apps/harness-worktrees/feat/send"


def write_moved_session(
    home: Path,
    session_id: str = "abc",
    left: str = MAIN,
    filed_under: str = WORKTREE,
    stale: int = 300,
) -> Path:
    def record(cwd: str, branch: str, text: str) -> str:
        return json.dumps(
            {
                "type": "user",
                "sessionId": session_id,
                "cwd": cwd,
                "gitBranch": branch,
                "version": "1.0.99",
                "message": {"role": "user", "content": text},
            }
        )

    directory = Path(session_dir_for(filed_under, str(home)))
    directory.mkdir(parents=True, exist_ok=True)
    lines = [record(left, "main", "started here") for _ in range(stale)]
    lines.append(record(filed_under, "feat/send", "moved here"))
    path = directory / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_prefers_the_cwd_the_project_directory_is_named_after(tmp_path: Path) -> None:
    """The first record's cwd is stale, and the live one first appears well past
    any fixed scan window, so the directory is the only disambiguator."""
    path = write_moved_session(tmp_path)

    meta = read_session_meta(str(path))

    assert meta is not None
    assert meta.cwd == WORKTREE
    assert meta.other_cwds == (MAIN,)


def test_branch_is_read_alongside_the_chosen_cwd(tmp_path: Path) -> None:
    """A branch from a directory the session has left is the same defect as the
    stale cwd, and it decides which index entry the bundle is filed under."""
    path = write_moved_session(tmp_path)

    meta = read_session_meta(str(path))

    assert meta is not None
    assert meta.git_branch == "feat/send"


def test_a_moved_session_is_listed_under_the_branch_it_ended_on(
    tmp_path: Path,
) -> None:
    write_moved_session(tmp_path)

    assert [
        meta.session_id
        for meta in list_sessions(cwd=WORKTREE, branch="feat/send", home=str(tmp_path))
    ] == ["abc"]
    assert list_sessions(cwd=WORKTREE, branch="main", home=str(tmp_path)) == []


def test_falls_back_to_the_first_cwd_when_the_directory_matches_nothing(
    tmp_path: Path,
) -> None:
    """A hand-copied transcript or a renamed directory keeps the old behaviour
    rather than dropping the session."""
    directory = tmp_path / "unrelated"
    directory.mkdir(parents=True)
    path = directory / "abc.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"cwd": MAIN, "gitBranch": "main"}),
                json.dumps({"cwd": WORKTREE, "gitBranch": "feat/send"}),
            ]
        ),
        encoding="utf-8",
    )

    meta = read_session_meta(str(path))

    assert meta is not None
    assert meta.cwd == MAIN
    assert meta.other_cwds == (WORKTREE,)


def test_transcript_cwds_counts_only_the_top_level_field() -> None:
    """`"cwd"` also occurs inside tool inputs and results — lochy's own --json
    output among them — where it names some other process's directory."""
    raw = "\n".join(
        [
            json.dumps(
                {"cwd": MAIN, "toolUseResult": json.dumps({"cwd": "/elsewhere"})}
            ),
            json.dumps({"cwd": MAIN}),
            "",
            "not json at all",
        ]
    )

    assert transcript_cwds(raw) == {MAIN: 2}


def test_lists_and_filters_sessions(tmp_path: Path) -> None:
    cwd = "/Users/mike/proj"
    write_session(tmp_path, cwd, "one", git_branch="main")
    write_session(tmp_path, cwd, "two", git_branch="feature/x")

    every = list_sessions(cwd=cwd, home=str(tmp_path))
    assert {meta.session_id for meta in every} == {"one", "two"}

    by_branch = list_sessions(cwd=cwd, branch="feature/x", home=str(tmp_path))
    assert [meta.session_id for meta in by_branch] == ["two"]

    by_id = list_sessions(cwd=cwd, session_id="one", home=str(tmp_path))
    assert [meta.session_id for meta in by_id] == ["one"]

    assert list_sessions(cwd="/Users/mike/other", home=str(tmp_path)) == []
