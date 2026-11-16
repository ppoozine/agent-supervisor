"""db.py 的測試：schema、交易與不變量。"""

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from agent_supervisor import db
from agent_supervisor.enums import EventType, ExitStatus, RunTrigger


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    connection = await db.connect(tmp_path / "test.db")
    await db.init_schema(connection)
    yield connection
    await connection.close()


async def make_task(conn: aiosqlite.Connection) -> tuple[db.Task, db.Run]:
    project = await db.upsert_project(conn, "/repos/demo")
    return await db.create_task_with_initial_run(
        conn,
        project_id=project.id,
        title="demo",
        prompt="do the thing",
        agent_type="fake",
        model=None,
        worktree_path="/repos/demo",
    )


async def test_schema_has_the_four_tables(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    names = {row["name"] for row in await cursor.fetchall()}

    assert {"projects", "tasks", "runs", "events"} <= names


async def test_upsert_project_registers_once_and_updates_settings(
    conn: aiosqlite.Connection,
) -> None:
    first = await db.upsert_project(conn, "/repos/demo")
    second = await db.upsert_project(
        conn, "/repos/demo", github_repo="owner/demo", default_branch="trunk", require_review=True
    )

    assert first.id == second.id
    assert first.name == "demo"
    assert (first.github_repo, first.default_branch, first.require_review) == (None, "main", False)
    assert (second.github_repo, second.default_branch, second.require_review) == (
        "owner/demo",
        "trunk",
        True,
    )
    assert await db.get_project_by_path(conn, "/repos/unknown") is None


async def test_update_task_changes_only_whitelisted_fields(conn: aiosqlite.Connection) -> None:
    task, _ = await make_task(conn)

    await db.update_task(conn, task.id, branch="asv/task-1", pr_number=5, pr_state="open")

    stored = await db.get_task(conn, task.id)
    assert (stored.branch, stored.pr_number, stored.pr_state) == ("asv/task-1", 5, "open")
    with pytest.raises(ValueError, match="title"):
        await db.update_task(conn, task.id, title="renamed")


async def test_archive_task_sets_archived_at(conn: aiosqlite.Connection) -> None:
    task, _ = await make_task(conn)

    await db.archive_task(conn, task.id)

    assert (await db.get_task(conn, task.id)).archived_at is not None


async def test_create_task_also_creates_an_open_initial_run(conn: aiosqlite.Connection) -> None:
    task, run = await make_task(conn)

    assert run.task_id == task.id
    assert run.trigger == RunTrigger.INITIAL
    assert run.input_text == "do the thing"
    assert run.ended_at is None
    assert run.exit_status is None
    assert task.ci_fix_attempts == 0
    assert task.session_id is None


async def test_second_open_run_for_same_task_is_rejected(conn: aiosqlite.Connection) -> None:
    task, _ = await make_task(conn)

    with pytest.raises(db.RunAlreadyActiveError):
        await db.create_run(conn, task.id, RunTrigger.USER_MESSAGE, "again")


async def test_new_run_is_allowed_after_previous_run_finished(conn: aiosqlite.Connection) -> None:
    task, run = await make_task(conn)
    await db.finish_run(conn, run.id, ExitStatus.SUCCESS)

    second = await db.create_run(conn, task.id, RunTrigger.USER_MESSAGE, "again")

    assert second.trigger == RunTrigger.USER_MESSAGE
    assert second.id != run.id


async def test_create_run_is_rejected_for_archived_task(conn: aiosqlite.Connection) -> None:
    task, run = await make_task(conn)
    await db.finish_run(conn, run.id, ExitStatus.SUCCESS)
    await conn.execute("UPDATE tasks SET archived_at = ? WHERE id = ?", (db.utc_now(), task.id))
    await conn.commit()

    with pytest.raises(db.TaskNotRunnableError):
        await db.create_run(conn, task.id, RunTrigger.USER_MESSAGE, "again")


@pytest.mark.parametrize("pr_state", ["merged", "closed"])
async def test_create_run_is_rejected_when_pr_is_finished(
    conn: aiosqlite.Connection, pr_state: str
) -> None:
    task, run = await make_task(conn)
    await db.finish_run(conn, run.id, ExitStatus.SUCCESS)
    await conn.execute("UPDATE tasks SET pr_state = ? WHERE id = ?", (pr_state, task.id))
    await conn.commit()

    with pytest.raises(db.TaskNotRunnableError):
        await db.create_run(conn, task.id, RunTrigger.USER_MESSAGE, "again")


async def test_finish_run_records_result_once(conn: aiosqlite.Connection) -> None:
    _, run = await make_task(conn)

    first = await db.finish_run(
        conn, run.id, ExitStatus.STOPPED, tokens_in=10, tokens_out=2, cost_usd=0.5
    )
    second = await db.finish_run(conn, run.id, ExitStatus.SUCCESS, tokens_in=99)

    stored = await db.get_run(conn, run.id)
    assert first is True
    assert second is False
    assert stored.exit_status == ExitStatus.STOPPED
    assert stored.tokens_in == 10
    assert stored.cost_usd == 0.5
    assert stored.ended_at is not None


async def test_set_task_session_id_is_persisted(conn: aiosqlite.Connection) -> None:
    task, _ = await make_task(conn)

    await db.set_task_session_id(conn, task.id, "session-1")

    assert (await db.get_task(conn, task.id)).session_id == "session-1"


async def test_events_are_appended_in_order_with_json_payload(conn: aiosqlite.Connection) -> None:
    task, run = await make_task(conn)

    await db.append_event(conn, task.id, run.id, EventType.AGENT_MESSAGE, {"text": "hi"})
    await db.append_event(conn, task.id, None, EventType.STATE_CHANGE, {"change": "x"})

    events = await db.list_events(conn, task.id)
    assert [event.type for event in events] == [EventType.AGENT_MESSAGE, EventType.STATE_CHANGE]
    assert events[0].payload == {"text": "hi"}
    assert events[0].run_id == run.id
    assert events[1].run_id is None


async def test_events_cannot_be_updated_or_deleted(conn: aiosqlite.Connection) -> None:
    task, run = await make_task(conn)
    await db.append_event(conn, task.id, run.id, EventType.AGENT_MESSAGE, {"text": "hi"})

    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute("UPDATE events SET type = 'x'")
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute("DELETE FROM events")


async def test_list_open_runs_returns_only_unfinished_runs(conn: aiosqlite.Connection) -> None:
    _, first = await make_task(conn)
    _, second = await make_task(conn)
    await db.finish_run(conn, first.id, ExitStatus.SUCCESS)

    open_runs = await db.list_open_runs(conn)

    assert [run.id for run in open_runs] == [second.id]


async def test_task_usage_sums_cost_and_tokens_across_runs(conn: aiosqlite.Connection) -> None:
    task, first = await make_task(conn)
    await db.finish_run(
        conn, first.id, ExitStatus.SUCCESS, tokens_in=100, tokens_out=10, cost_usd=1.0
    )
    second = await db.create_run(conn, task.id, RunTrigger.USER_MESSAGE, "more")
    await db.finish_run(
        conn, second.id, ExitStatus.SUCCESS, tokens_in=50, tokens_out=5, cost_usd=0.25
    )

    usage = await db.get_task_usage(conn, task.id)

    assert usage.cost_usd == pytest.approx(1.25)
    assert usage.tokens == 165
    assert usage.is_complete is True


async def test_task_usage_is_incomplete_when_a_finished_run_has_unknown_usage(
    conn: aiosqlite.Connection,
) -> None:
    task, first = await make_task(conn)
    await db.finish_run(conn, first.id, ExitStatus.INTERRUPTED)

    usage = await db.get_task_usage(conn, task.id)

    assert usage.tokens == 0
    assert usage.is_complete is False


async def test_last_usage_raw_returns_latest_recorded_usage(conn: aiosqlite.Connection) -> None:
    task, run = await make_task(conn)
    assert await db.get_last_usage_raw(conn, task.id) is None

    await db.append_event(
        conn,
        task.id,
        run.id,
        EventType.STATE_CHANGE,
        {"change": "run_ended", "usage_raw": {"n": 1}},
    )
    await db.append_event(
        conn, task.id, run.id, EventType.STATE_CHANGE, {"change": "run_ended", "usage_raw": None}
    )

    assert await db.get_last_usage_raw(conn, task.id) == {"n": 1}
