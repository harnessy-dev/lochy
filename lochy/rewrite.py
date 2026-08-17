import re
from dataclasses import dataclass

from .claude import encode_path, transcript_cwds


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
    result means the restored session will reference files that don't exist on
    this machine.

    Necessary and nowhere near sufficient — see foreign_cwds. This only catches
    a path that still carries an origin marker, and the more dangerous failure
    consumes the marker on its way to being wrong.
    """
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


@dataclass(frozen=True)
class ForeignCwd:
    count: int
    restored: str


def foreign_cwds(raw: str, spec: RewriteSpec) -> dict[str, ForeignCwd]:
    """Directories a transcript works in that no rewrite into `target_cwd` can
    satisfy, keyed by their path on the *origin* machine, with the number of
    records naming each and the string they end up as here.

    This is the check residual_origin_paths structurally cannot make. A cwd the
    spec was not keyed on declines the cwd pair and falls through to the home
    pair, which turns it into a well-formed path on this machine that simply
    isn't there. The origin marker is consumed on the way, so a substring
    search for it comes back clean — the restore reports success while every
    one of those records points at nothing.

    Takes the transcript *before* rewriting, which is the only place the origin
    path still exists losslessly. Recovering it from the restored value would
    mean reversing the home substitution, and that reversal is a guess: a
    leading target home may have been rewritten from the origin's or may have
    been there all along, with no way to tell after the fact. Guessing is the
    failure this module exists to stop, so the caller is handed the real string
    instead. `restored` is derived by putting each cwd through the same spec,
    so it is what the file on disk actually says.

    Non-empty does not always mean a bug: a session that genuinely moved
    between directories has records only one of which any single target cwd can
    satisfy. It always means the restored transcript is partly wrong about the
    filesystem, which is what a caller needs to know.
    """
    found: dict[str, ForeignCwd] = {}
    for cwd, count in transcript_cwds(raw).items():
        restored = rewrite_transcript(cwd, spec)
        if restored != spec.target_cwd:
            found[cwd] = ForeignCwd(count=count, restored=restored)
    return found
