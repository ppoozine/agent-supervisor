"""Poller 的測試：GitHub 狀態快取的更新。GitHub 是替身，Runner 與資料庫是真的。"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from agent_supervisor import db
from agent_supervisor.config import Config
from agent_supervisor.enums import (
    CiState,
    EventType,
    ExitStatus,
    MergeState,
    PrState,
    ReviewState,
)
from agent_supervisor.github import FeedbackItem, FeedbackKind, GitHubError
from agent_supervisor.poller import Poller
from agent_supervisor.runner import EventWriter, Runner
from tests.fake_adapter import FakeAdapter, result, session_started
from tests.test_stages import FakeGitHub, pr_status


class Scene:
    """一個 PR 開著的任務，加上 Poller。"""

    def __init__(self, conn: aiosqlite.Connection, config: Config) -> None:
        self.conn = conn
        self.github = FakeGitHub()
        self.adapter = FakeAdapter([])
        self.runner = Runner(conn, {"fake": self.adapter}, config, prepare=self._prepare)
        self.poller = Poller(conn, self.github, stop_run=self.runner.stop_if_running)  # type: ignore[arg-type]
        self.finalized: list[int] = []

    async def _prepare(self, task: db.Task, run: db.Run, record: EventWriter) -> None:
        await db.update_task(self.conn, task.id, worktree_path="/work")

    async def task_with_pr(self, number: int = 7, head_sha: str = "sha-1") -> db.Task:
        """開一個任務、跑完 initial run，並當作收尾已經 push 且開了 PR。"""
        self.adapter.scripts.append([session_started(), result()])
        project = await db.upsert_project(
            self.conn, f"/repos/demo-{number}", github_repo="owner/demo"
        )
        run = await self.runner.start_task(
            project_id=project.id, title="demo", prompt="do it", agent_type="fake", model=None
        )
        await self.runner.wait(run.id)
        await db.update_task(
            self.conn,
            run.task_id,
            pr_number=number,
            pr_state="open",
            head_sha=head_sha,
            ci_state="pending",
            merge_state="clean",
        )
        return await db.get_task(self.conn, run.task_id)

    async def ci_events(self, task_id: int) -> list[db.Event]:
        events = await db.list_events(self.conn, task_id)
        return [event for event in events if event.type == "ci_status"]


@pytest.fixture
async def scene(tmp_path: Path) -> AsyncIterator[Scene]:
    conn = await db.connect(tmp_path / "test.db")
    await db.init_schema(conn)
    yield Scene(conn, Config(data_dir=tmp_path))
    await conn.close()


async def test_poll_updates_the_four_fields_together(scene: Scene) -> None:
    task = await scene.task_with_pr()
    scene.github.statuses[7] = pr_status("sha-1", CiState.FAILED, review_state=ReviewState.APPROVED)

    report = await scene.poller.poll_once()

    after = await db.get_task(scene.conn, task.id)
    assert (after.pr_state, after.head_sha, after.ci_state, after.review_state) == (
        "open",
        "sha-1",
        "failed",
        "approved",
    )
    assert [change.task_id for change in report.changes] == [task.id]
    assert len(await scene.ci_events(task.id)) == 1


async def test_poll_writes_an_event_only_when_something_changed(scene: Scene) -> None:
    task = await scene.task_with_pr()
    scene.github.statuses[7] = pr_status("sha-1", CiState.PENDING)
    await scene.poller.poll_once()
    await scene.poller.poll_once()
    assert await scene.ci_events(task.id) == []

    scene.github.statuses[7] = pr_status("sha-1", CiState.PASSED)
    await scene.poller.poll_once()
    report = await scene.poller.poll_once()

    assert len(await scene.ci_events(task.id)) == 1
    assert report.changes == ()


async def test_poll_follows_a_commit_pushed_by_someone_else(scene: Scene) -> None:
    task = await scene.task_with_pr(head_sha="sha-1")
    scene.github.statuses[7] = pr_status("sha-by-hand", CiState.PENDING)

    await scene.poller.poll_once()

    assert (await db.get_task(scene.conn, task.id)).head_sha == "sha-by-hand"


async def test_stale_result_is_discarded_when_a_push_happened_during_the_query(
    scene: Scene,
) -> None:
    task = await scene.task_with_pr(head_sha="sha-1")
    scene.github.statuses[7] = pr_status("sha-1", CiState.FAILED)

    async def push_during_query() -> None:
        await db.update_task(scene.conn, task.id, head_sha="sha-2", ci_state="pending")

    scene.github.on_status_query = push_during_query

    report = await scene.poller.poll_once()

    after = await db.get_task(scene.conn, task.id)
    assert (after.head_sha, after.ci_state) == ("sha-2", "pending")
    assert report.changes == ()
    assert await scene.ci_events(task.id) == []


@pytest.mark.parametrize("final_state", [PrState.MERGED, PrState.CLOSED])
async def test_running_run_is_stopped_when_the_pull_request_is_finished(
    scene: Scene, final_state: PrState
) -> None:
    task = await scene.task_with_pr()
    scene.adapter.scripts.append([asyncio.Event(), result()])
    run = await scene.runner.send_message(task.id, "keep going")
    scene.github.statuses[7] = pr_status("sha-1", CiState.PASSED, pr_state=final_state)

    await scene.poller.poll_once()

    finished = await db.get_run(scene.conn, run.id)
    assert finished.exit_status == ExitStatus.STOPPED
    assert (await db.get_task(scene.conn, task.id)).pr_state == final_state
    with pytest.raises(db.TaskNotRunnableError):
        await scene.runner.send_message(task.id, "one more thing")


async def test_finished_pull_request_without_a_run_is_just_recorded(scene: Scene) -> None:
    task = await scene.task_with_pr()
    scene.github.statuses[7] = pr_status("sha-1", CiState.PASSED, pr_state=PrState.MERGED)

    await scene.poller.poll_once()
    scene.github.statuses.clear()  # 之後不該再查這個 PR
    report = await scene.poller.poll_once()

    assert (await db.get_task(scene.conn, task.id)).pr_state == "merged"
    assert report.errors == ()


async def test_one_failing_query_does_not_block_the_other_tasks(scene: Scene) -> None:
    broken = await scene.task_with_pr(number=7)
    healthy = await scene.task_with_pr(number=8)
    scene.github.statuses[8] = pr_status("sha-1", CiState.PASSED)

    report = await scene.poller.poll_once()

    assert [task_id for task_id, _ in report.errors] == [broken.id]
    assert (await db.get_task(scene.conn, healthy.id)).ci_state == "passed"
    assert (await db.get_task(scene.conn, broken.id)).ci_state == "pending"


async def test_lagging_github_cannot_bring_back_a_commit_we_already_replaced(
    scene: Scene,
) -> None:
    """GitHub 的資料落後剛 push 的 commit 時，不能把快取寫回舊的 commit。"""
    task = await scene.task_with_pr(head_sha="sha-1")
    for sha in ("sha-1", "sha-2"):
        await db.append_event(scene.conn, task.id, None, EventType.PUSHED, {"head_sha": sha})
    await db.update_task(scene.conn, task.id, head_sha="sha-2", ci_state="pending")
    scene.github.statuses[7] = pr_status("sha-1", CiState.FAILED)

    report = await scene.poller.poll_once()

    after = await db.get_task(scene.conn, task.id)
    assert (after.head_sha, after.ci_state) == ("sha-2", "pending")
    assert report.changes == ()


@pytest.mark.parametrize("final_state", [PrState.MERGED, PrState.CLOSED])
async def test_finished_pull_request_is_recorded_even_if_it_stays_on_an_earlier_commit(
    scene: Scene, final_state: PrState
) -> None:
    """PR 在舊 commit 上被合併或關閉後我們又 push 了一次：GitHub 回報的永遠是舊 commit。"""
    task = await scene.task_with_pr(head_sha="sha-1")
    for sha in ("sha-1", "sha-2"):
        await db.append_event(scene.conn, task.id, None, EventType.PUSHED, {"head_sha": sha})
    await db.update_task(scene.conn, task.id, head_sha="sha-2", ci_state="pending")
    scene.github.statuses[7] = pr_status("sha-1", CiState.PASSED, pr_state=final_state)

    report = await scene.poller.poll_once()

    assert (await db.get_task(scene.conn, task.id)).pr_state == final_state
    assert [change.task_id for change in report.changes] == [task.id]
    assert await db.list_tasks_with_open_pr(scene.conn) == []


async def test_archived_task_still_learns_how_its_pull_request_ended(scene: Scene) -> None:
    """封存不會關掉 PR；之後在 GitHub 上合併，任務仍然要知道。"""
    task = await scene.task_with_pr()
    await db.archive_task(scene.conn, task.id)
    scene.github.statuses[7] = pr_status("sha-1", CiState.PASSED, pr_state=PrState.MERGED)

    report = await scene.poller.poll_once()

    assert (await db.get_task(scene.conn, task.id)).pr_state == PrState.MERGED
    assert [change.task_id for change in report.changes] == [task.id]


def general_comment(github_id: str, *, is_bot: bool = False) -> FeedbackItem:
    return FeedbackItem(
        github_id=github_id,
        kind=FeedbackKind.COMMENT,
        author="ci[bot]" if is_bot else "alice",
        is_bot=is_bot,
        body="please rename this",
        created_at="2026-11-19T10:00:00Z",
        url="u",
    )


async def test_new_comment_is_recorded_and_reported(scene: Scene) -> None:
    task = await scene.task_with_pr()
    scene.github.statuses[7] = pr_status("sha-1", CiState.PENDING, feedback_ids=("IC_1",))
    scene.github.feedback[7] = [general_comment("IC_1")]

    report = await scene.poller.poll_once()

    (pending,) = await db.list_pending_feedback(scene.conn, task.id)
    assert report.new_feedback == (task.id,)
    assert (pending.type, pending.run_id) == (EventType.REVIEW_COMMENT, None)
    assert (pending.payload["author"], pending.payload["body"]) == ("alice", "please rename this")


async def test_comments_are_fetched_only_when_there_is_one_we_have_not_seen(scene: Scene) -> None:
    await scene.task_with_pr()
    scene.github.statuses[7] = pr_status("sha-1", CiState.PENDING)
    await scene.poller.poll_once()
    assert scene.github.feedback_queries == []

    scene.github.statuses[7] = pr_status("sha-1", CiState.PENDING, feedback_ids=("IC_1", "IC_2"))
    scene.github.feedback[7] = [general_comment("IC_1"), general_comment("IC_2", is_bot=True)]
    first = await scene.poller.poll_once()
    second = await scene.poller.poll_once()

    assert scene.github.feedback_queries == [7]
    assert (len(first.new_feedback), second.new_feedback) == (1, ())


async def test_bot_comment_alone_is_not_new_feedback(scene: Scene) -> None:
    task = await scene.task_with_pr()
    scene.github.statuses[7] = pr_status("sha-1", CiState.PENDING, feedback_ids=("IC_1",))
    scene.github.feedback[7] = [general_comment("IC_1", is_bot=True)]

    report = await scene.poller.poll_once()

    assert report.new_feedback == ()
    assert await db.list_pending_feedback(scene.conn, task.id) == []
    assert await db.list_feedback_ids(scene.conn, task.id) == {"IC_1"}


async def test_comments_on_an_archived_task_are_not_fetched(scene: Scene) -> None:
    task = await scene.task_with_pr()
    await db.archive_task(scene.conn, task.id)
    scene.github.statuses[7] = pr_status("sha-1", CiState.PENDING, feedback_ids=("IC_1",))
    scene.github.feedback[7] = [general_comment("IC_1")]

    report = await scene.poller.poll_once()

    assert (report.new_feedback, scene.github.feedback_queries) == ((), [])


async def test_failing_comment_query_keeps_the_status_and_is_reported(scene: Scene) -> None:
    task = await scene.task_with_pr()
    scene.github.statuses[7] = pr_status("sha-1", CiState.PASSED, feedback_ids=("IC_1",))

    async def fail(repo: str, number: int) -> list[FeedbackItem]:
        raise GitHubError("gh api failed: HTTP 502")

    scene.github.list_feedback = fail  # type: ignore[method-assign]

    report = await scene.poller.poll_once()

    assert (await db.get_task(scene.conn, task.id)).ci_state == "passed"
    assert [task_id for task_id, _ in report.errors] == [task.id]
    assert report.new_feedback == ()


async def test_poll_stores_whether_the_pull_request_conflicts(scene: Scene) -> None:
    task = await scene.task_with_pr()
    scene.github.statuses[7] = pr_status(
        "sha-1", CiState.PENDING, merge_state=MergeState.CONFLICTING
    )

    await scene.poller.poll_once()

    (event,) = await scene.ci_events(task.id)
    assert (await db.get_task(scene.conn, task.id)).merge_state == "conflicting"
    assert event.payload["merge_state"] == "conflicting"
