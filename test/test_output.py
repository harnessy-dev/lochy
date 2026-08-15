import json

import pytest

from lochy.output import SCHEMA, CommandError, Result, document, emit, emit_error


def test_a_success_envelope_carries_no_error_fields() -> None:
    body = document("save", True, {"ref": "abc"})
    assert body == {"schema": SCHEMA, "ok": True, "command": "save", "ref": "abc"}


def test_a_failure_envelope_carries_the_stable_code() -> None:
    body = document("restore", False, {"ref": "abc"}, "nothing-restored", "nothing")
    assert body["ok"] is False
    assert body["code"] == "nothing-restored"
    assert body["error"] == "nothing"
    assert body["ref"] == "abc"


def test_json_mode_writes_one_document_and_leaves_stderr_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit(Result("list", {"sessions": []}, text="prose", warnings=("warned\n",)), True)

    out, err = capsys.readouterr()
    assert json.loads(out)["sessions"] == []
    assert out.count("\n") == 1
    assert err == ""


def test_text_mode_writes_prose_and_warnings_to_their_own_streams(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit(
        Result("list", {"sessions": []}, text="prose\n", warnings=("warned\n",)), False
    )

    out, err = capsys.readouterr()
    assert out == "prose\n"
    assert err == "warned\n"


def test_an_error_exits_nonzero_in_both_modes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = CommandError("usage", "bad flag", exit_status=2)

    with pytest.raises(SystemExit) as failure:
        emit_error("list", error, True)
    out, err = capsys.readouterr()
    assert failure.value.code == 2
    assert json.loads(out)["code"] == "usage"
    assert err == ""

    with pytest.raises(SystemExit) as failure:
        emit_error("list", error, False)
    out, err = capsys.readouterr()
    assert failure.value.code == 2
    assert out == ""
    assert err == "lochy: bad flag\n"
