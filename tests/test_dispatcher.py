"""Dispatcher tests: dispatching auto-fixes and the infinite-loop guards.

The Runner and the database are real; the agent is a fake adapter. CI results are written
directly into the task's cached fields (normally the Poller's job), and pushes are simulated
by a fake wrap-up stage.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest

from agent_supervisor import db, status
from agent_supervisor.config import Config
from agent_supervisor.dispatcher import Dispatcher
from agent_supervisor.enums import ExitStatus, RunTrigger
from agent_supervisor.runner import EventWriter, Runner
from agent_supervisor.status import Column
from tests.fake_adapter import FakeAdapter, result, session_started

FIX_LOG = "CI failed on commit {sha}: AssertionError"


class Loop:
    """One task's fix loop: Runner, Dispatcher, and a controllable CI."""

    def __init__(self, conn: aiosqlite.Connection, config: Config) -> None:
        self.conn = conn
        self.config = config
        self.adapter = FakeAdapter([])
        self.pushes: list[str] = []
        self.fail_log_fetch = False
        self.runner = Runner(
            conn, {"fake": self.adapter}, config, prepare=self._prepare, finalize=self._finalize
        )
        self.limits = status.Limits(config.task_cost_limit_usd, config.task_token_limit)
        self.dispatcher = Dispatcher(conn, self.runner, self.limits)
        self.task_id = 0

    async def _prepare(self, task: db.Task, run: db.Run, record: EventWriter) -> None:
        await db.update_task(self.conn, task.id, worktree_path="/work", branch="asv/task")
        if run.trigger is not RunTrigger.CI_FAILURE:
            return
        if self.fail_log_fetch:
            raise RuntimeError("gh run view failed")
        await db.set_run_input_text(self.conn, run.id, FIX_LOG.format(sha=run.source_head_sha))

    async def _finalize(self, task: db.Task, run: db.Run, record: EventWriter) -> None:
        """Simulate wrap-up: if the script has a push, update head_sha and reset CI to pending."""
        if not self.pushes:
            return
        await db.update_task(
            self.conn,
            task.id,
            pr_number=7,
            pr_state="open",
            head_sha=self.pushes.pop(0),
            ci_state="pending",
        )

    async def start(self, *, pushes: str | None = "sha-1", cost_usd: float = 0.5) -> None:
        """Run the initial run to completion. By default it pushes a commit and opens a PR."""
        if pushes is not None:
            self.pushes.append(pushes)
        self.adapter.scripts.append([session_started(), result(cost_usd=cost_usd)])
        project = await db.upsert_project(self.conn, "/repos/demo")
        run = await self.runner.start_task(
            project_id=project.id, title="demo", prompt="do it", agent_type="fake", model=None
        )
        self.task_id = run.task_id
        await self.runner.wait(run.id)

    async def ci(self, state: str, **fields: object) -> None:
        """The Poller found a CI result. By default it also finds that the PR has no conflicts."""
        await db.update_task(
            self.conn, self.task_id, **{"ci_state": state, "merge_state": "clean", **fields}
        )

    async def dispatch(self) -> list[db.Run]:
        return await self.dispatcher.dispatch_once()

    async def fix(
        self, *, pushes: str | None, exit_status: ExitStatus = ExitStatus.SUCCESS
    ) -> db.Run:
        """Dispatch and wait for the fix run to finish. `pushes` is the commit this fix pushes."""
        if pushes is not None:
            self.pushes.append(pushes)
        self.adapter.scripts.append([result(exit_status)])
        (run,) = await self.dispatch()
        return await self.runner.wait(run.id)

    async def task(self) -> db.Task:
        return await db.get_task(self.conn, self.task_id)

    async def runs(self) -> list[db.Run]:
        return await db.list_runs(self.conn, self.task_id)

    async def board(self) -> status.BoardStatus:
        snapshot = await db.get_snapshot(self.conn, self.task_id)
        return status.board_status(snapshot, self.limits)


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    connection = await db.connect(tmp_path / "test.db")
    await db.init_schema(connection)
    yield connection
    await connection.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path)


@pytest.fixture
def loop(conn: aiosqlite.Connection, config: Config) -> Loop:
    return Loop(conn, config)


async def test_failed_ci_is_fed_back_to_the_same_session(loop: Loop) -> None:
    await loop.start()
    await loop.ci("failed")
    assert (await loop.board()).column == Column.WORKING

    run = await loop.fix(pushes="sha-2")

    assert (run.trigger, run.source_head_sha, run.exit_status) == (
        RunTrigger.CI_FAILURE,
        "sha-1",
        ExitStatus.SUCCESS,
    )
    call = loop.adapter.calls[-1]
    assert call.session_id == "session-1"
    assert call.prompt == FIX_LOG.format(sha="sha-1")
    task = await loop.task()
    assert (task.last_handled_head_sha, task.ci_fix_attempts, task.head_sha) == (
        "sha-1",
        1,
        "sha-2",
    )


async def test_nothing_is_dispatched_while_ci_is_not_failed(loop: Loop) -> None:
    await loop.start()

    for state in ("pending", "passed", "none"):
        await loop.ci(state)
        assert await loop.dispatch() == []


async def test_rule_1_the_same_commit_is_fed_back_only_once(loop: Loop) -> None:
    await loop.start()
    await loop.ci("failed")
    await loop.fix(pushes=None)  # the fix produced no new commit

    assert await loop.dispatch() == []
    assert await loop.dispatch() == []
    assert len(await loop.runs()) == 2
    assert (await loop.task()).ci_fix_attempts == 1
    assert await loop.board() == status.BoardStatus(Column.NEEDS_YOU, "CI failed")


async def test_rule_2_auto_fix_stops_after_three_attempts(loop: Loop) -> None:
    await loop.start()
    for attempt in range(1, 4):
        await loop.ci("failed")
        await loop.fix(pushes=f"fix-{attempt}")
    await loop.ci("failed")

    assert await loop.dispatch() == []
    assert (await loop.task()).ci_fix_attempts == 3
    assert [run.trigger for run in await loop.runs()].count(RunTrigger.CI_FAILURE) == 3
    assert await loop.board() == status.BoardStatus(Column.NEEDS_YOU, "CI failed")


async def test_third_fix_that_turns_ci_green_is_ready_to_merge(loop: Loop) -> None:
    await loop.start()
    for attempt in range(1, 4):
        await loop.ci("failed")
        await loop.fix(pushes=f"fix-{attempt}")
    await loop.ci("passed")

    assert await loop.dispatch() == []
    assert await loop.board() == status.BoardStatus(Column.READY_TO_MERGE, "Ready")


async def test_rule_3_no_auto_fix_once_the_task_cost_limit_is_reached(
    conn: aiosqlite.Connection, config: Config
) -> None:
    loop = Loop(conn, replace(config, task_cost_limit_usd=0.4))
    await loop.start(cost_usd=0.5)
    await loop.ci("failed")

    assert await loop.dispatch() == []
    assert len(await loop.runs()) == 1
    assert await loop.board() == status.BoardStatus(Column.NEEDS_YOU, "Over budget")


async def test_rule_3_no_auto_fix_once_the_task_token_limit_is_reached(
    conn: aiosqlite.Connection, config: Config
) -> None:
    loop = Loop(conn, replace(config, task_token_limit=110))  # one run uses 100 + 10
    await loop.start()
    await loop.ci("failed")

    assert await loop.dispatch() == []
    assert await loop.board() == status.BoardStatus(Column.NEEDS_YOU, "Over budget")


async def test_rule_3_budget_counts_again_from_the_latest_user_message(
    conn: aiosqlite.Connection, config: Config
) -> None:
    loop = Loop(conn, replace(config, task_cost_limit_usd=1.0))
    await loop.start(cost_usd=1.5)
    await loop.ci("failed")
    assert await loop.dispatch() == []
    loop.pushes.append("sha-2")
    loop.adapter.scripts.append([result(cost_usd=0.25)])
    await loop.runner.wait((await loop.runner.send_message(loop.task_id, "carry on")).id)
    await loop.ci("failed")

    run = await loop.fix(pushes="sha-3")

    assert run.trigger == RunTrigger.CI_FAILURE
    assert loop.adapter.calls[-1].limits.max_budget_usd == pytest.approx(0.75)
    # The card still shows the whole task's cost: 1.5 + 0.25 + 0.5 for this fix.
    assert (await db.get_task_usage(conn, loop.task_id)).cost_usd == pytest.approx(2.25)


async def test_rule_4_fix_run_gets_the_remaining_budget_and_turn_limit(
    conn: aiosqlite.Connection, config: Config
) -> None:
    loop = Loop(conn, replace(config, task_cost_limit_usd=2.0, max_turns_per_run=9))
    await loop.start(cost_usd=0.5)
    await loop.ci("failed")

    run = await loop.fix(pushes="sha-2", exit_status=ExitStatus.LIMIT_REACHED)

    limits = loop.adapter.calls[-1].limits
    assert limits.max_budget_usd == pytest.approx(1.5)
    assert limits.max_turns == 9
    assert run.exit_status == ExitStatus.LIMIT_REACHED
    assert (await loop.board()).column == Column.NEEDS_YOU


async def test_rule_5_fix_run_that_exceeds_the_time_limit_is_cut_off(
    conn: aiosqlite.Connection, config: Config
) -> None:
    loop = Loop(conn, replace(config, run_timeout_seconds=0.05))
    await loop.start()
    await loop.ci("failed")
    loop.adapter.scripts.append([asyncio.Event(), result()])  # the agent never finishes

    (run,) = await loop.dispatch()
    finished = await loop.runner.wait(run.id)

    assert finished.exit_status == ExitStatus.LIMIT_REACHED
    assert await loop.dispatch() == []
    assert await loop.board() == status.BoardStatus(Column.NEEDS_YOU, "Limit reached")


async def test_no_auto_fix_after_the_user_stopped_a_run(loop: Loop) -> None:
    await loop.start()
    await loop.ci("failed")
    loop.adapter.scripts.append([asyncio.Event(), result()])
    (run,) = await loop.dispatch()

    await loop.runner.stop(loop.task_id)
    await loop.ci("failed", head_sha="someone-else-pushed")  # even if a new failing commit appears

    assert (await loop.runner.wait(run.id)).exit_status == ExitStatus.STOPPED
    assert await loop.dispatch() == []
    assert await loop.board() == status.BoardStatus(Column.NEEDS_YOU, "Stopped")


async def test_user_message_is_refused_while_a_fix_is_running(loop: Loop) -> None:
    await loop.start()
    await loop.ci("failed")
    release = asyncio.Event()
    loop.adapter.scripts.append([release, result()])
    (run,) = await loop.dispatch()

    with pytest.raises(db.RunAlreadyActiveError):
        await loop.runner.send_message(loop.task_id, "also do this")
    assert await loop.dispatch() == []

    release.set()
    await loop.runner.wait(run.id)
    assert len(await loop.runs()) == 2


async def test_no_auto_fix_when_a_reviewer_requested_changes(loop: Loop) -> None:
    await loop.start()
    await loop.ci("failed", review_state="changes_requested")

    assert await loop.dispatch() == []
    assert await loop.board() == status.BoardStatus(Column.NEEDS_YOU, "Changes requested")


async def test_failing_to_fetch_the_log_ends_the_run_and_keeps_the_attempt(loop: Loop) -> None:
    await loop.start()
    await loop.ci("failed")
    loop.fail_log_fetch = True

    (run,) = await loop.dispatch()
    finished = await loop.runner.wait(run.id)

    assert finished.exit_status == ExitStatus.ERROR
    assert len(loop.adapter.calls) == 1  # the agent was not started
    assert (await loop.task()).ci_fix_attempts == 1
    assert await loop.dispatch() == []
    assert await loop.board() == status.BoardStatus(Column.NEEDS_YOU, "Agent error")


async def test_successful_user_message_restores_auto_fix_with_fresh_attempts(loop: Loop) -> None:
    await loop.start()
    for attempt in range(1, 4):
        await loop.ci("failed")
        await loop.fix(pushes=f"fix-{attempt}")
    await loop.ci("failed")
    assert await loop.dispatch() == []

    loop.pushes.append("by-hand")
    loop.adapter.scripts.append([result()])
    message = await loop.runner.send_message(loop.task_id, "try a different approach")
    await loop.runner.wait(message.id)
    await loop.ci("failed")

    run = await loop.fix(pushes="fix-4")
    assert run.source_head_sha == "by-hand"
    assert (await loop.task()).ci_fix_attempts == 1


async def test_dispatch_stops_between_tasks_when_asked_to(loop: Loop) -> None:
    await loop.start()
    first_task_id = loop.task_id
    await loop.ci("failed")
    await loop.start(pushes="sha-2")
    await loop.ci("failed")
    loop.adapter.scripts.append([result()])
    answers = iter([False, True])

    runs = await loop.dispatcher.dispatch_once(should_stop=lambda: next(answers))

    assert [run.task_id for run in runs] == [first_task_id]
    await loop.runner.wait(runs[0].id)
    assert (await loop.task()).ci_fix_attempts == 0
    assert len(await loop.runs()) == 1


async def comments(loop: Loop, *github_ids: str) -> None:
    """The Poller saw a new comment on the PR."""
    payloads = [
        {"github_id": github_id, "author": "alice", "body": f"fix {github_id}", "ignored": None}
        for github_id in github_ids
    ]
    await db.record_feedback(loop.conn, loop.task_id, payloads)


async def test_comments_are_fed_back_to_the_same_session_in_one_run(loop: Loop) -> None:
    await loop.start()
    await loop.ci("passed")
    await comments(loop, "IC_1", "IC_2")
    assert await loop.board() == status.BoardStatus(Column.WORKING, "Addressing review")

    run = await loop.fix(pushes="sha-2")

    call = loop.adapter.calls[-1]
    assert (run.trigger, run.exit_status) == (RunTrigger.REVIEW_FEEDBACK, ExitStatus.SUCCESS)
    assert call.session_id == "session-1"
    assert "fix IC_1" in call.prompt and "fix IC_2" in call.prompt
    assert (await loop.task()).feedback_attempts == 1


async def test_the_same_comment_is_never_fed_back_twice(loop: Loop) -> None:
    await loop.start()
    await loop.ci("passed")
    await comments(loop, "IC_1")
    await loop.fix(pushes="sha-2")
    await loop.ci("passed")

    assert await loop.dispatch() == []
    assert await loop.board() == status.BoardStatus(Column.READY_TO_MERGE, "Ready")


async def test_comments_seen_in_this_round_wait_for_the_next_one(loop: Loop) -> None:
    await loop.start()
    await loop.ci("passed")
    await comments(loop, "IC_1")

    held = await loop.dispatcher.dispatch_once(hold_feedback={loop.task_id})
    await comments(loop, "IC_2")
    run = await loop.fix(pushes="sha-2")

    assert held == []
    assert "fix IC_1" in run.input_text and "fix IC_2" in run.input_text
    assert len(await loop.runs()) == 2


async def test_comments_are_handled_before_a_failed_ci(loop: Loop) -> None:
    await loop.start()
    await loop.ci("failed")
    await comments(loop, "IC_1")

    run = await loop.fix(pushes=None)
    after = await loop.task()

    assert run.trigger is RunTrigger.REVIEW_FEEDBACK
    assert (after.ci_fix_attempts, after.last_handled_head_sha) == (0, None)
    # Handling the comment produced no new commit: the CI failure on the same commit is then
    # fixed as usual.
    fix = await loop.fix(pushes="sha-2")
    assert fix.trigger is RunTrigger.CI_FAILURE


async def test_addressing_feedback_stops_after_five_rounds_until_a_person_steps_in(
    loop: Loop,
) -> None:
    await loop.start()
    await loop.ci("passed")
    for round_number in range(1, 6):
        await comments(loop, f"IC_{round_number}")
        await loop.fix(pushes=f"sha-{round_number + 1}")
        await loop.ci("passed")
    await comments(loop, "IC_6")

    assert await loop.dispatch() == []
    assert await loop.board() == status.BoardStatus(Column.NEEDS_YOU, "Changes requested")

    loop.adapter.scripts.append([result()])
    message = await loop.runner.send_message(loop.task_id, "carry on")
    await loop.runner.wait(message.id)

    resumed = await loop.fix(pushes=None)
    assert resumed.trigger is RunTrigger.REVIEW_FEEDBACK


async def test_conflict_is_handed_to_the_agent_once_per_commit(loop: Loop) -> None:
    await loop.start()
    await loop.ci("passed", merge_state="conflicting")
    assert await loop.board() == status.BoardStatus(Column.WORKING, "Resolving conflict")

    run = await loop.fix(pushes=None)
    after = await loop.task()

    assert (run.trigger, run.source_head_sha) == (RunTrigger.MERGE_CONFLICT, "sha-1")
    assert (after.last_handled_conflict_head_sha, after.ci_fix_attempts) == ("sha-1", 1)
    # The agent produced no new commit and the conflict remains: stop handling it automatically
    # and wait for a human.
    assert await loop.dispatch() == []
    assert await loop.board() == status.BoardStatus(Column.NEEDS_YOU, "Merge conflict")


async def test_comments_then_conflict_then_failed_ci(loop: Loop) -> None:
    await loop.start()
    await loop.ci("failed", merge_state="conflicting")
    await comments(loop, "IC_1")

    first = await loop.fix(pushes=None)
    second = await loop.fix(pushes="sha-2")

    assert [first.trigger, second.trigger] == [
        RunTrigger.REVIEW_FEEDBACK,
        RunTrigger.MERGE_CONFLICT,
    ]
