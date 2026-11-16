"""共用的測試 fixture。"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass(frozen=True)
class GitRepo:
    """一個暫存的 git repo，以及當作 `origin` 的本機 bare repo。"""

    path: Path
    origin: Path

    def git(self, *args: str, cwd: Path | None = None) -> str:
        """在 repo（或指定目錄）執行 git，回傳去掉前後空白的輸出。"""
        completed = subprocess.run(
            ["git", *args], cwd=cwd or self.path, capture_output=True, text=True, check=True
        )
        return completed.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    """建立一個有一筆 commit、已 push 到本機 origin 的 repo。"""
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
