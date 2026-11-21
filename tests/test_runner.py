"""Runner tests: run lifecycle, limits, stopping, and locks. All use the fake adapter."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest

from agent_supervisor import db
from agent_supervisor.adapters.base import PermissionPolicy, SandboxLevel
from agent_supervisor.config import Config
from agent_supervisor.enums import EventType, ExitStatus, RunTrigger
from agent_supervisor.runner import (
    BudgetExhaustedError,
    EventWriter,
    NoActiveRunError,
    Runner,
    UnknownAgentError,
)
from tests.fake_adapter import FULL_CAPABILITIES, FakeAdapter, message, result, session_started


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    connection = await db.connect(tmp_path / "test.db")
    await db.init_schema(connection)
    yield connection
    await connection.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path)


WORK_DIR = "/work/dir"


def make_runner(
    conn: aiosqlite.Connection, config: Config, adapter: FakeAdapter, **kwargs: object
) -> Runner:
    async def prepare(task: db.Task, run: db.Run, record: object) -> None:
        await db.update_task(conn, task.id, worktree_path=WORK_DIR)

    kwargs.setdefault("prepare", prepare)
    return Runner(conn, {"fake": adapter}, config, **kwargs)  # type: ignore[arg-type]


async def start(runner: Runner, conn: aiosqlite.Connection, prompt: str = "do it") -> db.Run:
    project = await db.upsert_project(conn, "/repos/demo")
    return await runner.start_task(
        project_id=project.id, title="demo", prompt=prompt, agent_type="fake", model=None
    )


async def test_successful_run_records_events_and_usage(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter([[session_started(), message("working"), result()]])
    runner = make_runner(conn, config, adapter)

    run = await runner.wait((await start(runner, conn)).id)

    assert run.exit_status == ExitStatus.SUCCESS
    assert run.ended_at is not None
    assert (run.tokens_in, run.tokens_out, run.cost_usd) == (100, 10, 0.5)
    events = await db.list_events(conn, run.task_id)
    assert [event.type for event in events] == [
        EventType.SESSION_STARTED,
        EventType.AGENT_MESSAGE,
        EventType.STATE_CHANGE,
    ]
    assert events[1].payload["text"] == "working"
    assert all(event.run_id == run.id for event in events)


async def test_run_end_event_keeps_raw_usage(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter([[session_started(), result(usage_raw={"cumulative": 7})]])
    runner = make_runner(conn, config, adapter)

    run = await runner.wait((await start(runner, conn)).id)

    last_event = (await db.list_events(conn, run.task_id))[-1]
    assert last_event.payload["change"] == "run_ended"
    assert last_event.payload["exit_status"] == "success"
    assert last_event.payload["usage_raw"] == {"cumulative": 7}


async def test_session_id_is_saved_as_soon_as_it_is_known(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    gate = asyncio.Event()
    adapter = FakeAdapter([[session_started("early"), gate, result(session_id="early")]])
    runner = make_runner(conn, config, adapter)

    run = await start(runner, conn)
    await _until(lambda: _session_id(conn, run.task_id), "early")
    still_running = (await db.get_run(conn, run.id)).ended_at is None
    gate.set()
    await runner.wait(run.id)

    assert still_running is True


async def test_stream_without_result_is_an_error(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter([[session_started(), message("then nothing")]])
    runner = make_runner(conn, config, adapter)

    run = await runner.wait((await start(runner, conn)).id)

    assert run.exit_status == ExitStatus.ERROR
    assert run.tokens_in is None


async def test_adapter_exception_is_an_error_with_detail(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter([[session_started(), RuntimeError("agent exploded")]])
    runner = make_runner(conn, config, adapter)

    run = await runner.wait((await start(runner, conn)).id)

    last_event = (await db.list_events(conn, run.task_id))[-1]
    assert run.exit_status == ExitStatus.ERROR
    assert "agent exploded" in last_event.payload["detail"]


async def test_limit_reported_by_adapter_ends_run_as_limit_reached(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter([[session_started(), result(ExitStatus.LIMIT_REACHED)]])
    runner = make_runner(conn, config, adapter)

    run = await runner.wait((await start(runner, conn)).id)

    assert run.exit_status == ExitStatus.LIMIT_REACHED
    assert run.tokens_in == 100


async def test_stop_ends_run_as_stopped_and_closes_the_adapter(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter([[session_started(), asyncio.Event(), result()]])
    runner = make_runner(conn, config, adapter)
    run = await start(runner, conn)
    await _until(lambda: _session_id(conn, run.task_id), "session-1")

    await runner.stop(run.task_id)

    stored = await db.get_run(conn, run.id)
    assert stored.exit_status == ExitStatus.STOPPED
    assert stored.ended_at is not None
    assert adapter.closed_count == 1


async def test_stop_without_active_run_is_rejected(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter([[session_started(), result()]])
    runner = make_runner(conn, config, adapter)
    run = await runner.wait((await start(runner, conn)).id)

    with pytest.raises(NoActiveRunError):
        await runner.stop(run.task_id)


async def test_run_exceeding_time_limit_ends_as_limit_reached(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter([[session_started(), asyncio.Event(), result()]])
    runner = make_runner(conn, replace(config, run_timeout_seconds=0.05), adapter)

    run = await runner.wait((await start(runner, conn)).id)

    assert run.exit_status == ExitStatus.LIMIT_REACHED
    assert adapter.closed_count == 1
    assert "time limit" in (await db.list_events(conn, run.task_id))[-1].payload["detail"]


async def test_message_resumes_the_saved_session_with_previous_usage(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter(
        [
            [session_started("s-1"), result(session_id="s-1", usage_raw={"cumulative": 3})],
            [session_started("s-1"), result(session_id="s-1")],
        ]
    )
    runner = make_runner(conn, config, adapter)
    first = await runner.wait((await start(runner, conn)).id)

    second = await runner.wait((await runner.send_message(first.task_id, "one more thing")).id)

    assert second.trigger == RunTrigger.USER_MESSAGE
    assert second.exit_status == ExitStatus.SUCCESS
    assert adapter.calls[0].session_id is None
    assert adapter.calls[0].previous_usage is None
    assert adapter.calls[1].session_id == "s-1"
    assert adapter.calls[1].prompt == "one more thing"
    assert adapter.calls[1].previous_usage == {"cumulative": 3}


async def test_message_is_rejected_while_a_run_is_active(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    gate = asyncio.Event()
    adapter = FakeAdapter([[session_started(), gate, result()]])
    runner = make_runner(conn, config, adapter)
    run = await start(runner, conn)

    with pytest.raises(db.RunAlreadyActiveError):
        await runner.send_message(run.task_id, "too early")

    gate.set()
    await runner.wait(run.id)
    assert len(await db.list_runs(conn, run.task_id)) == 1


async def test_limits_carry_max_turns_and_remaining_budget(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter(
        [[session_started(), result(cost_usd=1.5)], [session_started(), result(cost_usd=0.1)]]
    )
    runner = make_runner(
        conn, replace(config, task_cost_limit_usd=4.0, max_turns_per_run=7), adapter
    )
    first = await runner.wait((await start(runner, conn)).id)

    await runner.wait((await runner.send_message(first.task_id, "more")).id)

    assert adapter.calls[0].limits.max_turns == 7
    assert adapter.calls[0].limits.max_budget_usd == pytest.approx(4.0)
    assert adapter.calls[1].limits.max_budget_usd == pytest.approx(2.5)


async def test_limits_are_omitted_when_adapter_cannot_enforce_them(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    capabilities = replace(FULL_CAPABILITIES, can_limit_turns=False, can_limit_budget=False)
    adapter = FakeAdapter([[session_started(), result()]], capabilities=capabilities)
    runner = make_runner(conn, config, adapter)

    await runner.wait((await start(runner, conn)).id)

    assert adapter.calls[0].limits.max_turns is None
    assert adapter.calls[0].limits.max_budget_usd is None


async def test_message_is_rejected_when_cost_budget_is_exhausted(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter([[session_started(), result(cost_usd=2.0)]])
    runner = make_runner(conn, replace(config, task_cost_limit_usd=2.0), adapter)
    first = await runner.wait((await start(runner, conn)).id)

    with pytest.raises(BudgetExhaustedError):
        await runner.send_message(first.task_id, "more")

    assert len(await db.list_runs(conn, first.task_id)) == 1


async def test_message_is_rejected_when_token_budget_is_exhausted(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter(
        [[session_started(), result(tokens_in=900, tokens_out=100, cost_usd=None)]]
    )
    runner = make_runner(conn, replace(config, task_token_limit=1000), adapter)
    first = await runner.wait((await start(runner, conn)).id)

    with pytest.raises(BudgetExhaustedError):
        await runner.send_message(first.task_id, "more")


async def test_policy_from_config_is_passed_to_the_adapter(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    policy = PermissionPolicy(sandbox_level=SandboxLevel.READ_ONLY, allow_network=False)
    adapter = FakeAdapter([[session_started(), result()]])
    runner = make_runner(conn, replace(config, permission=policy), adapter)

    await runner.wait((await start(runner, conn)).id)

    assert adapter.calls[0].policy == policy
    assert adapter.calls[0].cwd == Path(WORK_DIR)


async def test_unknown_agent_type_is_rejected_before_creating_anything(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    runner = make_runner(conn, config, FakeAdapter([]))

    project = await db.upsert_project(conn, "/repos/demo")

    with pytest.raises(UnknownAgentError):
        await runner.start_task(
            project_id=project.id, title="demo", prompt="x", agent_type="nope", model=None
        )

    cursor = await conn.execute("SELECT COUNT(*) AS n FROM tasks")
    assert (await cursor.fetchone())["n"] == 0


async def test_finalize_runs_only_after_a_successful_agent_turn(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    finalized: list[int] = []

    async def finalize(task: db.Task, run: db.Run, record: object) -> None:
        finalized.append(run.id)

    adapter = FakeAdapter(
        [[session_started(), result()], [session_started(), result(ExitStatus.ERROR)]]
    )
    runner = make_runner(conn, config, adapter, finalize=finalize)
    first = await runner.wait((await start(runner, conn)).id)
    second = await runner.wait((await runner.send_message(first.task_id, "again")).id)

    assert finalized == [first.id]
    assert second.exit_status == ExitStatus.ERROR


async def test_finalize_failure_turns_the_run_into_an_error_but_keeps_usage(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    async def finalize(task: db.Task, run: db.Run, record: object) -> None:
        raise RuntimeError("push rejected")

    adapter = FakeAdapter([[session_started(), result()]])
    runner = make_runner(conn, config, adapter, finalize=finalize)

    run = await runner.wait((await start(runner, conn)).id)

    assert run.exit_status == ExitStatus.ERROR
    assert run.tokens_in == 100
    assert "push rejected" in (await db.list_events(conn, run.task_id))[-1].payload["detail"]


async def test_prepare_failure_ends_the_run_as_error_without_starting_the_agent(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    async def prepare(task: db.Task, run: db.Run, record: object) -> None:
        raise RuntimeError("worktree failed")

    adapter = FakeAdapter([[session_started(), result()]])
    runner = make_runner(conn, config, adapter, prepare=prepare)

    run = await runner.wait((await start(runner, conn)).id)

    assert run.exit_status == ExitStatus.ERROR
    assert adapter.calls == []


async def leftover_run(conn: aiosqlite.Connection) -> db.Run:
    """A run still open from before a restart: no job in this Runner is running it."""
    project = await db.upsert_project(conn, "/repos/demo")
    _, run = await db.create_task_with_initial_run(
        conn, project_id=project.id, title="t", prompt="p", agent_type="fake", model=None
    )
    return run


async def test_stop_hands_a_leftover_run_to_be_finished(
    conn: aiosqlite.Connection, config: Config
) -> None:
    run = await leftover_run(conn)
    handed: list[int] = []

    async def finish_leftover(leftover: db.Run) -> None:
        handed.append(leftover.id)

    runner = make_runner(conn, config, FakeAdapter([]), finish_leftover=finish_leftover)

    await runner.stop(run.task_id)

    assert handed == [run.id]


async def test_automatic_stop_leaves_a_leftover_run_alone(
    conn: aiosqlite.Connection, config: Config
) -> None:
    # Stopping because the PR closed must not block the poller on a process it may not stop.
    run = await leftover_run(conn)
    handed: list[int] = []

    async def finish_leftover(leftover: db.Run) -> None:
        handed.append(leftover.id)

    runner = make_runner(conn, config, FakeAdapter([]), finish_leftover=finish_leftover)

    assert await runner.stop_if_running(run.task_id) is False
    assert handed == []


async def test_stop_during_finalize_does_not_become_success(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    entered = asyncio.Event()

    async def finalize(task: db.Task, run: db.Run, record: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    adapter = FakeAdapter([[session_started(), result()]])
    runner = make_runner(conn, config, adapter, finalize=finalize)
    run = await start(runner, conn)
    await entered.wait()

    await runner.stop(run.task_id)

    stored = await db.get_run(conn, run.id)
    assert stored.exit_status == ExitStatus.STOPPED
    assert stored.tokens_in == 100


async def test_shutdown_marks_active_runs_as_interrupted(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter([[session_started(), asyncio.Event(), result()]])
    runner = make_runner(conn, config, adapter)
    run = await start(runner, conn)
    await _until(lambda: _session_id(conn, run.task_id), "session-1")

    await runner.shutdown()

    assert (await db.get_run(conn, run.id)).exit_status == ExitStatus.INTERRUPTED
    assert adapter.closed_count == 1


async def test_on_event_callback_receives_each_recorded_event(
    conn: aiosqlite.Connection, config: Config, tmp_path: Path
) -> None:
    seen: list[str] = []
    adapter = FakeAdapter([[session_started(), message("hello"), result()]])
    runner = make_runner(
        conn, config, adapter, on_event=lambda event_type, payload: seen.append(str(event_type))
    )

    await runner.wait((await start(runner, conn)).id)

    assert seen == ["session_started", "agent_message", "state_change"]


async def _session_id(conn: aiosqlite.Connection, task_id: int) -> str | None:
    return (await db.get_task(conn, task_id)).session_id


async def _until(read: object, expected: object) -> None:
    """Wait until an async reader returns the expected value; fail the test on timeout."""
    async with asyncio.timeout(2.0):
        # We are watching a value in the database; there is no event to await, so poll.
        while await read() != expected:  # type: ignore[operator]  # noqa: ASYNC110
            await asyncio.sleep(0.005)


async def test_run_without_a_working_directory_is_an_error(
    conn: aiosqlite.Connection, config: Config
) -> None:
    async def prepare(task: db.Task, run: db.Run, record: object) -> None:
        return None

    adapter = FakeAdapter([[session_started(), result()]])
    runner = make_runner(conn, config, adapter, prepare=prepare)

    run = await runner.wait((await start(runner, conn)).id)

    assert run.exit_status == ExitStatus.ERROR
    assert adapter.calls == []
    assert "working directory" in (await db.list_events(conn, run.task_id))[-1].payload["detail"]


async def test_events_written_by_a_stage_are_recorded_for_the_run(
    conn: aiosqlite.Connection, config: Config
) -> None:
    async def finalize(task: db.Task, run: db.Run, record: EventWriter) -> None:
        await record(EventType.PUSHED, {"branch": "asv/task-1", "head_sha": "abc"})

    adapter = FakeAdapter([[session_started(), result()]])
    runner = make_runner(conn, config, adapter, finalize=finalize)

    run = await runner.wait((await start(runner, conn)).id)

    events = await db.list_events(conn, run.task_id)
    assert [event.type for event in events][-2:] == [EventType.PUSHED, EventType.STATE_CHANGE]
    assert events[-2].run_id == run.id
    assert events[-2].payload["head_sha"] == "abc"


async def test_archive_runs_the_archive_stage_then_marks_the_task(
    conn: aiosqlite.Connection, config: Config
) -> None:
    archived: list[int] = []

    async def archive(task: db.Task) -> None:
        archived.append(task.id)

    adapter = FakeAdapter([[session_started(), result()]])
    runner = make_runner(conn, config, adapter, archive=archive)
    run = await runner.wait((await start(runner, conn)).id)

    await runner.archive(run.task_id)

    assert archived == [run.task_id]
    assert (await db.get_task(conn, run.task_id)).archived_at is not None
    with pytest.raises(db.TaskNotRunnableError):
        await runner.send_message(run.task_id, "too late")


async def test_archive_is_rejected_while_a_run_is_active(
    conn: aiosqlite.Connection, config: Config
) -> None:
    gate = asyncio.Event()
    archived: list[int] = []

    async def archive(task: db.Task) -> None:
        archived.append(task.id)

    adapter = FakeAdapter([[session_started(), gate, result()]])
    runner = make_runner(conn, config, adapter, archive=archive)
    run = await start(runner, conn)

    with pytest.raises(db.RunAlreadyActiveError):
        await runner.archive(run.task_id)

    gate.set()
    await runner.wait(run.id)
    assert archived == []
    assert (await db.get_task(conn, run.task_id)).archived_at is None


async def test_task_is_not_archived_when_the_archive_stage_fails(
    conn: aiosqlite.Connection, config: Config
) -> None:
    async def archive(task: db.Task) -> None:
        raise RuntimeError("commit hook rejected the changes")

    adapter = FakeAdapter([[session_started(), result()]])
    runner = make_runner(conn, config, adapter, archive=archive)
    run = await runner.wait((await start(runner, conn)).id)

    with pytest.raises(RuntimeError, match="commit hook"):
        await runner.archive(run.task_id)

    assert (await db.get_task(conn, run.task_id)).archived_at is None


async def test_message_cannot_start_a_run_while_the_task_is_being_archived(
    conn: aiosqlite.Connection, config: Config
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def archive(task: db.Task) -> None:
        entered.set()
        await release.wait()

    adapter = FakeAdapter([[session_started(), result()], [session_started(), result()]])
    runner = make_runner(conn, config, adapter, archive=archive)
    run = await runner.wait((await start(runner, conn)).id)
    archiving = asyncio.create_task(runner.archive(run.task_id))
    await entered.wait()

    sending = asyncio.create_task(runner.send_message(run.task_id, "racing the archive"))
    await asyncio.sleep(0.01)
    release.set()
    await archiving
    done, _ = await asyncio.wait({sending})

    assert isinstance(sending.exception(), db.TaskNotRunnableError)
    assert len(await db.list_runs(conn, run.task_id)) == 1
    assert done == {sending}


async def test_stop_right_after_the_run_is_created_still_closes_it(
    conn: aiosqlite.Connection, config: Config
) -> None:
    adapter = FakeAdapter([[asyncio.Event(), result()]])
    runner = make_runner(conn, config, adapter)

    run = await start(runner, conn)
    await runner.stop(run.task_id)  # the task that owns this run has not started yet

    assert (await db.get_run(conn, run.id)).exit_status == ExitStatus.STOPPED


async def test_run_that_is_already_being_cancelled_is_not_cancelled_again(
    conn: aiosqlite.Connection, config: Config
) -> None:
    """A second stop after the first waiter is cancelled mid-stop must not cut the cleanup short."""
    cleaning_up, may_finish = asyncio.Event(), asyncio.Event()
    cleaned_up: list[bool] = []

    async def prepare(task: db.Task, run: db.Run, record: EventWriter) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning_up.set()
            await may_finish.wait()
            cleaned_up.append(True)
            raise

    runner = make_runner(conn, config, FakeAdapter([]), prepare=prepare)
    run = await start(runner, conn)

    async with asyncio.timeout(5.0):
        first_stop = asyncio.create_task(runner.stop(run.task_id))
        await cleaning_up.wait()
        first_stop.cancel()
        await asyncio.wait({first_stop})
        second_stop = asyncio.create_task(runner.stop(run.task_id))
        await asyncio.sleep(0.02)
        may_finish.set()
        await second_stop

    assert cleaned_up == [True]
    assert (await db.get_run(conn, run.id)).exit_status == ExitStatus.STOPPED
