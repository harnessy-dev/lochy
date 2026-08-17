import json

from lochy.rewrite import (
    ForeignCwd,
    RewriteSpec,
    foreign_cwds,
    residual_origin_paths,
    rewrite_transcript,
)

CROSS_MACHINE = RewriteSpec(
    origin_cwd="/Users/mike/proj",
    origin_home="/Users/mike",
    target_cwd="/Users/alice/work/proj",
    target_home="/Users/alice",
)


def dumps(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def test_rewrites_the_cwd_field_and_paths_inside_tool_results() -> None:
    raw = "\n".join(
        [
            dumps({"type": "user", "cwd": "/Users/mike/proj", "gitBranch": "main"}),
            dumps(
                {"type": "assistant", "toolUseResult": "read /Users/mike/proj/src/a.py"}
            ),
        ]
    )

    out = rewrite_transcript(raw, CROSS_MACHINE)

    assert '"cwd":"/Users/alice/work/proj"' in out
    assert "/Users/alice/work/proj/src/a.py" in out
    assert "/Users/mike" not in out


def test_does_not_double_rewrite_a_cwd_nested_under_the_home_directory() -> None:
    raw = dumps(
        {"cwd": "/Users/mike/proj", "config": "/Users/mike/.claude/settings.json"}
    )

    out = rewrite_transcript(raw, CROSS_MACHINE)

    assert '"cwd":"/Users/alice/work/proj"' in out
    assert "/Users/alice/.claude/settings.json" in out
    assert "/Users/alice/work/proj/.claude/settings.json" not in out


def test_rewrites_the_encoded_project_directory_form() -> None:
    raw = dumps({"note": "~/.claude/projects/-Users-mike-proj/abc.jsonl"})

    out = rewrite_transcript(raw, CROSS_MACHINE)

    assert "-Users-alice-work-proj" in out
    assert "-Users-mike-proj" not in out


def test_rewrites_the_session_id_when_a_new_one_is_minted() -> None:
    raw = dumps({"sessionId": "old-id", "uuid": "x"})

    out = rewrite_transcript(
        raw,
        RewriteSpec(
            origin_cwd=CROSS_MACHINE.origin_cwd,
            origin_home=CROSS_MACHINE.origin_home,
            target_cwd=CROSS_MACHINE.target_cwd,
            target_home=CROSS_MACHINE.target_home,
            origin_session_id="old-id",
            target_session_id="new-id",
        ),
    )

    assert '"sessionId":"new-id"' in out


def test_leaves_the_transcript_untouched_when_origin_and_target_match() -> None:
    same_machine = RewriteSpec(
        origin_cwd="/Users/mike/proj",
        origin_home="/Users/mike",
        target_cwd="/Users/mike/proj",
        target_home="/Users/mike",
    )
    raw = dumps({"cwd": "/Users/mike/proj"})

    assert rewrite_transcript(raw, same_machine) == raw


def test_residual_reports_nothing_when_the_rewrite_was_complete() -> None:
    out = rewrite_transcript(dumps({"cwd": "/Users/mike/proj"}), CROSS_MACHINE)
    assert residual_origin_paths(out, CROSS_MACHINE) == []


def test_residual_reports_origin_paths_that_survived() -> None:
    assert residual_origin_paths(
        "stale /Users/mike/elsewhere/file.py", CROSS_MACHINE
    ) == ["/Users/mike"]


# Harness puts every worktree at `<repo>-worktrees/<branch>`, a sibling of the
# repo, so a session whose cwd is the main repo shares a prefix with every
# worktree path in the same transcript.
HARNESS_LAYOUT = RewriteSpec(
    origin_cwd="/Users/mike/apps/harness",
    origin_home="/Users/mike",
    target_cwd="/private/tmp/dest-worktrees/feat/send",
    target_home="/Users/mike",
)


def test_leaves_a_sibling_directory_that_only_extends_the_cwd() -> None:
    sibling = "/Users/mike/apps/harness-worktrees/feat/send"

    out = rewrite_transcript(dumps({"cwd": sibling}), HARNESS_LAYOUT)

    assert out == dumps({"cwd": sibling})


def test_a_skipped_sibling_becomes_a_visible_residual() -> None:
    raw = dumps({"cwd": "/Users/mike/apps/harness-worktrees/feat/send"})

    out = rewrite_transcript(raw, HARNESS_LAYOUT)

    assert residual_origin_paths(out, HARNESS_LAYOUT) == ["/Users/mike/apps/harness"]


def test_rewrites_the_real_cwd_while_leaving_its_sibling_alone() -> None:
    raw = "\n".join(
        [
            dumps({"cwd": "/Users/mike/apps/harness"}),
            dumps({"cwd": "/Users/mike/apps/harness-worktrees/feat/send"}),
            dumps({"toolUseResult": "read /Users/mike/apps/harness/src/main.py"}),
        ]
    )

    out = rewrite_transcript(raw, HARNESS_LAYOUT)

    assert out.splitlines() == [
        dumps({"cwd": "/private/tmp/dest-worktrees/feat/send"}),
        dumps({"cwd": "/Users/mike/apps/harness-worktrees/feat/send"}),
        dumps(
            {"toolUseResult": "read /private/tmp/dest-worktrees/feat/send/src/main.py"}
        ),
    ]


def test_leaves_a_home_directory_that_only_extends_the_origin_home() -> None:
    raw = dumps({"path": "/Users/mikey/notes.txt"})

    assert rewrite_transcript(raw, CROSS_MACHINE) == raw


def test_falls_through_to_the_home_pair_when_the_cwd_pair_fails_its_guard() -> None:
    out = rewrite_transcript(
        dumps({"path": "/Users/mike/projector/a.py"}), CROSS_MACHINE
    )

    assert out == dumps({"path": "/Users/alice/projector/a.py"})


def test_rewrites_the_encoded_slug_only_as_a_complete_component() -> None:
    raw = dumps(
        {
            "own": "~/.claude/projects/-Users-mike-proj/abc.jsonl",
            "sibling": "~/.claude/projects/-Users-mike-proj-worktrees-feat-send/d.jsonl",
        }
    )

    out = rewrite_transcript(raw, CROSS_MACHINE)

    assert "projects/-Users-alice-work-proj/abc.jsonl" in out
    assert "projects/-Users-mike-proj-worktrees-feat-send/d.jsonl" in out


def test_foreign_cwds_is_empty_when_every_record_reaches_the_target() -> None:
    assert foreign_cwds(dumps({"cwd": "/Users/mike/proj"}), CROSS_MACHINE) == {}


def test_foreign_cwds_catches_what_residual_paths_cannot_see() -> None:
    """The failure that reported success: a cwd the spec was not keyed on
    declines the cwd pair, falls through to the home pair, and lands as a
    well-formed path on this machine that isn't there. The origin marker is
    consumed on the way, so residual_origin_paths comes back clean."""
    raw = "\n".join(
        [
            dumps({"cwd": "/Users/mike/proj"}),
            dumps({"cwd": "/Users/mike/other"}),
            dumps({"cwd": "/Users/mike/other"}),
        ]
    )

    out = rewrite_transcript(raw, CROSS_MACHINE)

    assert "/Users/alice/other" in out
    assert residual_origin_paths(out, CROSS_MACHINE) == []
    assert foreign_cwds(raw, CROSS_MACHINE) == {
        "/Users/mike/other": ForeignCwd(count=2, restored="/Users/alice/other")
    }


def test_foreign_cwds_keys_on_the_origin_rather_than_a_reversal() -> None:
    """The origin path is the left-hand side of any mapping a caller states,
    and it survives losslessly only before the rewrite. Recovering it from the
    restored value would mean assuming a leading target home came from the
    origin's — right most of the time, silently wrong otherwise, which is the
    failure this branch exists to stop."""
    raw = dumps({"cwd": "/Users/mike/other"})

    (origin,) = foreign_cwds(raw, CROSS_MACHINE)

    assert origin == "/Users/mike/other"
    assert origin in raw
    assert origin not in rewrite_transcript(raw, CROSS_MACHINE)


def test_foreign_cwds_ignores_a_cwd_nested_in_tool_output() -> None:
    """`"cwd"` also appears inside tool results — lochy's own --json output
    among them — where it names some other process's directory. Counting those
    would fire on a restore that was entirely correct."""
    raw = dumps(
        {
            "cwd": "/Users/mike/proj",
            "toolUseResult": dumps({"cwd": "/somewhere/else"}),
        }
    )

    assert foreign_cwds(raw, CROSS_MACHINE) == {}


def test_still_rewrites_a_path_at_every_ordinary_terminator() -> None:
    for terminator in ('"', "/", "\\", " ", ":", ",", "\n", ")", "'", ""):
        raw = f"/Users/mike/proj{terminator}"

        assert rewrite_transcript(raw, CROSS_MACHINE) == (
            f"/Users/alice/work/proj{terminator}"
        )
