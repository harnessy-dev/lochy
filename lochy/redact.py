import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: str

    @property
    def key(self) -> str:
        return self.name.replace("-", "_")


# The value of a named assignment is the only way to find a credential that
# carries no distinctive prefix (an AWS secret access key is 40 chars of
# base64). Uppercase-only keeps it off ordinary prose. The lookaheads keep it
# off filesystem paths and off a token this module already wrote, which is what
# makes a second pass over redacted text find nothing.
ENV_ASSIGNMENT = (
    r"(?P<env_assignment_keep>"
    r"[A-Z0-9_]*"
    r"(?:AWS_SECRET_ACCESS_KEY|SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)"
    r"[A-Z0-9_]*"
    r"[\"']?\s*[=:]\s*[\"']?"
    r")"
    r"(?!\[REDACTED:)(?![/~.])[^\s\"',;\\]{8,}"
)

# A PEM block is the one rule whose match spans newlines. Its body excludes the
# double quote so a BEGIN and an END in two different JSONL records can't be
# joined into one match — that would swallow the records between them.
PRIVATE_KEY_BLOCK = (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"[^\"]*?"
    r"-----END [A-Z ]*PRIVATE KEY-----"
)

# A prefixed key is only a key when the prefix starts a word. Without this,
# scanning 350MB of real transcripts turned up 631 "AWS keys" (ACCA landing
# mid-base64-blob) and 56 "OpenAI keys" (the "sk-" inside a CSS `.task-*`
# class), and zero real ones. The fixed-length keys get a closing guard too,
# since a longer run of the same characters means it isn't a key.
START = r"(?<![A-Za-z0-9_-])"
END = r"(?![A-Za-z0-9_-])"

RULES: tuple[Rule, ...] = (
    Rule("aws-access-key", rf"{START}(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{{16}}{END}"),
    Rule("github-token", rf"{START}gh[pousr]_[A-Za-z0-9]{{36,}}"),
    Rule("slack-token", rf"{START}xox[abprs]-[A-Za-z0-9-]{{10,}}"),
    Rule("stripe-key", rf"{START}sk_live_[A-Za-z0-9]{{24,}}"),
    # Before openai-key, which would otherwise claim the same "sk-" prefix.
    Rule("anthropic-key", rf"{START}sk-ant-[A-Za-z0-9_-]{{20,}}"),
    Rule("openai-key", rf"{START}sk-(?:proj-)?[A-Za-z0-9_-]{{20,}}"),
    Rule("google-api-key", rf"{START}AIza[0-9A-Za-z_-]{{35}}{END}"),
    Rule("private-key-block", PRIVATE_KEY_BLOCK),
    Rule("jwt", rf"{START}eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    Rule("env-assignment", ENV_ASSIGNMENT),
)


class RedactionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Redaction:
    text: str
    counts: dict[str, int]


@lru_cache(maxsize=None)
def _compiled(rules: tuple[Rule, ...]) -> re.Pattern[str]:
    """One alternation, applied in a single left-to-right pass, so a rule can
    never match inside the replacement an earlier rule produced."""
    return re.compile("|".join(f"(?P<{rule.key}>{rule.pattern})" for rule in rules))


def _matched_rule(match: re.Match[str], rules: tuple[Rule, ...]) -> Rule:
    for rule in rules:
        if match.group(rule.key) is not None:
            return rule
    raise RedactionError("a match belonged to no rule")


def scan(text: str, rules: tuple[Rule, ...] = RULES) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in _compiled(rules).finditer(text):
        name = _matched_rule(match, rules).name
        counts[name] = counts.get(name, 0) + 1
    return counts


def redact(text: str, rules: tuple[Rule, ...] = RULES) -> Redaction:
    counts: dict[str, int] = {}

    def substitute(match: re.Match[str]) -> str:
        rule = _matched_rule(match, rules)
        counts[rule.name] = counts.get(rule.name, 0) + 1
        kept = match.groupdict().get(f"{rule.key}_keep") or ""
        return f"{kept}[REDACTED:{rule.name}]"

    redacted = _compiled(rules).sub(substitute, text)

    leftover = scan(redacted, rules)
    if leftover:
        raise RedactionError(
            f"secrets still match after redaction: {_detail(leftover)}"
        )

    return Redaction(text=redacted, counts=counts)


def _detail(counts: dict[str, int]) -> str:
    return ", ".join(f"{name} ×{count}" for name, count in sorted(counts.items()))


def summarize(counts: dict[str, int]) -> str:
    if not counts:
        return ""
    total = sum(counts.values())
    return f"redacted {total} secret{'' if total == 1 else 's'} ({_detail(counts)})"
