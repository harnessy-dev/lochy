import json
import re
from pathlib import Path

import pytest
from test_claude import write_session

from lochy.bundle import unpack_bundle
from lochy.claude import session_dir_for, transcript_path_for
from lochy.cli import main


def invoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    home: Path,
    *argv: str,
) -> tuple[str, str, int | None]:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.argv", ["lochy", *argv])
    code: int | None = None
    try:
        main()
    except SystemExit as exit_signal:
        code = int(exit_signal.code or 0)
    captured = capsys.readouterr()
    return captured.out, captured.err, code


def test_help_is_printed_without_a_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out, _, code = invoke(monkeypatch, capsys, tmp_path)
    assert out.startswith("lochy — save and restore")
    assert code is None


def test_unknown_command_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _, err, code = invoke(monkeypatch, capsys, tmp_path, "bogus")
    assert err == "lochy: unknown command 'bogus' (try: lochy help)\n"
    assert code == 1


def test_list_reports_sessions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    write_session(home, str(project), "abc", summary="do the thing")

    out, _, _ = invoke(monkeypatch, capsys, home, "list", "--cwd", str(project))

    assert "1 session(s) for" in out
    assert "abc" in out
    assert "[main] do the thing" in out


def test_list_reports_nothing_for_an_unknown_cwd(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out, _, _ = invoke(
        monkeypatch, capsys, tmp_path / "home", "list", "--cwd", str(tmp_path)
    )
    assert out.startswith("no claude-code sessions found for")


def test_save_then_restore_onto_another_machine(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    origin_home = tmp_path / "origin-home"
    origin_project = tmp_path / "origin" / "proj"
    origin_project.mkdir(parents=True)
    store = tmp_path / "store"

    transcript = origin_home / "note.txt"
    write_session(origin_home, str(origin_project), "abc")
    session_path = Path(
        transcript_path_for(str(origin_project), "abc", str(origin_home))
    )
    session_path.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "abc",
                "cwd": str(origin_project.resolve()),
                "gitBranch": "main",
                "version": "1.0.99",
                "message": {"role": "user", "content": "read " + str(transcript)},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    out, _, _ = invoke(
        monkeypatch,
        capsys,
        origin_home,
        "save",
        "--cwd",
        str(origin_project),
        "--store",
        str(store),
    )
    assert "packed abc [main]" in out
    ref = re.search(r"^ref ([0-9a-f]{64})$", out, re.M)
    assert ref is not None
    assert (store / f"{ref.group(1)}.loch").exists()

    target_home = tmp_path / "target-home"
    target_project = tmp_path / "target" / "work" / "proj"
    target_project.mkdir(parents=True)

    out, _, _ = invoke(
        monkeypatch,
        capsys,
        target_home,
        "restore",
        ref.group(1),
        "--into",
        str(target_project),
        "--store",
        str(store),
    )
    assert "restored abc [main]" in out
    assert f"cd {target_project.resolve()} && claude --resume abc" in out

    restored = Path(transcript_path_for(str(target_project), "abc", str(target_home)))
    body = restored.read_text(encoding="utf-8")
    assert str(target_project.resolve()) in body
    assert str(origin_project.resolve()) not in body
    assert str(origin_home) not in body
    assert str(target_home / "note.txt") in body


def test_save_redacts_secrets_without_touching_the_local_transcript(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "store"
    secret = "AKIAZZZZZZZZEXAMPLE0"
    session_path = write_session(
        home, str(project), "abc", summary=f"ran env, saw {secret}"
    )

    out, _, _ = invoke(
        monkeypatch, capsys, home, "save", "--cwd", str(project), "--store", str(store)
    )

    assert "redacted 1 secret (aws-access-key ×1)" in out
    assert secret not in out
    assert secret in session_path.read_text(encoding="utf-8")

    ref = re.search(r"^ref ([0-9a-f]{64})$", out, re.M)
    assert ref is not None
    bundle = unpack_bundle((store / f"{ref.group(1)}.loch").read_bytes())
    assert secret not in bundle.sessions[0].transcript
    assert "[REDACTED:aws-access-key]" in bundle.sessions[0].transcript


def test_restore_skips_an_existing_session_unless_forced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "store"
    write_session(home, str(project), "abc")

    out, _, _ = invoke(
        monkeypatch, capsys, home, "save", "--cwd", str(project), "--store", str(store)
    )
    ref = re.search(r"^ref ([0-9a-f]{64})$", out, re.M)
    assert ref is not None

    _, err, code = invoke(
        monkeypatch,
        capsys,
        home,
        "restore",
        ref.group(1),
        "--into",
        str(project),
        "--store",
        str(store),
    )
    assert "skipped abc (already exists; --force to overwrite)" in err
    assert err.endswith("lochy: nothing restored\n")
    assert code == 1

    out, _, _ = invoke(
        monkeypatch,
        capsys,
        home,
        "restore",
        ref.group(1),
        "--into",
        str(project),
        "--store",
        str(store),
        "--force",
    )
    assert "restored abc [main]" in out


def test_restore_with_a_new_id_rewrites_filename_and_records(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "store"
    write_session(home, str(project), "abc")

    out, _, _ = invoke(
        monkeypatch, capsys, home, "save", "--cwd", str(project), "--store", str(store)
    )
    ref = re.search(r"^ref ([0-9a-f]{64})$", out, re.M)
    assert ref is not None

    out, _, _ = invoke(
        monkeypatch,
        capsys,
        home,
        "restore",
        ref.group(1),
        "--into",
        str(project),
        "--store",
        str(store),
        "--new-id",
    )

    directory = Path(session_dir_for(str(project), str(home)))
    minted = [path for path in directory.glob("*.jsonl") if path.stem != "abc"]
    assert len(minted) == 1
    record = json.loads(minted[0].read_text(encoding="utf-8").splitlines()[0])
    assert record["sessionId"] == minted[0].stem


def test_save_fails_when_nothing_matches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _, err, code = invoke(
        monkeypatch,
        capsys,
        tmp_path / "home",
        "save",
        "--cwd",
        str(tmp_path),
        "--store",
        str(tmp_path / "store"),
    )
    assert err.startswith("lochy: no claude-code sessions found for")
    assert code == 1


def test_restore_requires_a_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _, err, code = invoke(monkeypatch, capsys, tmp_path, "restore")
    assert err == "lochy: restore requires a bundle ref\n"
    assert code == 1


def test_restore_reports_an_unreadable_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _, err, code = invoke(
        monkeypatch, capsys, tmp_path, "restore", "deadbeef", "--store", str(tmp_path)
    )
    assert err.startswith("lochy: could not read deadbeef from")
    assert code == 1
