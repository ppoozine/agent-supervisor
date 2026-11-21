"""git worktree and branch management. All `git` commands live in this module.

Each task gets its own worktree and branch, cut from the remote default branch, so it doesn't depend
on whether the local default branch is up to date, and never touches the user's own working
directory.
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


# One lock per event loop. Worktrees of the same repo share the same git data, and two fetches
# updating the same remote branch at once fight over its lock, so one of them fails.
_fetch_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


class GitError(RuntimeError):
    """A git command failed."""


async def fetch(path: Path, branch: str) -> None:
    """Update one remote branch. Only one fetch runs at a time."""
    async with _fetch_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock()):
        await _git(path, "fetch", REMOTE, branch)


def branch_name(task_id: int, title: str) -> str:
    """Build a branch name from the task ID and title (alphanumerics only), or just the ID."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    name = f"{BRANCH_PREFIX}{task_id}"
    for word in words:
        if len(name) + 1 + len(word) > MAX_BRANCH_LENGTH:
            break
        name = f"{name}-{word}"
    return name


async def create(repo_path: Path, worktree_path: Path, *, branch: str, base_branch: str) -> None:
    """Update the remote base branch first, then create a new branch and worktree from it.

    Also finishes a creation that was cut off earlier. The caller only creates a worktree the task
    has not recorded yet, so the agent never ran in whatever an earlier attempt left behind: a
    registered worktree at this path is removed and made again, and an existing branch is reused
    instead of failing on `-b`.
    """
    await fetch(repo_path, base_branch)
    await prune(repo_path)
    if await _is_registered(repo_path, worktree_path):
        # Forced twice: a cut-off `worktree add` leaves the worktree locked as "initializing", and
        # both prune and a single --force skip locked worktrees.
        await _git(repo_path, "worktree", "remove", "--force", "--force", str(worktree_path))
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if await _branch_exists(repo_path, branch):
        await _git(repo_path, "worktree", "add", str(worktree_path), branch)
        return
    await _git(
        repo_path, "worktree", "add", "-b", branch, str(worktree_path), f"{REMOTE}/{base_branch}"
    )


async def prune(repo_path: Path) -> None:
    """Forget worktrees whose directories no longer exist."""
    await _git(repo_path, "worktree", "prune")


async def _is_registered(repo_path: Path, worktree_path: Path) -> bool:
    listing = await _git(repo_path, "worktree", "list", "--porcelain")
    for line in listing.splitlines():
        path = Path(line.removeprefix("worktree "))
        if line.startswith("worktree ") and await asyncio.to_thread(
            _is_same_directory, path, worktree_path
        ):
            return True
    return False


async def _branch_exists(repo_path: Path, branch: str) -> bool:
    result = await run_command(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo_path
    )
    return result.returncode == 0


async def remove(repo_path: Path, worktree_path: Path) -> None:
    """Remove the worktree's directory; the branch is kept."""
    await _git(repo_path, "worktree", "remove", "--force", str(worktree_path))


async def has_uncommitted_changes(worktree_path: Path) -> bool:
    """Whether the working directory has uncommitted changes (including untracked files)."""
    return bool(await _git(worktree_path, "status", "--porcelain"))


async def commit_all(worktree_path: Path, message: str) -> str:
    """Commit all changes and return the new commit."""
    await _git(worktree_path, "add", "-A")
    await _git(worktree_path, "commit", "-m", message)
    return await head_sha(worktree_path)


async def start_merge(worktree_path: Path, base_branch: str) -> list[str]:
    """Merge the latest remote base branch into the current branch without committing.

    Return the conflicted files. With conflicts git exits non-zero, which is expected: the working
    directory stays mid-merge, with markers left in the conflicted files, to be committed after a
    person or the agent resolves them.
    With no conflicts, return an empty list (the merge result is already staged, or no merge was
    needed).
    Other failures (e.g. uncommitted changes in the working directory get in the way) raise
    `GitError` and roll back to before the merge.
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
    """Whether the working directory is in the middle of a merge."""
    result = await run_command(
        ["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"], cwd=worktree_path
    )
    return result.returncode == 0


async def unmerged_files(worktree_path: Path) -> list[str]:
    """Files not yet marked resolved after the merge."""
    output = await _git(worktree_path, "diff", "--name-only", "--diff-filter=U")
    return [line for line in output.splitlines() if line]


async def files_with_conflict_markers(worktree_path: Path) -> list[str]:
    """Files not yet marked resolved whose content still contains conflict markers.

    Only files git considers conflicted are checked, so other files whose content happens to look
    like markers are not misjudged.
    """
    files = await unmerged_files(worktree_path)
    return [name for name in files if await asyncio.to_thread(_has_markers, worktree_path / name)]


def _has_markers(path: Path) -> bool:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        # E.g. one side deleted the file: there is no content to check.
        return False
    return any(line.startswith(("<<<<<<< ", ">>>>>>> ")) for line in text.splitlines())


async def abort_merge(worktree_path: Path) -> None:
    """Abort the in-progress merge and return to the state before it."""
    await _git(worktree_path, "merge", "--abort")


async def diff_against_base(repo_path: Path, base_branch: str, branch: str) -> str:
    """All changes on the branch since it left the base branch: what the PR's Files changed shows.

    Runs in the project's repo rather than the worktree: after archiving the worktree is gone, but
    the branch remains.
    Only committed content is included. The three-dot form is used, so later commits on the base
    branch are not counted.
    """
    return await _git(
        repo_path, "diff", "--no-color", "--no-ext-diff", f"{REMOTE}/{base_branch}...{branch}", "--"
    )


async def head_sha(worktree_path: Path) -> str:
    """The current commit."""
    return await _git(worktree_path, "rev-parse", "HEAD")


async def count_commits_ahead(worktree_path: Path, base_branch: str) -> int:
    """How many commits the current branch is ahead of the remote base branch."""
    return int(await _git(worktree_path, "rev-list", "--count", f"{REMOTE}/{base_branch}..HEAD"))


async def has_unpushed_commits(worktree_path: Path, branch: str) -> bool:
    """Whether there are commits the remote does not have yet."""
    remote_ref = f"refs/remotes/{REMOTE}/{branch}"
    known = await run_command(
        ["git", "rev-parse", "--verify", "--quiet", remote_ref], cwd=worktree_path
    )
    exclusion = [f"^{remote_ref}"] if known.returncode == 0 else ["--not", f"--remotes={REMOTE}"]
    return int(await _git(worktree_path, "rev-list", "--count", "HEAD", *exclusion)) > 0


async def fetch_remote_branch(worktree_path: Path, branch: str) -> str | None:
    """Fetch the branch from the remote and return its latest commit; None if it is not there."""
    listing = await _git(worktree_path, "ls-remote", "--heads", REMOTE, f"refs/heads/{branch}")
    if not listing:
        return None
    await fetch(worktree_path, branch)
    return listing.split()[0]


async def is_ancestor_of_head(worktree_path: Path, commit: str) -> bool:
    """Whether the commit is HEAD or one of its ancestors."""
    result = await run_command(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"], cwd=worktree_path
    )
    if result.returncode not in (0, 1):
        detail = (result.stderr or result.stdout).strip()
        raise GitError(f"git merge-base --is-ancestor {commit} HEAD failed: {detail}")
    return result.returncode == 0


async def push(worktree_path: Path, branch: str, *, replacing: str | None = None) -> None:
    """Push the branch to the remote.

    With `replacing`, overwrite the remote branch, but only while it is still at that commit.
    """
    lease = [] if replacing is None else [f"--force-with-lease=refs/heads/{branch}:{replacing}"]
    await _git(worktree_path, "push", "-u", *lease, REMOTE, branch)


async def ensure_repository(repo_path: Path) -> None:
    """Confirm the path is the top level of a git repo working tree; raise `GitError` if not.

    Subdirectories, the inside of `.git`, and bare repos don't count: a repo can be registered under
    only one path.
    """
    if not await asyncio.to_thread(repo_path.is_dir):
        raise GitError(f"{repo_path} is not a directory")
    top_level = Path(await _git(repo_path, "rev-parse", "--show-toplevel"))
    if not await asyncio.to_thread(_is_same_directory, top_level, repo_path):
        raise GitError(f"{repo_path} is inside the repository at {top_level}; use that path")


def _is_same_directory(first: Path, second: Path) -> bool:
    return first.resolve() == second.resolve()


async def detect_github_repo(repo_path: Path) -> str | None:
    """Read `owner/name` from the origin URL; None if it is not a GitHub URL."""
    result = await run_command(["git", "remote", "get-url", REMOTE], cwd=repo_path)
    match = GITHUB_URL.search(result.stdout.strip()) if result.returncode == 0 else None
    return None if match is None else f"{match['owner']}/{match['name']}"


async def detect_default_branch(repo_path: Path) -> str:
    """Read the remote's default branch; assume main if it cannot be read."""
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
