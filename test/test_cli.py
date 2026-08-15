import json
import re
from pathlib import Path
from typing import Any

import pytest
from test_claude import SECRET, write_session

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


def invoke_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    home: Path,
    *argv: str,
) -> tuple[dict[str, Any], str, int | None]:
    """Asserts the contract a consumer depends on: stdout is one JSON document
    and nothing else. Prose leaking from any helper fails the parse."""
    out, err, code = invoke(monkeypatch, capsys, home, *argv)
    assert out.count("\n") == 1
    document = json.loads(out)
    assert document["schema"] == 1
    return document, err, code


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
    assert (store / "bundles" / f"{ref.group(1)}.loch").exists()
    assert (store / "index" / "branch" / "main" / ref.group(1)).exists()

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
    bundle = unpack_bundle((store / "bundles" / f"{ref.group(1)}.loch").read_bytes())
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


def save_two_branches(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    home: Path,
    project: Path,
    store: Path,
) -> str:
    project.mkdir(parents=True, exist_ok=True)
    write_session(home, str(project), "abc", git_branch="main")
    write_session(home, str(project), "def", git_branch="feature/foo")

    out, _, _ = invoke(
        monkeypatch, capsys, home, "save", "--cwd", str(project), "--store", str(store)
    )
    ref = re.search(r"^ref ([0-9a-f]{64})$", out, re.M)
    assert ref is not None
    assert "indexed under [feature/foo], [main]" in out
    return ref.group(1)


def test_list_remote_finds_a_bundle_by_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    store = tmp_path / "store"
    ref = save_two_branches(monkeypatch, capsys, home, tmp_path / "proj", store)

    out, _, _ = invoke(
        monkeypatch,
        capsys,
        home,
        "list",
        "--remote",
        "--branch",
        "feature/foo",
        "--store",
        str(store),
    )
    assert "1 bundle(s) for [feature/foo]" in out
    assert ref in out
    assert "1 session(s)" in out
    assert "claude 1.0.99" in out

    out, _, _ = invoke(
        monkeypatch,
        capsys,
        home,
        "list",
        "--remote",
        "--branch",
        "nope",
        "--store",
        str(store),
    )
    assert out.startswith("no bundles for [nope] in")


def test_list_remote_defaults_to_the_branch_you_are_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    store = tmp_path / "store"
    ref = save_two_branches(monkeypatch, capsys, home, tmp_path / "proj", store)
    monkeypatch.setattr("lochy.cli.current_branch", lambda cwd: "feature/foo")

    out, _, _ = invoke(
        monkeypatch, capsys, home, "list", "--remote", "--store", str(store)
    )

    assert "1 bundle(s) for [feature/foo]" in out
    assert ref in out


def test_list_remote_all_shows_every_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    store = tmp_path / "store"
    save_two_branches(monkeypatch, capsys, home, tmp_path / "proj", store)

    out, _, _ = invoke(
        monkeypatch, capsys, home, "list", "--all", "--store", str(store)
    )

    assert out.startswith("1 bundle(s) in")
    assert "[feature/foo]" in out
    assert "[main]" in out


def test_delete_removes_the_bundle_and_its_entries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    store = tmp_path / "store"
    ref = save_two_branches(monkeypatch, capsys, home, tmp_path / "proj", store)

    out, _, _ = invoke(monkeypatch, capsys, home, "delete", ref, "--store", str(store))
    assert f"removed bundles/{ref}.loch" in out
    assert f"removed index/branch/feature%2Ffoo/{ref}" in out

    out, _, _ = invoke(
        monkeypatch, capsys, home, "list", "--all", "--store", str(store)
    )
    assert out.startswith("no bundles in")

    _, err, code = invoke(
        monkeypatch, capsys, home, "restore", ref, "--store", str(store)
    )
    assert err.startswith(f"lochy: could not read {ref} from")
    assert code == 1


def test_delete_reports_a_ref_that_is_not_there(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _, err, code = invoke(
        monkeypatch, capsys, tmp_path, "delete", "deadbeef", "--store", str(tmp_path)
    )
    assert err.startswith("lochy: could not delete deadbeef from")
    assert code == 1


def test_reindex_rebuilds_a_wiped_index(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    store = tmp_path / "store"
    ref = save_two_branches(monkeypatch, capsys, home, tmp_path / "proj", store)

    for entry in (store / "index").rglob("*"):
        if entry.is_file():
            entry.unlink()

    out, _, _ = invoke(monkeypatch, capsys, home, "reindex", "--store", str(store))
    assert "scanned 1 bundle(s) in" in out
    assert "wrote 2 index entries, removed 0 stale" in out

    out, _, _ = invoke(
        monkeypatch, capsys, home, "list", "--all", "--store", str(store)
    )
    assert ref in out
    assert "[feature/foo]" in out
    assert "[main]" in out


def test_json_list_emits_raw_values_not_formatted_columns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    write_session(home, str(project), "abc", summary="do the thing")

    document, err, code = invoke_json(
        monkeypatch, capsys, home, "list", "--json", "--cwd", str(project)
    )

    assert (document["ok"], document["command"], code, err) == (True, "list", None, "")
    assert document["remote"] is False
    (session,) = document["sessions"]
    assert session["sessionId"] == "abc"
    assert session["branch"] == "main"
    assert isinstance(session["bytes"], int)
    assert re.fullmatch(r".*\dZ", session["modifiedAt"])


def test_json_list_carries_a_scrubbed_summary_in_both_renderings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The summary is the only transcript content either rendering prints, and
    it reaches a picker row and a pasted bug report without a `save` in between."""
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    write_session(home, str(project), "abc", summary=f"deploy with {SECRET}")

    document, _, _ = invoke_json(
        monkeypatch, capsys, home, "list", "--json", "--cwd", str(project)
    )
    out, _, _ = invoke(monkeypatch, capsys, home, "list", "--cwd", str(project))

    assert document["sessions"][0]["summary"] == "deploy with [REDACTED:github-token]"
    assert SECRET not in out


def test_json_list_flag_is_accepted_before_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    write_session(home, str(project), "abc")

    document, _, _ = invoke_json(
        monkeypatch, capsys, home, "--json", "list", "--cwd", str(project)
    )

    assert [session["sessionId"] for session in document["sessions"]] == ["abc"]


def test_json_list_finding_nothing_is_an_empty_result_not_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    document, _, code = invoke_json(
        monkeypatch,
        capsys,
        tmp_path / "home",
        "list",
        "--json",
        "--cwd",
        str(tmp_path),
    )

    assert document["ok"] is True
    assert document["sessions"] == []
    assert code is None


def test_json_list_remote_finding_nothing_is_an_empty_result_not_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    store = tmp_path / "store"
    save_two_branches(monkeypatch, capsys, home, tmp_path / "proj", store)

    document, _, code = invoke_json(
        monkeypatch,
        capsys,
        home,
        "list",
        "--json",
        "--remote",
        "--branch",
        "nope",
        "--store",
        str(store),
    )

    assert document["ok"] is True
    assert document["entries"] == []
    assert document["bundles"] == 0
    assert code is None


def test_json_list_remote_reports_entries_by_dimension(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    store = tmp_path / "store"
    ref = save_two_branches(monkeypatch, capsys, home, tmp_path / "proj", store)

    document, _, _ = invoke_json(
        monkeypatch, capsys, home, "list", "--json", "--all", "--store", str(store)
    )

    assert document["remote"] is True
    assert document["all"] is True
    assert document["bundles"] == 1
    assert {(entry["dimension"], entry["value"]) for entry in document["entries"]} == {
        ("branch", "main"),
        ("branch", "feature/foo"),
    }
    assert all(entry["ref"] == ref for entry in document["entries"])
    assert all(isinstance(entry["bytes"], int) for entry in document["entries"])


def test_json_save_reports_the_ref_and_redaction_counts_without_the_match(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "store"
    secret = "AKIAZZZZZZZZEXAMPLE0"
    write_session(home, str(project), "abc", summary=f"ran env, saw {secret}")

    document, _, _ = invoke_json(
        monkeypatch,
        capsys,
        home,
        "save",
        "--json",
        "--cwd",
        str(project),
        "--store",
        str(store),
    )

    assert re.fullmatch(r"[0-9a-f]{64}", document["ref"])
    assert (
        document["bytes"]
        == (store / "bundles" / f"{document['ref']}.loch").stat().st_size
    )
    assert document["store"] == str(store)
    assert document["redacted"] == 1
    (session,) = document["sessions"]
    assert session["sessionId"] == "abc"
    assert session["branch"] == "main"
    assert session["redactions"] == {"aws-access-key": 1}
    assert document["indexed"] == [
        {
            "dimension": "branch",
            "value": "main",
            "key": f"index/branch/main/{document['ref']}",
        }
    ]
    assert secret not in json.dumps(document)


def test_json_save_reports_every_branch_it_indexed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "store"
    write_session(home, str(project), "abc", git_branch="main")
    write_session(home, str(project), "def", git_branch="feature/foo")

    document, _, _ = invoke_json(
        monkeypatch,
        capsys,
        home,
        "save",
        "--json",
        "--cwd",
        str(project),
        "--store",
        str(store),
    )

    assert {entry["value"] for entry in document["indexed"]} == {"main", "feature/foo"}
    assert {session["sessionId"] for session in document["sessions"]} == {"abc", "def"}
    assert document["redacted"] == 0


def test_json_restore_reports_per_session_status_and_resume_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    origin_home = tmp_path / "origin-home"
    origin_project = tmp_path / "origin" / "proj"
    origin_project.mkdir(parents=True)
    store = tmp_path / "store"
    write_session(origin_home, str(origin_project), "abc")

    saved, _, _ = invoke_json(
        monkeypatch,
        capsys,
        origin_home,
        "save",
        "--json",
        "--cwd",
        str(origin_project),
        "--store",
        str(store),
    )

    target_home = tmp_path / "target-home"
    target_project = tmp_path / "target" / "proj"
    target_project.mkdir(parents=True)

    document, err, code = invoke_json(
        monkeypatch,
        capsys,
        target_home,
        "restore",
        "--json",
        saved["ref"],
        "--into",
        str(target_project),
        "--store",
        str(store),
    )

    assert (document["ok"], code, err) == (True, None, "")
    assert document["cwd"] == str(target_project.resolve())
    assert document["origin"]["home"] == str(origin_home)
    (session,) = document["sessions"]
    assert session["status"] == "restored"
    assert session["sessionId"] == "abc"
    assert session["originSessionId"] == "abc"
    assert session["residualOriginPaths"] == []
    assert session["resumeCommand"] == (
        f"cd {target_project.resolve()} && claude --resume abc"
    )
    assert Path(session["path"]).read_text(encoding="utf-8")


def test_json_restore_reports_residual_origin_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "store"
    write_session(home, str(project), "abc")

    saved, _, _ = invoke_json(
        monkeypatch,
        capsys,
        home,
        "save",
        "--json",
        "--cwd",
        str(project),
        "--store",
        str(store),
    )

    # Restoring below the origin cwd leaves it a substring of the target, so
    # the rewritten transcript still mentions a path from the origin machine.
    nested = project / "nested"
    nested.mkdir()
    document, _, _ = invoke_json(
        monkeypatch,
        capsys,
        home,
        "restore",
        "--json",
        saved["ref"],
        "--into",
        str(nested),
        "--store",
        str(store),
    )

    (session,) = document["sessions"]
    assert session["residualOriginPaths"] == [str(project.resolve())]


def test_json_restore_reports_a_skip_as_status_not_as_stderr_prose(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "store"
    write_session(home, str(project), "abc")

    saved, _, _ = invoke_json(
        monkeypatch,
        capsys,
        home,
        "save",
        "--json",
        "--cwd",
        str(project),
        "--store",
        str(store),
    )

    document, err, code = invoke_json(
        monkeypatch,
        capsys,
        home,
        "restore",
        "--json",
        saved["ref"],
        "--into",
        str(project),
        "--store",
        str(store),
    )

    assert (document["ok"], document["code"], code) == (False, "nothing-restored", 1)
    assert err == ""
    (session,) = document["sessions"]
    assert session["status"] == "skipped"
    assert session["resumeCommand"] is None
    assert session["path"] == transcript_path_for(str(project), "abc", str(home))


def test_json_delete_reports_the_keys_it_removed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    store = tmp_path / "store"
    ref = save_two_branches(monkeypatch, capsys, home, tmp_path / "proj", store)

    document, _, code = invoke_json(
        monkeypatch, capsys, home, "delete", "--json", ref, "--store", str(store)
    )

    assert (document["ok"], code) == (True, None)
    assert document["ref"] == ref
    assert set(document["removed"]) == {
        f"bundles/{ref}.loch",
        f"index/branch/main/{ref}",
        f"index/branch/feature%2Ffoo/{ref}",
    }


def test_json_reindex_reports_what_it_scanned_and_wrote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    home = tmp_path / "home"
    store = tmp_path / "store"
    save_two_branches(monkeypatch, capsys, home, tmp_path / "proj", store)

    for entry in (store / "index").rglob("*"):
        if entry.is_file():
            entry.unlink()

    document, _, _ = invoke_json(
        monkeypatch, capsys, home, "reindex", "--json", "--store", str(store)
    )

    assert (document["bundles"], document["entries"], document["removed"]) == (1, 2, 0)
    assert document["store"] == str(store)


def test_json_failure_is_still_one_parseable_document_on_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    document, err, code = invoke_json(
        monkeypatch,
        capsys,
        tmp_path / "home",
        "save",
        "--json",
        "--cwd",
        str(tmp_path),
        "--store",
        str(tmp_path / "store"),
    )

    assert (document["ok"], document["code"], code) == (False, "no-sessions", 1)
    assert document["error"].startswith("no claude-code sessions found for")
    assert err == ""


def test_json_separates_a_ref_that_is_absent_from_a_store_that_did_not_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The distinction a consumer acts on: an absent ref is terminal, an
    unreachable store is worth retrying."""
    document, _, code = invoke_json(monkeypatch, capsys, tmp_path, "restore", "--json")
    assert (document["code"], code) == ("missing-ref", 1)

    for command in ("restore", "delete"):
        document, _, code = invoke_json(
            monkeypatch,
            capsys,
            tmp_path,
            command,
            "--json",
            "deadbeef",
            "--store",
            str(tmp_path / "store"),
        )
        assert (document["code"], code) == ("bundle-not-found", 1)

    # A file where the store's directory should be: the store cannot answer at
    # all, which is a different failure from the object being absent.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a store", encoding="utf-8")
    document, _, code = invoke_json(
        monkeypatch,
        capsys,
        tmp_path,
        "restore",
        "--json",
        "deadbeef",
        "--store",
        str(blocked),
    )
    assert (document["code"], code) == ("store-unreachable", 1)


def test_json_reports_a_bundle_that_arrived_but_will_not_unpack(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    store = tmp_path / "store"
    bundles = store / "bundles"
    bundles.mkdir(parents=True)
    (bundles / "deadbeef.loch").write_bytes(b"not a gzip stream")

    document, _, code = invoke_json(
        monkeypatch,
        capsys,
        tmp_path,
        "restore",
        "--json",
        "deadbeef",
        "--store",
        str(store),
    )

    assert (document["code"], code) == ("bundle-unreadable", 1)


def test_json_reports_an_unknown_command_and_a_bad_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    document, err, code = invoke_json(monkeypatch, capsys, tmp_path, "--json", "bogus")
    assert (document["command"], document["code"], code) == (
        "bogus",
        "unknown-command",
        1,
    )
    assert err == ""

    document, err, code = invoke_json(
        monkeypatch, capsys, tmp_path, "list", "--json", "--bogus"
    )
    assert (document["command"], document["code"], code) == ("list", "usage", 2)
    assert err == ""


def test_json_help_is_a_document_rather_than_the_usage_block(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    document, _, code = invoke_json(monkeypatch, capsys, tmp_path, "--json")
    assert (document["command"], document["ok"], code) == ("help", True, None)
    assert document["usage"].startswith("lochy — save and restore")

    document, _, code = invoke_json(
        monkeypatch, capsys, tmp_path, "save", "--json", "--help"
    )
    assert (document["command"], document["ok"], code) == ("save", True, None)
    assert "--store" in document["usage"]
