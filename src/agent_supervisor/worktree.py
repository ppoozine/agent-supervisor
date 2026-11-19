"""git worktree 與 branch 管理。所有 `git` 指令都集中在這個模組。

每個任務一個獨立的 worktree 與 branch，從遠端的預設分支切出來，
所以不受本機預設分支是否更新影響，也不會動到使用者自己的工作目錄。
"""

import asyncio
import re
import weakref
from pathlib import Path

from agent_supervisor.process import run_command

REMOTE = "origin"
BRANCH_PREFIX = "asv/task-"
MAX_BRANCH_LENGTH = 60
GITHUB_URL = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$")


# 每個事件迴圈一把鎖。同一個 repo 的各個 worktree 共用同一份 git 資料，
# 兩個 fetch 同時更新同一個遠端分支會互搶鎖，其中一個會失敗。
_fetch_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


class GitError(RuntimeError):
    """git 指令失敗。"""


async def fetch(path: Path, branch: str) -> None:
    """更新遠端的一個分支。同一時間只進行一個 fetch。"""
    async with _fetch_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock()):
        await _git(path, "fetch", REMOTE, branch)


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
    await fetch(repo_path, base_branch)
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


async def start_merge(worktree_path: Path, base_branch: str) -> list[str]:
    """把遠端最新的基底分支合併進目前的 branch，不 commit；回傳有衝突的檔案。

    有衝突時 git 會以非零結束，這是預期的結果：工作目錄停在合併到一半的狀態，
    衝突的檔案裡留著標記，等人或 agent 解掉之後再 commit。
    沒有衝突時回傳空清單（合併的結果已經放進暫存區，或本來就不需要合併）。
    其他失敗（例如工作目錄有未 commit 的變更擋住）拋出 `GitError`，並還原到合併之前。
    """
    await fetch(worktree_path, base_branch)
    arguments = ["git", "merge", "--no-commit", "--no-ff", f"{REMOTE}/{base_branch}"]
    result = await run_command(arguments, cwd=worktree_path)
    conflicted = await unmerged_files(worktree_path)
    if result.returncode != 0 and not conflicted:
        if await is_merge_in_progress(worktree_path):
            await abort_merge(worktree_path)
        detail = (result.stderr or result.stdout).strip()
        raise GitError(f"git merge {REMOTE}/{base_branch} failed: {detail}")
    return conflicted


async def is_merge_in_progress(worktree_path: Path) -> bool:
    """工作目錄是否停在合併到一半的狀態。"""
    result = await run_command(
        ["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"], cwd=worktree_path
    )
    return result.returncode == 0


async def unmerged_files(worktree_path: Path) -> list[str]:
    """合併後還沒標成已解決的檔案。"""
    output = await _git(worktree_path, "diff", "--name-only", "--diff-filter=U")
    return [line for line in output.splitlines() if line]


async def files_with_conflict_markers(worktree_path: Path) -> list[str]:
    """還沒標成已解決、而且內容裡還留著衝突標記的檔案。

    只看 git 認定有衝突的檔案，所以內容剛好長得像標記的其他檔案不會被誤判。
    """
    files = await unmerged_files(worktree_path)
    return [name for name in files if await asyncio.to_thread(_has_markers, worktree_path / name)]


def _has_markers(path: Path) -> bool:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        # 例如其中一邊把檔案刪掉了：沒有內容可以檢查。
        return False
    return any(line.startswith(("<<<<<<< ", ">>>>>>> ")) for line in text.splitlines())


async def abort_merge(worktree_path: Path) -> None:
    """放棄合併到一半的狀態，回到合併之前。"""
    await _git(worktree_path, "merge", "--abort")


async def diff_against_base(repo_path: Path, base_branch: str, branch: str) -> str:
    """branch 從基底分支分出來之後的所有變更，也就是 PR 的 Files changed 會顯示的內容。

    在專案的 repo 裡執行而不是在 worktree 裡：封存之後 worktree 不在了，但 branch 還在。
    只看已經 commit 的內容。用三個點的寫法，所以基底分支後來的 commit 不會算進來。
    """
    return await _git(
        repo_path, "diff", "--no-color", "--no-ext-diff", f"{REMOTE}/{base_branch}...{branch}", "--"
    )


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


async def ensure_repository(repo_path: Path) -> None:
    """確認路徑是一個 git repo 工作目錄的最上層，不是的話拋出 `GitError`。

    子目錄、`.git` 裡面、bare repo 都不算：同一個 repo 只能用一個路徑登記。
    """
    if not await asyncio.to_thread(repo_path.is_dir):
        raise GitError(f"{repo_path} is not a directory")
    top_level = Path(await _git(repo_path, "rev-parse", "--show-toplevel"))
    if not await asyncio.to_thread(_is_same_directory, top_level, repo_path):
        raise GitError(f"{repo_path} is inside the repository at {top_level}; use that path")


def _is_same_directory(first: Path, second: Path) -> bool:
    return first.resolve() == second.resolve()


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
