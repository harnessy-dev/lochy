import json

from lochy.rewrite import RewriteSpec, residual_origin_paths, rewrite_transcript

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
                {"type": "assistant", "toolUseResult": "read /Users/mike/proj/src/a.ts"}
            ),
        ]
    )

    out = rewrite_transcript(raw, CROSS_MACHINE)

    assert '"cwd":"/Users/alice/work/proj"' in out
    assert "/Users/alice/work/proj/src/a.ts" in out
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
        "stale /Users/mike/elsewhere/file.ts", CROSS_MACHINE
    ) == ["/Users/mike"]
