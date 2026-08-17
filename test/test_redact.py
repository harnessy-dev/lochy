import json

import pytest

from lochy.redact import RULES, RedactionError, Rule, redact, scan, summarize

# Structurally valid, obviously synthetic. Nothing here is or was a credential.
FAKE = {
    "aws-access-key": "AKIAZZZZZZZZEXAMPLE0",
    "github-token": "ghp_" + "A0" * 20,
    # Split, and with non-numeric segments, so the literal in this file does
    # not match GitHub's push-protection pattern for a real Slack token.
    "slack-token": "xoxb-" + "EXAMPLE-NOT-A-REAL-TOKEN-" + "Z" * 12,
    "stripe-key": "sk_live_" + "0" * 24,
    "anthropic-key": "sk-ant-api03-" + "x" * 24,
    "openai-key": "sk-proj-" + "y" * 24,
    "google-api-key": "AIza" + "B" * 35,
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.c2lnbmF0dXJl",
    "private-key-block": (
        "-----BEGIN RSA PRIVATE KEY-----\\nMIIBOgIBAAJBAKfake\\n"
        "-----END RSA PRIVATE KEY-----"
    ),
}


def line(**fields: object) -> str:
    return json.dumps(fields, separators=(",", ":"))


@pytest.mark.parametrize("name", sorted(FAKE))
def test_each_rule_matches_and_removes_the_secret(name: str) -> None:
    out = redact(f"the value is {FAKE[name]} ok")

    assert out.counts == {name: 1}
    assert FAKE[name] not in out.text
    assert f"[REDACTED:{name}]" in out.text


def test_an_env_assignment_keeps_the_name_and_drops_the_value() -> None:
    out = redact("AWS_SECRET_ACCESS_KEY=wJalrFAKEfakeFAKEfakeFAKEfakeFAKEfake0000")

    assert out.text == "AWS_SECRET_ACCESS_KEY=[REDACTED:env-assignment]"
    assert out.counts == {"env-assignment": 1}


def test_an_env_assignment_is_found_inside_a_json_string_value() -> None:
    out = redact(line(GITHUB_TOKEN="ghs_notarealtokenatall000000"))

    assert json.loads(out.text) == {"GITHUB_TOKEN": "[REDACTED:env-assignment]"}


def test_redacted_lines_still_parse_as_json() -> None:
    raw = "\n".join(
        [
            line(type="user", message=f"my key is {FAKE['aws-access-key']}"),
            line(type="assistant", toolUseResult=f"PASSWORD={'p' * 20}"),
            line(type="assistant", toolUseResult=FAKE["private-key-block"]),
        ]
    )

    out = redact(raw)

    for redacted_line in out.text.splitlines():
        json.loads(redacted_line)
    assert sum(out.counts.values()) == 3


def test_a_redaction_token_is_not_itself_redacted() -> None:
    once = redact(f"AWS_SECRET_ACCESS_KEY={'z' * 40} and {FAKE['github-token']}")
    twice = redact(once.text)

    assert twice.text == once.text
    assert twice.counts == {}


def test_the_longest_match_wins_between_overlapping_prefixes() -> None:
    out = redact(FAKE["anthropic-key"])

    assert out.counts == {"anthropic-key": 1}


def test_verification_rejects_a_rule_that_leaves_its_own_match_behind() -> None:
    # A rule whose replacement still matches: the token contains "AKIA..." only
    # because the rule keeps the text it was supposed to remove.
    broken = (Rule("keeps-the-secret", r"(?P<keeps_the_secret_keep>AKIA[0-9A-Z]{16})"),)

    with pytest.raises(RedactionError, match="still match after redaction"):
        redact(FAKE["aws-access-key"], rules=broken)


def test_ordinary_transcript_content_survives_untouched() -> None:
    innocuous = "\n".join(
        [
            line(cwd="/Users/mike/Programming/personal/apps/lochy"),
            line(message="I refactored rewrite.py to keep the single-pass property."),
            line(commit="c4056e9a1f2b3c4d5e6f708192a3b4c5d6e7f809"),
            line(uuid="3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8"),
            line(note="see https://github.com/ness-dev/lochy for the source"),
            line(usage={"input_tokens": 12345678, "output_tokens": 4096}),
            line(path="~/.claude/projects/-Users-mike-proj/abc.jsonl"),
            line(sha256="a" * 64),
        ]
    )

    out = redact(innocuous)

    assert out.text == innocuous
    assert out.counts == {}


def test_a_key_prefix_landing_mid_word_is_not_a_key() -> None:
    out = redact(
        "\n".join(
            [
                line(diff=".task-container-header { flex: 1; }"),
                line(blob="PiW6ADDuAAAAAAAAAAAAAAAAAAAAACCAAAAAAAAAAAAAAAAAAAAA"),
                line(css="risk-assessment-panel-collapsed-state"),
            ]
        )
    )

    assert out.counts == {}


def test_a_short_or_path_like_value_is_not_treated_as_a_secret() -> None:
    out = redact("TOKEN: no\nSECRET_PATH=/Users/mike/.config/creds\nTOKEN_DIR=~/keys")

    assert out.counts == {}


def test_scan_reports_what_a_redacted_transcript_no_longer_contains() -> None:
    raw = f"{FAKE['jwt']} {FAKE['stripe-key']}"

    assert scan(raw) == {"jwt": 1, "stripe-key": 1}
    assert scan(redact(raw).text) == {}


def test_summarize_names_each_rule_without_quoting_the_secret() -> None:
    assert summarize({}) == ""
    assert summarize({"jwt": 1}) == "redacted 1 secret (jwt ×1)"
    assert (
        summarize({"env-assignment": 3, "aws-access-key": 1})
        == "redacted 4 secrets (aws-access-key ×1, env-assignment ×3)"
    )


def test_every_rule_has_a_token_safe_name() -> None:
    for rule in RULES:
        assert not set(rule.name) & set('"\\\n')
