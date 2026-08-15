import subprocess
from pathlib import Path

from lochy.git import current_branch


def test_current_branch_reads_the_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "feature/foo", str(repo)],
        check=True,
        capture_output=True,
    )
    assert current_branch(str(repo)) == "feature/foo"


def test_current_branch_is_none_outside_a_repo(tmp_path: Path) -> None:
    assert current_branch(str(tmp_path)) is None
