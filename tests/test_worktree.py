"""Tests for worktree.py. Uses real git with a local bare repo as the remote; no network."""

import asyncio
from pathlib import Path

import pytest

from agent_supervisor import worktree
from tests.conftest import GitRepo


async def make_worktree(repo: GitRepo, tmp_path: Path, name: str = "task-1") -> Path:
    path = tmp_path / "worktrees" / name
    await worktree.create(repo.path, path, branch=f"asv/{name}", base_branch="main")
    return path


async def test_create_makes_a_branch_and_directory_from_the_remote_base(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await make_worktree(git_repo, tmp_path)

    assert (path / "README.md").read_text() == "demo\n"
    assert git_repo.git("rev-parse", "--abbrev-ref", "HEAD", cwd=path) == "asv/task-1"
    assert git_repo.git("rev-parse", "HEAD", cwd=path) == git_repo.git("rev-parse", "origin/main")


async def test_create_uses_the_remote_base_even_when_local_main_is_behind(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    other = tmp_path / "other-clone"
    git_repo.git("clone", "-q", str(git_repo.origin), str(other), cwd=tmp_path)
    git_repo.git("config", "user.email", "t@example.com", cwd=other)
    git_repo.git("config", "user.name", "T", cwd=other)
    (other / "new.txt").write_text("from elsewhere\n")
    git_repo.git("add", "new.txt", cwd=other)
    git_repo.git("commit", "-q", "-m", "remote change", cwd=other)
    git_repo.git("push", "-q", "origin", "main", cwd=other)

    path = await make_worktree(git_repo, tmp_path)

    assert (path / "new.txt").exists()
    assert not (git_repo.path / "new.txt").exists()


async def test_two_worktrees_do_not_affect_each_other_or_the_main_checkout(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    first = await make_worktree(git_repo, tmp_path, "task-1")
    second = await make_worktree(git_repo, tmp_path, "task-2")

    (first / "only-first.txt").write_text("x")

    assert not (second / "only-first.txt").exists()
    assert not (git_repo.path / "only-first.txt").exists()
    assert git_repo.git("status", "--porcelain") == ""


async def test_create_fails_with_a_clear_error_when_branch_exists(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    await make_worktree(git_repo, tmp_path, "task-1")

    with pytest.raises(worktree.GitError, match="asv/task-1"):
        await worktree.create(
            git_repo.path, tmp_path / "elsewhere", branch="asv/task-1", base_branch="main"
        )


async def test_uncommitted_changes_are_detected_including_untracked_files(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await make_worktree(git_repo, tmp_path)
    clean = await worktree.has_uncommitted_changes(path)

    (path / "untracked.txt").write_text("x")

    assert clean is False
    assert await worktree.has_uncommitted_changes(path) is True


async def test_commit_all_commits_tracked_and_untracked_changes(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await make_worktree(git_repo, tmp_path)
    (path / "untracked.txt").write_text("x")
    (path / "README.md").write_text("changed\n")

    sha = await worktree.commit_all(path, "chore(agent): save changes")

    assert await worktree.has_uncommitted_changes(path) is False
    assert sha == git_repo.git("rev-parse", "HEAD", cwd=path)
    assert git_repo.git("log", "-1", "--format=%s", cwd=path) == "chore(agent): save changes"


async def test_commits_ahead_counts_commits_not_on_the_remote_base(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await make_worktree(git_repo, tmp_path)
    before = await worktree.count_commits_ahead(path, "main")

    (path / "a.txt").write_text("x")
    await worktree.commit_all(path, "one")

    assert before == 0
    assert await worktree.count_commits_ahead(path, "main") == 1


async def test_push_publishes_the_branch_and_clears_unpushed_state(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await make_worktree(git_repo, tmp_path)
    (path / "a.txt").write_text("x")
    sha = await worktree.commit_all(path, "one")
    unpushed_before = await worktree.has_unpushed_commits(path, "asv/task-1")

    await worktree.push(path, "asv/task-1")

    assert unpushed_before is True
    assert await worktree.has_unpushed_commits(path, "asv/task-1") is False
    assert git_repo.git("rev-parse", "asv/task-1", cwd=git_repo.origin) == sha


async def test_branch_without_new_commits_has_nothing_to_push(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await make_worktree(git_repo, tmp_path)

    assert await worktree.has_unpushed_commits(path, "asv/task-1") is False


async def test_second_commit_after_push_is_unpushed_again(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await make_worktree(git_repo, tmp_path)
    (path / "a.txt").write_text("x")
    await worktree.commit_all(path, "one")
    await worktree.push(path, "asv/task-1")

    (path / "b.txt").write_text("y")
    await worktree.commit_all(path, "two")

    assert await worktree.has_unpushed_commits(path, "asv/task-1") is True


async def test_remove_deletes_the_directory_but_keeps_the_branch(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await make_worktree(git_repo, tmp_path)

    await worktree.remove(git_repo.path, path)

    assert not path.exists()
    assert "asv/task-1" in git_repo.git("branch", "--list", "asv/task-1")


async def test_remote_details_are_detected_from_origin(git_repo: GitRepo) -> None:
    git_repo.git("remote", "set-url", "origin", "git@github.com:someone/some-repo.git")
    ssh = await worktree.detect_github_repo(git_repo.path)
    git_repo.git("remote", "set-url", "origin", "https://github.com/someone/some-repo")
    https = await worktree.detect_github_repo(git_repo.path)
    git_repo.git("remote", "set-url", "origin", str(git_repo.origin))

    assert ssh == "someone/some-repo"
    assert https == "someone/some-repo"
    assert await worktree.detect_github_repo(git_repo.path) is None


def test_branch_name_uses_task_id_and_ascii_title_words() -> None:
    assert worktree.branch_name(7, "Fix flaky NewTaskDialog test!") == (
        "asv/task-7-fix-flaky-newtaskdialog-test"
    )
    assert worktree.branch_name(8, "修正測試") == "asv/task-8"
    assert len(worktree.branch_name(9, "word " * 40)) <= 60


async def test_ensure_repository_accepts_a_repo_and_rejects_anything_else(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    inside = git_repo.path / "nested"
    inside.mkdir()

    await worktree.ensure_repository(git_repo.path)

    with pytest.raises(worktree.GitError, match="not a git repository"):
        await worktree.ensure_repository(plain)
    with pytest.raises(worktree.GitError, match="is not a directory"):
        await worktree.ensure_repository(tmp_path / "missing")
    with pytest.raises(worktree.GitError, match="is inside the repository"):
        await worktree.ensure_repository(inside)
    with pytest.raises(worktree.GitError):
        await worktree.ensure_repository(git_repo.path / ".git")


async def conflicting_worktree(repo: GitRepo, tmp_path: Path) -> Path:
    """A worktree that changed the same README line as origin's main."""
    path = await make_worktree(repo, tmp_path)
    (path / "README.md").write_text("from the task\n")
    await worktree.commit_all(path, "task change")
    repo.push_to_main("README.md", "from main\n")
    return path


async def test_merge_with_a_conflict_stops_and_reports_the_files(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await conflicting_worktree(git_repo, tmp_path)

    conflicted = await worktree.start_merge(path, "main")

    assert conflicted == ["README.md"]
    assert await worktree.is_merge_in_progress(path)
    assert await worktree.files_with_conflict_markers(path) == ["README.md"]
    assert "<<<<<<< " in (path / "README.md").read_text()


async def test_resolved_file_no_longer_counts_as_having_markers(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await conflicting_worktree(git_repo, tmp_path)
    await worktree.start_merge(path, "main")

    (path / "README.md").write_text("from the task and from main\n")

    assert await worktree.files_with_conflict_markers(path) == []
    merge_commit = await worktree.commit_all(path, "merge main")
    assert len(git_repo.git("rev-list", "--parents", "-1", merge_commit, cwd=path).split()) == 3
    assert not await worktree.is_merge_in_progress(path)


async def test_merge_without_a_conflict_reports_nothing(git_repo: GitRepo, tmp_path: Path) -> None:
    path = await make_worktree(git_repo, tmp_path)
    (path / "feature.txt").write_text("new\n")
    await worktree.commit_all(path, "task change")
    git_repo.push_to_main("README.md", "from main\n")

    assert await worktree.start_merge(path, "main") == []
    assert await worktree.files_with_conflict_markers(path) == []


async def test_merge_when_already_up_to_date_reports_nothing(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await make_worktree(git_repo, tmp_path)

    assert await worktree.start_merge(path, "main") == []
    assert not await worktree.is_merge_in_progress(path)


async def test_merge_blocked_by_local_changes_fails_and_leaves_them_alone(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await conflicting_worktree(git_repo, tmp_path)
    (path / "README.md").write_text("not committed yet\n")

    with pytest.raises(worktree.GitError, match="git merge origin/main failed"):
        await worktree.start_merge(path, "main")

    assert (path / "README.md").read_text() == "not committed yet\n"
    assert not await worktree.is_merge_in_progress(path)


async def test_aborting_a_merge_restores_the_branch(git_repo: GitRepo, tmp_path: Path) -> None:
    path = await conflicting_worktree(git_repo, tmp_path)
    await worktree.start_merge(path, "main")

    await worktree.abort_merge(path)

    assert (path / "README.md").read_text() == "from the task\n"
    assert not await worktree.has_uncommitted_changes(path)


async def test_tasks_started_at_the_same_time_do_not_fight_over_the_fetch(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    """Parallel fetches of a remote branch race for a lock; visible only with new remote commits."""
    other = tmp_path / "other-clone"
    git_repo.git("clone", "-q", str(git_repo.origin), str(other), cwd=tmp_path)
    git_repo.git("config", "user.email", "t@example.com", cwd=other)
    git_repo.git("config", "user.name", "T", cwd=other)
    (other / "new.txt").write_text("from elsewhere\n")
    git_repo.git("add", "new.txt", cwd=other)
    git_repo.git("commit", "-q", "-m", "remote change", cwd=other)
    git_repo.git("push", "-q", "origin", "main", cwd=other)

    paths = await asyncio.gather(
        *(make_worktree(git_repo, tmp_path, f"task-{number}") for number in range(1, 5))
    )

    assert all((path / "new.txt").exists() for path in paths)


async def test_diff_shows_only_what_the_branch_changed(git_repo: GitRepo, tmp_path: Path) -> None:
    path = await make_worktree(git_repo, tmp_path)
    (path / "feature.txt").write_text("new\n")
    await worktree.commit_all(path, "task change")
    # Later commits on the base branch are not this task's changes and must not appear in the
    # diff.
    git_repo.push_to_main("README.md", "from main\n")
    await worktree.fetch(git_repo.path, "main")

    diff = await worktree.diff_against_base(git_repo.path, "main", "asv/task-1")

    assert "+++ b/feature.txt" in diff and "+new" in diff
    assert "README.md" not in diff


async def test_diff_ignores_changes_that_are_not_committed(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await make_worktree(git_repo, tmp_path)
    (path / "draft.txt").write_text("not committed\n")

    assert await worktree.diff_against_base(git_repo.path, "main", "asv/task-1") == ""


async def test_diff_is_still_available_after_the_worktree_is_removed(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    path = await make_worktree(git_repo, tmp_path)
    (path / "feature.txt").write_text("new\n")
    await worktree.commit_all(path, "task change")
    await worktree.remove(git_repo.path, path)

    assert "+new" in await worktree.diff_against_base(git_repo.path, "main", "asv/task-1")
