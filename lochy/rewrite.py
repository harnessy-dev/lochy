import re
from dataclasses import dataclass

from .claude import encode_path


@dataclass(frozen=True)
class RewriteSpec:
    origin_cwd: str
    origin_home: str
    target_cwd: str
    target_home: str
    origin_session_id: str | None = None
    target_session_id: str | None = None


# A path only matches when it ends on a component boundary, so a sibling
# directory that merely extends the origin's last component is left alone.
# The guard rejects any character that could continue a filename and allows
# everything else, because a transcript surrounds paths with `"`, `\`, `:`,
# `,`, spaces and newlines as often as with `/`. Erring strict is safe: an
# unsubstituted path shows up in residual_origin_paths, while an over-eager
# one corrupts the text silently.
_PATH_BOUNDARY = r"(?![A-Za-z0-9._-])"

# encode_path maps every non-alphanumeric to `-`, so `<repo>/sub` and
# `<repo>-sub` encode identically and no lookahead can tell them apart.
# The slug is therefore only rewritten as a whole path component, which
# declines to touch `<encoded-repo>-worktrees-...`; guessing there is what
# produced the corruption this guard exists to prevent.
_ENCODED_COMPONENT_BOUNDARY = r"(?![A-Za-z0-9-])"

# The session id is a UUID, not a path: it appears bare in filenames and in
# `"sessionId":"..."` values, where no boundary rule holds.
_UNGUARDED = ""


def _apply_replacements(text: str, pairs: list[tuple[str, str, str]]) -> str:
    """Single left-to-right pass so a substitution's output can never be
    re-matched by a later pair. Longest patterns win, which keeps a cwd
    nested under a home directory from being half-rewritten by the home
    pair. Each alternative carries its own zero-width boundary guard, so a
    longer pattern that fails its guard falls through to a shorter one."""
    active = [
        (source, dest, guard)
        for source, dest, guard in pairs
        if source and source != dest
    ]
    if not active:
        return text

    ordered = sorted(active, key=lambda pair: len(pair[0]), reverse=True)
    lookup = {source: dest for source, dest, _ in ordered}
    pattern = re.compile(
        "|".join(re.escape(source) + guard for source, _, guard in ordered)
    )
    return pattern.sub(lambda match: lookup[match.group(0)], text)


def rewrite_transcript(raw: str, spec: RewriteSpec) -> str:
    pairs = [
        (spec.origin_cwd, spec.target_cwd, _PATH_BOUNDARY),
        (spec.origin_home, spec.target_home, _PATH_BOUNDARY),
        (
            encode_path(spec.origin_cwd),
            encode_path(spec.target_cwd),
            _ENCODED_COMPONENT_BOUNDARY,
        ),
    ]

    if spec.origin_session_id and spec.target_session_id:
        pairs.append((spec.origin_session_id, spec.target_session_id, _UNGUARDED))

    return _apply_replacements(raw, pairs)


def residual_origin_paths(text: str, spec: RewriteSpec) -> list[str]:
    """Occurrences of the origin's paths that survived a rewrite. A non-empty
    result means the restored session will reference files that don't exist
    on this machine."""
    found: dict[str, None] = {}
    for needle in (spec.origin_cwd, spec.origin_home):
        if (
            needle
            and needle != spec.target_cwd
            and needle != spec.target_home
            and needle in text
        ):
            found[needle] = None
    return list(found)
