import subprocess


def current_branch(cwd: str) -> str | None:
    """None covers both a detached HEAD and a directory outside any repo —
    the same two cases that leave gitBranch unset on a session."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() or None
