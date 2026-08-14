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


def _apply_replacements(text: str, pairs: list[tuple[str, str]]) -> str:
    """Single left-to-right pass so a substitution's output can never be
    re-matched by a later pair. Longest patterns win, which keeps a cwd
    nested under a home directory from being half-rewritten by the home
    pair."""
    active = [(source, dest) for source, dest in pairs if source and source != dest]
    if not active:
        return text

    ordered = sorted(active, key=lambda pair: len(pair[0]), reverse=True)
    lookup = dict(ordered)
    pattern = re.compile("|".join(re.escape(source) for source, _ in ordered))
    return pattern.sub(lambda match: lookup[match.group(0)], text)


def rewrite_transcript(raw: str, spec: RewriteSpec) -> str:
    pairs = [
        (spec.origin_cwd, spec.target_cwd),
        (spec.origin_home, spec.target_home),
        (encode_path(spec.origin_cwd), encode_path(spec.target_cwd)),
    ]

    if spec.origin_session_id and spec.target_session_id:
        pairs.append((spec.origin_session_id, spec.target_session_id))

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
