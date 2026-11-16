"""git worktree 與 branch 管理。所有 `git` 指令都集中在這個模組。

每個任務一個獨立的 worktree 與 branch，從遠端的預設分支切出來，
所以不受本機預設分支是否更新影響，也不會動到使用者自己的工作目錄。
"""

import re
from pathlib import Path

from agent_supervisor.process import run_command

REMOTE = "origin"
BRANCH_PREFIX = "asv/task-"
MAX_BRANCH_LENGTH = 60
GITHUB_URL = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$")


class GitError(RuntimeError):
    """git 指令失敗。"""


def branch_name(task_id: int, title: str) -> str:
    """由任務編號與標題組出 branch 名稱；標題只取英數字，沒有就只用編號。"""
    words = re.findall(r"[a-z0-9]+", title.lower())
    name = f"{BRANCH_PREFIX}{task_id}"
    for word in words:
        if len(name) + 1 + len(word) > MAX_BRANCH_LENGTH:
            break
        name = f"{name}-{word}"
    return name


async def create(repo_path: Path, worktree_path: Path, *, branch: str, base_branch: str) -> None:
    """先更新遠端的基底分支，再從它建立新的 branch 與 worktree。"""
    await _git(repo_path, "fetch", REMOTE, base_branch)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    await _git(
        repo_path, "worktree", "add", "-b", branch, str(worktree_path), f"{REMOTE}/{base_branch}"
    )


async def remove(repo_path: Path, worktree_path: Path) -> None:
    """移除 worktree 的目錄；branch 保留。"""
    await _git(repo_path, "worktree", "remove", "--force", str(worktree_path))


async def has_uncommitted_changes(worktree_path: Path) -> bool:
    """工作目錄是否有還沒 commit 的變更（含未追蹤的檔案）。"""
    return bool(await _git(worktree_path, "status", "--porcelain"))


async def commit_all(worktree_path: Path, message: str) -> str:
    """把所有變更 commit 起來，回傳新的 commit。"""
    await _git(worktree_path, "add", "-A")
    await _git(worktree_path, "commit", "-m", message)
    return await head_sha(worktree_path)


async def head_sha(worktree_path: Path) -> str:
    """目前的 commit。"""
    return await _git(worktree_path, "rev-parse", "HEAD")


async def count_commits_ahead(worktree_path: Path, base_branch: str) -> int:
    """目前的 branch 比遠端基底分支多幾個 commit。"""
    return int(await _git(worktree_path, "rev-list", "--count", f"{REMOTE}/{base_branch}..HEAD"))


async def has_unpushed_commits(worktree_path: Path, branch: str) -> bool:
    """是否有遠端還沒有的 commit。"""
    remote_ref = f"refs/remotes/{REMOTE}/{branch}"
    known = await run_command(
        ["git", "rev-parse", "--verify", "--quiet", remote_ref], cwd=worktree_path
    )
    exclusion = [f"^{remote_ref}"] if known.returncode == 0 else ["--not", f"--remotes={REMOTE}"]
    return int(await _git(worktree_path, "rev-list", "--count", "HEAD", *exclusion)) > 0


async def push(worktree_path: Path, branch: str) -> None:
    """把 branch 推到遠端。"""
    await _git(worktree_path, "push", "-u", REMOTE, branch)


async def detect_github_repo(repo_path: Path) -> str | None:
    """從 origin 的網址讀出 `owner/name`；不是 GitHub 的網址則回傳 None。"""
    result = await run_command(["git", "remote", "get-url", REMOTE], cwd=repo_path)
    match = GITHUB_URL.search(result.stdout.strip()) if result.returncode == 0 else None
    return None if match is None else f"{match['owner']}/{match['name']}"


async def detect_default_branch(repo_path: Path) -> str:
    """讀出遠端的預設分支；讀不到時假設是 main。"""
    result = await run_command(
        ["git", "symbolic-ref", "--short", f"refs/remotes/{REMOTE}/HEAD"], cwd=repo_path
    )
    if result.returncode != 0:
        return "main"
    return result.stdout.strip().removeprefix(f"{REMOTE}/")


async def _git(cwd: Path, *args: str) -> str:
    result = await run_command(["git", *args], cwd=cwd)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()
