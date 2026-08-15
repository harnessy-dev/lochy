import json
import sys
from dataclasses import dataclass, field
from typing import Any, NoReturn

SCHEMA = 1

JSON_FLAG = "--json"


@dataclass(frozen=True)
class Result:
    """What a command produced, in both renderings. Building the payload and
    the prose in one place is what keeps them from drifting apart."""

    command: str
    payload: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    warnings: tuple[str, ...] = ()


class CommandError(Exception):
    """A failure the caller is meant to act on. `code` is a stable token —
    consumers branch on it, so reword `message` freely but not `code`."""

    def __init__(
        self,
        code: str,
        message: str,
        payload: dict[str, Any] | None = None,
        warnings: tuple[str, ...] = (),
        exit_status: int = 1,
        prose: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.payload = payload if payload is not None else {}
        self.warnings = warnings
        self.exit_status = exit_status
        self.prose = prose


def document(
    command: str,
    ok: bool,
    payload: dict[str, Any],
    code: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"schema": SCHEMA, "ok": ok, "command": command}
    if not ok:
        body["code"] = code
        body["error"] = error
    body.update(payload)
    return body


def emit(result: Result, as_json: bool) -> None:
    if as_json:
        _write(document(result.command, True, result.payload))
        return
    # Warnings are stderr-only prose; JSON carries the same facts as per-item
    # status, so nothing but the document is written in that mode.
    for warning in result.warnings:
        sys.stderr.write(warning)
    sys.stdout.write(result.text)


def emit_error(command: str, error: CommandError, as_json: bool) -> NoReturn:
    if as_json:
        _write(
            document(command, False, error.payload, error.code, error.message),
        )
    else:
        for warning in error.warnings:
            sys.stderr.write(warning)
        sys.stderr.write(error.prose if error.prose else f"lochy: {error.message}\n")
    raise SystemExit(error.exit_status)


def _write(body: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(body) + "\n")
