"""Tests for stages.py: run preparation, wrap-up, and archiving. git is real; GitHub is a fake."""

import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from agent_supervisor import db, worktree
from agent_supervisor.config import Config
from agent_supervisor.enums import (
    CiState,
    EventType,
    ExitStatus,
    MergeState,
    PrState,
    ReviewState,
    RunTrigger,
)
from agent_supervisor.github import (
    FailedCheck,
    FeedbackItem,
    GitHubError,
    PullRequest,
    PullRequestStatus,
)
from agent_supervisor.runner import EventWriter
from agent_supervisor.stages import RunStages, StageError
from tests.conftest import GitRepo


def pr_status(
    head_sha: str,
    ci_state: CiState = CiState.PASSED,
    *,
    pr_state: PrState = PrState.OPEN,
    review_state: ReviewState = ReviewState.NONE,
    failed_checks: tuple[FailedCheck, ...] = (),
    feedback_ids: tuple[str, ...] = (),
    merge_state: MergeState = MergeState.CLEAN,
) -> PullRequestStatus:
    """Build a status as the Poller would find it. No conflicts by default."""
    return PullRequestStatus(
        pr_state, head_sha, ci_state, review_state, failed_checks, feedback_ids, merge_state
    )


class FakeGitHub:
    """Fake GitHub: remembers opened PRs; the next PR creation can be set to fail.

    `statuses` holds each PR's current status (keyed by PR number); `job_logs` holds each
    job's log.
    `on_status_query` is called *before* a status query returns, to simulate things happening
    during the query.
    """

    def __init__(self) -> None:
        self.pull_requests: dict[str, PullRequest] = {}
        self.created: list[dict[str, str]] = []
        self.fail_next_create = False
        self.statuses: dict[int, PullRequestStatus] = {}
        self.job_logs: dict[int, str] = {}
        self.on_status_query: Any = None
        self.feedback: dict[int, list[FeedbackItem]] = {}
        self.feedback_queries: list[int] = []

    async def list_feedback(self, repo: str, number: int) -> list[FeedbackItem]:
        self.feedback_queries.append(number)
        return self.feedback.get(number, [])

    async def get_pull_request_status(self, repo: str, number: int) -> PullRequestStatus:
        if number not in self.statuses:
            raise GitHubError(f"gh pr view failed: no pull request {number}")
        status = self.statuses[number]
        if self.on_status_query is not None:
            await self.on_status_query()
        return status

    async def fetch_job_log(self, repo: str, job_id: int) -> str:
        if job_id not in self.job_logs:
            raise GitHubError(f"gh run view failed: no log for job {job_id}")
        return self.job_logs[job_id]

    async def find_pull_request(self, repo: str, branch: str) -> PullRequest | None:
        return self.pull_requests.get(branch)

    async def create_pull_request(
        self, repo: str, *, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        if self.fail_next_create:
            self.fail_next_create = False
            raise GitHubError("gh pr create failed: HTTP 500")
        self.created.append(
            {"repo": repo, "head": head, "base": base, "title": title, "body": body}
        )
        pull_request = PullRequest(len(self.created) + 40, "open", "sha", f"https://x/{head}")
        self.pull_requests[head] = pull_request
        return pull_request


class Recorder:
    """Collect the events written during wrap-up, and store them as the Runner would."""

    def __init__(self, store: EventWriter | None = None) -> None:
        self.events: list[tuple[EventType, dict[str, Any]]] = []
        self._store = store

    async def __call__(self, event_type: EventType, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))
        if self._store is not None:
            await self._store(event_type, payload)

    def types(self) -> list[EventType]:
        return [event_type for event_type, _ in self.events]


class Scene:
    """A test scenario with a registered project and a created task."""

    def __init__(self, conn: aiosqlite.Connection, repo: GitRepo, config: Config) -> None:
        self.conn = conn
        self.repo = repo
        self.github = FakeGitHub()
        self.stages = RunStages(conn, config, self.github)  # type: ignore[arg-type]
        self.record = Recorder(self._store)
        self.task: db.Task
        self.run: db.Run

    async def setup(self, github_repo: str | None = "owner/demo") -> None:
        project = await db.upsert_project(
            self.conn, str(self.repo.path), github_repo=github_repo, default_branch="main"
        )
        self.task, self.run = await db.create_task_with_initial_run(
            self.conn,
            project_id=project.id,
            title="Add greeting",
            prompt="please add a greeting",
            agent_type="fake",
            model=None,
        )

    async def _store(self, event_type: EventType, payload: dict[str, Any]) -> None:
        await db.append_event(self.conn, self.task.id, self.run.id, event_type, payload)

    async def prepare(self) -> Path:
        await self.stages.prepare(self.task, self.run, self.record)
        self.task = await db.get_task(self.conn, self.task.id)
        assert self.task.worktree_path is not None
        return Path(self.task.worktree_path)

    async def finalize(self) -> db.Task:
        await self.stages.finalize(self.task, self.run, self.record)
        self.task = await db.get_task(self.conn, self.task.id)
        return self.task

    def remote_sha(self) -> str:
        assert self.task.branch is not None
        return self.repo.git("rev-parse", self.task.branch, cwd=self.repo.origin)


@pytest.fixture
async def scene(tmp_path: Path, git_repo: GitRepo) -> AsyncIterator[Scene]:
    conn = await db.connect(tmp_path / "test.db")
    await db.init_schema(conn)
    made = Scene(conn, git_repo, Config(data_dir=tmp_path / "data"))
    await made.setup()
    yield made
    await conn.close()


async def test_prepare_creates_a_worktree_and_records_it(scene: Scene, tmp_path: Path) -> None:
    path = await scene.prepare()

    assert path == tmp_path / "data" / "worktrees" / "repo" / f"task-{scene.task.id}"
    assert (path / "README.md").exists()
    assert scene.task.branch == f"asv/task-{scene.task.id}-add-greeting"


async def test_prepare_does_nothing_when_the_worktree_already_exists(scene: Scene) -> None:
    path = await scene.prepare()
    (path / "work-in-progress.txt").write_text("keep me")

    again = await scene.prepare()

    assert again == path
    assert (path / "work-in-progress.txt").read_text() == "keep me"


async def interrupted_prepare(scene: Scene, tmp_path: Path, *, with_worktree: bool) -> Path:
    """Leave what a prepare cut off before recording the worktree would leave behind."""
    branch = worktree.branch_name(scene.task.id, scene.task.title)
    path = tmp_path / "data" / "worktrees" / "repo" / f"task-{scene.task.id}"
    if with_worktree:
        await worktree.create(scene.repo.path, path, branch=branch, base_branch="main")
    else:
        scene.repo.git("branch", branch, "origin/main")
    return path


async def test_prepare_resumes_when_the_worktree_was_created_but_not_recorded(
    scene: Scene, tmp_path: Path
) -> None:
    expected = await interrupted_prepare(scene, tmp_path, with_worktree=True)

    path = await scene.prepare()

    assert path == expected
    assert (path / "README.md").exists()
    assert scene.task.branch == f"asv/task-{scene.task.id}-add-greeting"


async def test_prepare_resumes_when_only_the_branch_was_created(
    scene: Scene, tmp_path: Path
) -> None:
    expected = await interrupted_prepare(scene, tmp_path, with_worktree=False)

    path = await scene.prepare()

    assert path == expected
    assert (path / "README.md").exists()


@pytest.mark.parametrize("directory_left", [True, False], ids=["directory-left", "directory-gone"])
async def test_prepare_resumes_when_git_was_killed_while_adding_the_worktree(
    scene: Scene, tmp_path: Path, directory_left: bool
) -> None:
    # A killed `git worktree add` leaves the worktree registered and locked as "initializing".
    expected = await interrupted_prepare(scene, tmp_path, with_worktree=True)
    scene.repo.git("worktree", "lock", "--reason", "initializing", str(expected))
    if not directory_left:
        shutil.rmtree(expected)

    path = await scene.prepare()

    assert path == expected
    assert (path / "README.md").exists()


async def test_finalize_commits_pushes_and_opens_a_pull_request(scene: Scene) -> None:
    path = await scene.prepare()
    (path / "greeting.txt").write_text("hello\n")

    task = await scene.finalize()

    assert await worktree.has_uncommitted_changes(path) is False
    assert scene.remote_sha() == await worktree.head_sha(path)
    assert scene.github.created == [
        {
            "repo": "owner/demo",
            "head": task.branch,
            "base": "main",
            "title": "Add greeting",
            "body": scene.github.created[0]["body"],
        }
    ]
    assert "please add a greeting" in scene.github.created[0]["body"]
    assert (task.pr_number, task.pr_state) == (41, "open")
    assert task.head_sha == scene.remote_sha()
    assert (task.ci_state, task.review_state) == ("pending", "none")
    assert scene.record.types() == [EventType.PUSHED, EventType.PR_OPENED]
    assert scene.record.events[0][1]["head_sha"] == task.head_sha
    assert scene.record.events[1][1]["number"] == 41


async def test_finalize_keeps_commits_the_agent_already_made(scene: Scene) -> None:
    path = await scene.prepare()
    (path / "greeting.txt").write_text("hello\n")
    agent_sha = await worktree.commit_all(path, "feat: add greeting")

    await scene.finalize()

    assert scene.remote_sha() == agent_sha
    assert scene.repo.git("log", "-1", "--format=%s", cwd=path) == "feat: add greeting"


async def test_finalize_without_any_change_publishes_nothing(scene: Scene) -> None:
    await scene.prepare()

    task = await scene.finalize()

    assert task.pr_number is None
    assert task.head_sha is None
    assert scene.github.created == []
    assert scene.record.events == []


async def test_later_finalize_pushes_new_commits_to_the_same_pull_request(scene: Scene) -> None:
    path = await scene.prepare()
    (path / "greeting.txt").write_text("hello\n")
    await scene.finalize()
    await scene.conn.execute(
        "UPDATE tasks SET ci_state = 'failed', review_state = 'approved' WHERE id = ?",
        (scene.task.id,),
    )
    await scene.conn.commit()
    (path / "greeting.txt").write_text("hello again\n")

    task = await scene.finalize()

    assert len(scene.github.created) == 1
    assert scene.remote_sha() == await worktree.head_sha(path)
    assert task.head_sha == scene.remote_sha()
    assert (task.ci_state, task.review_state) == ("pending", "none")
    assert scene.record.types() == [EventType.PUSHED, EventType.PR_OPENED, EventType.PUSHED]
    assert "forced" not in scene.record.events[-1][1]


async def test_repeating_finalize_with_nothing_new_does_nothing(scene: Scene) -> None:
    path = await scene.prepare()
    (path / "greeting.txt").write_text("hello\n")
    await scene.finalize()
    events_before = len(scene.record.events)

    await scene.finalize()

    assert len(scene.github.created) == 1
    assert len(scene.record.events) == events_before


async def test_existing_pull_request_is_adopted_instead_of_opening_another(scene: Scene) -> None:
    path = await scene.prepare()
    assert scene.task.branch is not None
    scene.github.pull_requests[scene.task.branch] = PullRequest(77, "open", "old", "https://x/77")
    (path / "greeting.txt").write_text("hello\n")

    task = await scene.finalize()

    assert scene.github.created == []
    assert (task.pr_number, task.pr_state) == (77, "open")
    assert EventType.PR_OPENED not in scene.record.types()


async def test_push_failure_is_reported(scene: Scene) -> None:
    path = await scene.prepare()
    (path / "greeting.txt").write_text("hello\n")
    scene.repo.git("remote", "set-url", "origin", "/nonexistent/origin.git")

    # The remote is first read before pushing, so that is where the failure shows up.
    with pytest.raises(worktree.GitError, match=r"nonexistent/origin\.git"):
        await scene.finalize()


def push_as_someone_else(scene: Scene, tmp_path: Path) -> str:
    """Add a commit to the task's branch from another clone, as a person on GitHub would."""
    assert scene.task.branch is not None
    other = tmp_path / "other"
    scene.repo.git("clone", "-q", "-b", scene.task.branch, str(scene.repo.origin), str(other))
    (other / "suggestion.txt").write_text("from review\n")
    scene.repo.git("add", "suggestion.txt", cwd=other)
    identity = ["-c", "user.email=other@example.com", "-c", "user.name=Other"]
    scene.repo.git(*identity, "commit", "-q", "-m", "apply suggestion", cwd=other)
    scene.repo.git("push", "-q", "origin", scene.task.branch, cwd=other)
    return scene.repo.git("rev-parse", "HEAD", cwd=other)


async def test_commits_the_agent_rewrote_after_pushing_replace_the_pushed_ones(
    scene: Scene,
) -> None:
    path = await scene.prepare()
    (path / "greeting.txt").write_text("hello\n")
    await scene.finalize()
    pushed = scene.remote_sha()
    (path / "greeting.txt").write_text("hello, amended\n")
    scene.repo.git("commit", "-q", "-a", "--amend", "-m", "feat: add greeting", cwd=path)

    task = await scene.finalize()

    assert scene.remote_sha() == await worktree.head_sha(path) == task.head_sha
    event_type, payload = scene.record.events[-1]
    assert event_type is EventType.PUSHED
    assert (payload["forced"], payload["replaced_sha"]) == (True, pushed)


@pytest.mark.parametrize("agent_rewrote", [False, True], ids=["new-commit", "rewritten"])
async def test_a_commit_someone_else_pushed_is_never_overwritten(
    scene: Scene, tmp_path: Path, agent_rewrote: bool
) -> None:
    path = await scene.prepare()
    (path / "greeting.txt").write_text("hello\n")
    await scene.finalize()
    theirs = push_as_someone_else(scene, tmp_path)
    (path / "greeting.txt").write_text("hello again\n")
    if agent_rewrote:
        scene.repo.git("commit", "-q", "-a", "--amend", "-m", "feat: add greeting", cwd=path)
    else:
        await worktree.commit_all(path, "feat: more greeting")
    events_before = len(scene.record.events)

    with pytest.raises(StageError, match="did not push"):
        await scene.finalize()

    assert scene.remote_sha() == theirs
    assert len(scene.record.events) == events_before


async def test_pull_request_failure_can_be_retried_without_pushing_again(scene: Scene) -> None:
    path = await scene.prepare()
    (path / "greeting.txt").write_text("hello\n")
    scene.github.fail_next_create = True
    with pytest.raises(GitHubError):
        await scene.finalize()

    task = await scene.finalize()

    assert task.pr_number == 41
    assert scene.record.types() == [EventType.PUSHED, EventType.PR_OPENED]


async def test_project_without_a_github_repo_cannot_be_published(
    tmp_path: Path, git_repo: GitRepo
) -> None:
    conn = await db.connect(tmp_path / "other.db")
    await db.init_schema(conn)
    scene = Scene(conn, git_repo, Config(data_dir=tmp_path / "data"))
    await scene.setup(github_repo=None)
    path = await scene.prepare()
    (path / "greeting.txt").write_text("hello\n")

    with pytest.raises(StageError, match="GitHub"):
        await scene.finalize()
    await conn.close()


async def test_archive_commits_leftovers_locally_and_removes_the_worktree(scene: Scene) -> None:
    path = await scene.prepare()
    (path / "half-done.txt").write_text("unfinished\n")
    assert scene.task.branch is not None

    await scene.stages.archive(scene.task)

    assert not path.exists()
    assert scene.repo.git("show", f"{scene.task.branch}:half-done.txt") == "unfinished"
    assert scene.repo.git("branch", "--list", scene.task.branch, cwd=scene.repo.origin) == ""


async def test_archive_completes_when_the_worktree_was_already_removed(scene: Scene) -> None:
    # The previous archive removed the worktree but was cut off before marking the task.
    path = await scene.prepare()
    (path / "half-done.txt").write_text("unfinished\n")
    await scene.stages.archive(scene.task)
    assert not path.exists()

    await scene.stages.archive(scene.task)

    assert scene.task.branch is not None
    assert scene.repo.git("show", f"{scene.task.branch}:half-done.txt") == "unfinished"


async def test_archive_keeps_the_worktree_when_leftovers_cannot_be_committed(scene: Scene) -> None:
    path = await scene.prepare()
    hook = scene.repo.path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (path / "half-done.txt").write_text("unfinished\n")

    with pytest.raises(worktree.GitError):
        await scene.stages.archive(scene.task)

    assert (path / "half-done.txt").exists()


async def test_archive_of_a_task_that_never_got_a_worktree_is_a_no_op(scene: Scene) -> None:
    await scene.stages.archive(scene.task)

    assert scene.task.worktree_path is None


async def test_prepare_refuses_a_task_whose_directory_is_not_a_task_worktree(scene: Scene) -> None:
    # A task created in the task 1 era: its working directory points straight at the user's
    # repo, with no branch of its own.
    await db.update_task(scene.conn, scene.task.id, worktree_path=str(scene.repo.path))

    with pytest.raises(StageError, match="before worktrees"):
        await scene.stages.prepare(scene.task, scene.run, scene.record)


async def test_archive_leaves_a_directory_that_is_not_a_task_worktree_alone(scene: Scene) -> None:
    await db.update_task(scene.conn, scene.task.id, worktree_path=str(scene.repo.path))
    (scene.repo.path / "users-own-work.txt").write_text("do not touch\n")

    await scene.stages.archive(scene.task)

    assert (scene.repo.path / "users-own-work.txt").exists()
    assert scene.repo.git("status", "--porcelain") == "?? users-own-work.txt"
    assert scene.repo.git("log", "--oneline").count("\n") == 0


async def ci_failure_run(scene: Scene, head_sha: str) -> db.Run:
    """Advance to "PR open, CI failed on head_sha" and create the matching ci_failure run."""
    await scene.prepare()
    await db.finish_run(scene.conn, scene.run.id, ExitStatus.SUCCESS)
    await db.update_task(scene.conn, scene.task.id, pr_number=7, pr_state="open", head_sha=head_sha)
    return await db.create_run(
        scene.conn, scene.task.id, RunTrigger.CI_FAILURE, "placeholder", head_sha
    )


async def test_prepare_for_a_ci_failure_puts_the_failed_log_into_the_run_input(
    scene: Scene,
) -> None:
    run = await ci_failure_run(scene, "abc123")
    scene.github.statuses[7] = pr_status(
        "abc123",
        CiState.FAILED,
        failed_checks=(FailedCheck("pytest", 11), FailedCheck("external/lint", None)),
    )
    scene.github.job_logs[11] = "pytest\tRun tests\t2026-01-01T00:00:00.0Z AssertionError: boom\n"

    await scene.stages.prepare(scene.task, run, scene.record)

    prompt = (await db.get_run(scene.conn, run.id)).input_text
    assert "abc123" in prompt
    assert "## Failed check: pytest" in prompt
    assert "AssertionError: boom" in prompt
    assert "2026-01-01T" not in prompt
    assert "external/lint" in prompt


async def test_prepare_for_a_ci_failure_fails_when_no_log_can_be_fetched(scene: Scene) -> None:
    run = await ci_failure_run(scene, "abc123")
    scene.github.statuses[7] = pr_status(
        "abc123", CiState.FAILED, failed_checks=(FailedCheck("pytest", 11),)
    )

    with pytest.raises(GitHubError, match="no log for job 11"):
        await scene.stages.prepare(scene.task, run, scene.record)


async def test_prepare_for_a_ci_failure_fails_when_only_external_checks_failed(
    scene: Scene,
) -> None:
    run = await ci_failure_run(scene, "abc123")
    scene.github.statuses[7] = pr_status(
        "abc123", CiState.FAILED, failed_checks=(FailedCheck("external/lint", None),)
    )

    with pytest.raises(StageError, match="no CI log"):
        await scene.stages.prepare(scene.task, run, scene.record)


async def test_prepare_for_a_ci_failure_fails_when_the_pull_request_moved_on(
    scene: Scene,
) -> None:
    run = await ci_failure_run(scene, "abc123")
    scene.github.statuses[7] = pr_status("def456", CiState.PENDING)

    with pytest.raises(StageError, match="another commit"):
        await scene.stages.prepare(scene.task, run, scene.record)


async def test_prepare_for_a_ci_failure_fails_clearly_when_ci_is_no_longer_failing(
    scene: Scene,
) -> None:
    """Someone reran the failed job on GitHub: the same commit no longer has failing checks."""
    run = await ci_failure_run(scene, "abc123")
    scene.github.statuses[7] = pr_status("abc123", CiState.PENDING)

    with pytest.raises(StageError, match="no longer failing on this commit"):
        await scene.stages.prepare(scene.task, run, scene.record)


async def conflict_run(scene: Scene, trigger: RunTrigger = RunTrigger.MERGE_CONFLICT) -> Path:
    """The task's branch and main changed the same line, and a run to handle it already exists."""
    path = await scene.prepare()
    (path / "README.md").write_text("from the task\n")
    await scene.finalize()
    await db.finish_run(scene.conn, scene.run.id, ExitStatus.SUCCESS)
    scene.repo.push_to_main("README.md", "from main\n")
    scene.run = await db.create_run(scene.conn, scene.task.id, trigger, "placeholder", "sha")
    return path


async def test_prepare_for_a_conflict_starts_the_merge_and_tells_the_agent_which_files(
    scene: Scene,
) -> None:
    path = await conflict_run(scene)

    await scene.prepare()

    prompt = (await db.get_run(scene.conn, scene.run.id)).input_text
    assert "- README.md" in prompt
    assert "Do not run git merge" in prompt
    assert await worktree.is_merge_in_progress(path)


async def test_finalize_publishes_the_merge_once_the_conflict_is_resolved(scene: Scene) -> None:
    path = await conflict_run(scene)
    await scene.prepare()
    (path / "README.md").write_text("from the task and from main\n")

    task = await scene.finalize()

    parents = scene.repo.git("rev-list", "--parents", "-1", "HEAD", cwd=path).split()
    assert len(parents) == 3
    assert scene.remote_sha() == await worktree.head_sha(path) == task.head_sha
    assert task.merge_state == "unknown"
    assert "resolve conflicts" in scene.repo.git("log", "-1", "--format=%s", cwd=path)


async def test_finalize_refuses_to_publish_leftover_conflict_markers(scene: Scene) -> None:
    path = await conflict_run(scene)
    await scene.prepare()
    before = scene.remote_sha()

    with pytest.raises(StageError, match="conflict markers are still present in: README"):
        await scene.finalize()

    assert scene.remote_sha() == before
    assert await worktree.is_merge_in_progress(path)


async def test_later_run_of_any_kind_cannot_publish_a_half_finished_merge(scene: Scene) -> None:
    """After a conflict run is stopped, the next run (e.g. a user message) can't push markers."""
    await conflict_run(scene)
    await scene.prepare()
    await db.finish_run(scene.conn, scene.run.id, ExitStatus.STOPPED)
    scene.run = await db.create_run(scene.conn, scene.task.id, RunTrigger.USER_MESSAGE, "go on")
    before = scene.remote_sha()

    with pytest.raises(StageError, match="conflict markers"):
        await scene.finalize()

    assert scene.remote_sha() == before


async def test_prepare_for_a_conflict_fails_when_there_is_nothing_to_resolve(scene: Scene) -> None:
    path = await scene.prepare()
    (path / "feature.txt").write_text("new\n")
    await scene.finalize()
    await db.finish_run(scene.conn, scene.run.id, ExitStatus.SUCCESS)
    scene.repo.push_to_main("README.md", "from main\n")
    scene.run = await db.create_run(
        scene.conn, scene.task.id, RunTrigger.MERGE_CONFLICT, "placeholder", "sha"
    )

    with pytest.raises(StageError, match="no conflict locally"):
        await scene.prepare()

    assert not await worktree.is_merge_in_progress(path)
    assert not await worktree.has_uncommitted_changes(path)


async def test_archive_drops_a_half_finished_merge_instead_of_saving_the_markers(
    scene: Scene,
) -> None:
    path = await conflict_run(scene)
    await scene.prepare()
    task_commit = scene.remote_sha()

    await scene.stages.archive(scene.task)

    assert not path.exists()
    assert scene.task.branch is not None
    assert scene.repo.git("rev-parse", scene.task.branch) == task_commit


async def test_merge_resolved_by_keeping_the_task_side_is_still_committed(scene: Scene) -> None:
    """Keeping "ours" everywhere matches the pre-merge content.

    Git then sees no change, but the merge must still be completed.
    """
    path = await conflict_run(scene)
    await scene.prepare()
    (path / "README.md").write_text("from the task\n")
    scene.repo.git("add", "README.md", cwd=path)
    assert not await worktree.has_uncommitted_changes(path)

    await scene.finalize()

    parents = scene.repo.git("rev-list", "--parents", "-1", "HEAD", cwd=path).split()
    assert len(parents) == 3
    assert scene.remote_sha() == await worktree.head_sha(path)
    assert not await worktree.is_merge_in_progress(path)
