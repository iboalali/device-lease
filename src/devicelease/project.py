import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Project:
    root: str
    """Main repository root; worktrees map back to it so policy follows the repository."""
    checkout: str
    """Top level of the current checkout, where a repo's `.devicelease.json` is read from."""


def resolve_project(cwd: str | None = None) -> Project:
    cwd = os.path.realpath(cwd or os.getcwd())
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute",
             "--show-toplevel", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Project(cwd, cwd)
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) < 2:
        return Project(cwd, cwd)
    checkout, common = lines[0], lines[1]
    if os.path.basename(common) == ".git":
        return Project(os.path.dirname(common), checkout)
    return Project(checkout, checkout)
