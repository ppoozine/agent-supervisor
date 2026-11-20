"""Shared test fixtures."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass(frozen=True)
class GitRepo:
    """A temporary git repo, plus a local bare repo acting as `origin`."""

    path: Path
    origin: Path

    def git(self, *args: str, cwd: Path | None = None) -> str:
        """Run git in the repo (or a given directory) and return the stripped output."""
        completed = subprocess.run(
            ["git", *args], cwd=cwd or self.path, capture_output=True, text=True, check=True
        )
        return completed.stdout.strip()

    def push_to_main(self, name: str, content: str) -> None:
        """Change a file in the main working directory, commit, and push to origin's main."""
        (self.path / name).write_text(content)
        self.git("add", name)
        self.git("commit", "-q", "-m", f"change {name} on main")
        self.git("push", "-q", "origin", "main")


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    """Create a repo with one commit, already pushed to the local origin."""
    origin = tmp_path / "origin.git"
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    repo = GitRepo(path=path, origin=origin)
    repo.git("config", "user.email", "test@example.com")
    repo.git("config", "user.name", "Test")
    repo.git("config", "commit.gpgsign", "false")
    (path / "README.md").write_text("demo\n")
    repo.git("add", "README.md")
    repo.git("commit", "-q", "-m", "initial")
    repo.git("remote", "add", "origin", str(origin))
    repo.git("push", "-q", "-u", "origin", "main")
    return repo
