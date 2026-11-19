"""db.py 的測試：schema、交易與不變量。"""

import asyncio
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


async def with_failed_ci(conn: aiosqlite.Connection, head_sha: str = "sha-1") -> db.Task:
    """建立一個任務：initial run 已成功、PR 開著、CI 在 head_sha 上失敗。"""
    task, run = await make_task(conn)
    await db.finish_run(conn, run.id, ExitStatus.SUCCESS, tokens_in=1, tokens_out=1, cost_usd=0.1)
    await db.update_task(
        conn, task.id, pr_number=7, pr_state="open", head_sha=head_sha, ci_state="failed"
    )
    return await db.get_task(conn, task.id)


async def test_ci_failure_run_records_the_commit_and_counts_the_attempt(
    conn: aiosqlite.Connection,
) -> None:
    task = await with_failed_ci(conn)

    run = await db.create_ci_failure_run(conn, task.id, "fix it", condition=lambda _: True)

    assert run is not None
    assert (run.trigger, run.source_head_sha) == (RunTrigger.CI_FAILURE, "sha-1")
    after = await db.get_task(conn, task.id)
    assert (after.last_handled_head_sha, after.ci_fix_attempts) == ("sha-1", 1)


async def test_ci_failure_run_is_not_created_when_the_condition_fails(
    conn: aiosqlite.Connection,
) -> None:
    task = await with_failed_ci(conn)
    seen: list[db.TaskSnapshot] = []

    def condition(snapshot: db.TaskSnapshot) -> bool:
        seen.append(snapshot)
        return False

    run = await db.create_ci_failure_run(conn, task.id, "fix it", condition=condition)

    assert run is None
    assert seen[0].task.id == task.id and not seen[0].has_open_run
    after = await db.get_task(conn, task.id)
    assert (after.last_handled_head_sha, after.ci_fix_attempts) == (None, 0)
    assert len(await db.list_runs(conn, task.id)) == 1


async def test_ci_failure_run_leaves_the_counters_alone_when_a_run_is_already_open(
    conn: aiosqlite.Connection,
) -> None:
    task = await with_failed_ci(conn)
    await db.create_run(conn, task.id, RunTrigger.USER_MESSAGE, "hold on")

    with pytest.raises(aiosqlite.IntegrityError):
        await db.create_ci_failure_run(conn, task.id, "fix it", condition=lambda _: True)

    after = await db.get_task(conn, task.id)
    assert (after.last_handled_head_sha, after.ci_fix_attempts) == (None, 0)


async def test_user_message_resets_the_fix_attempts(conn: aiosqlite.Connection) -> None:
    task = await with_failed_ci(conn)
    fix = await db.create_ci_failure_run(conn, task.id, "fix it", condition=lambda _: True)
    assert fix is not None
    await db.finish_run(conn, fix.id, ExitStatus.SUCCESS)
    assert (await db.get_task(conn, task.id)).ci_fix_attempts == 1

    await db.create_run(conn, task.id, RunTrigger.USER_MESSAGE, "try another way")

    assert (await db.get_task(conn, task.id)).ci_fix_attempts == 0


async def test_github_state_is_written_as_a_group_with_one_event(
    conn: aiosqlite.Connection,
) -> None:
    task = await with_failed_ci(conn, "sha-1")

    changed = await db.apply_github_state(
        conn,
        task.id,
        expected_head_sha="sha-1",
        pr_state="open",
        head_sha="sha-1",
        ci_state="passed",
        review_state="approved",
    )

    assert changed
    after = await db.get_task(conn, task.id)
    assert (after.ci_state, after.review_state) == ("passed", "approved")
    events = [event for event in await db.list_events(conn, task.id) if event.type == "ci_status"]
    assert len(events) == 1
    assert events[0].run_id is None
    assert events[0].payload == {
        "pr_number": 7,
        "pr_state": "open",
        "head_sha": "sha-1",
        "ci_state": "passed",
        "review_state": "approved",
    }


async def test_github_state_is_discarded_when_the_head_moved_during_the_query(
    conn: aiosqlite.Connection,
) -> None:
    task = await with_failed_ci(conn, "sha-1")
    await db.update_task(conn, task.id, head_sha="sha-2", ci_state="pending")

    changed = await db.apply_github_state(
        conn,
        task.id,
        expected_head_sha="sha-1",
        pr_state="open",
        head_sha="sha-1",
        ci_state="failed",
        review_state="none",
    )

    assert not changed
    after = await db.get_task(conn, task.id)
    assert (after.head_sha, after.ci_state) == ("sha-2", "pending")
    assert [event.type for event in await db.list_events(conn, task.id)] == []


async def test_unchanged_github_state_writes_no_event(conn: aiosqlite.Connection) -> None:
    task = await with_failed_ci(conn, "sha-1")

    changed = await db.apply_github_state(
        conn,
        task.id,
        expected_head_sha="sha-1",
        pr_state="open",
        head_sha="sha-1",
        ci_state="failed",
        review_state="none",
    )

    assert not changed
    assert await db.list_events(conn, task.id) == []


async def test_snapshot_reports_the_last_run_and_usage(conn: aiosqlite.Connection) -> None:
    task = await with_failed_ci(conn)
    await db.create_run(conn, task.id, RunTrigger.USER_MESSAGE, "more")

    snapshot = await db.get_snapshot(conn, task.id)

    assert snapshot.has_open_run
    assert snapshot.last_run is not None and snapshot.last_run.trigger == RunTrigger.USER_MESSAGE
    assert snapshot.usage.cost_usd == pytest.approx(0.1)
    assert snapshot.require_review is False


async def test_open_pull_requests_are_polled_even_when_archived(conn: aiosqlite.Connection) -> None:
    open_task = await with_failed_ci(conn)
    merged, _ = await make_task(conn)
    await db.update_task(conn, merged.id, pr_number=8, pr_state="merged")
    no_pr, _ = await make_task(conn)
    archived, run = await make_task(conn)
    await db.update_task(conn, archived.id, pr_number=9, pr_state="open")
    await db.finish_run(conn, run.id, ExitStatus.SUCCESS)
    await db.archive_task(conn, archived.id)

    polled = await db.list_tasks_with_open_pr(conn)

    assert [task.id for task in polled] == [open_task.id, archived.id]
    assert no_pr.id not in [task.id for task in polled]


async def test_a_concurrent_write_cannot_commit_half_of_another_transaction(
    conn: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """另一個工作的寫入不能把做到一半的交易提前 commit。

    讓交易在寫入 run 之後、更新任務之前失敗，期間另一個工作寫了一筆事件。
    兩者沒有被寫入鎖隔開的話，那筆事件的 commit 會把寫到一半的 run 一起存進去。
    """
    task = await with_failed_ci(conn)
    insert_run = db._insert_run
    other_write: list[asyncio.Task[int]] = []

    async def insert_then_fail(*args: object) -> int:
        await insert_run(*args)  # type: ignore[arg-type]
        other_write.append(
            asyncio.create_task(db.append_event(conn, task.id, None, EventType.CI_STATUS, {}))
        )
        for _ in range(20):  # 讓另一個工作有機會執行
            await asyncio.sleep(0.001)
        raise RuntimeError("failed halfway")

    monkeypatch.setattr(db, "_insert_run", insert_then_fail)

    with pytest.raises(RuntimeError, match="failed halfway"):
        await db.create_ci_failure_run(conn, task.id, "fix it", condition=lambda _: True)
    await other_write[0]

    assert len(await db.list_runs(conn, task.id)) == 1
    assert len(await db.list_events(conn, task.id)) == 1


OLD_TASKS_TABLE = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, title TEXT NOT NULL,
    prompt TEXT NOT NULL, agent_type TEXT NOT NULL, model TEXT, session_id TEXT, branch TEXT,
    worktree_path TEXT, pr_number INTEGER, pr_state TEXT, head_sha TEXT,
    ci_state TEXT NOT NULL DEFAULT 'none', review_state TEXT NOT NULL DEFAULT 'none',
    last_handled_head_sha TEXT, ci_fix_attempts INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO tasks (id, project_id, title, prompt, agent_type, created_at, updated_at)
VALUES (1, 1, 'old task', 'do it', 'fake', 't', 't');
"""


async def test_database_from_an_older_version_gains_the_new_columns(tmp_path: Path) -> None:
    connection = await db.connect(tmp_path / "old.db")
    await connection.executescript(OLD_TASKS_TABLE)
    await connection.commit()

    await db.init_schema(connection)
    await db.init_schema(connection)
    task = await db.get_task(connection, 1)
    await connection.close()

    assert (task.title, task.last_handled_feedback_event_id, task.feedback_attempts) == (
        "old task",
        None,
        0,
    )


def comment(github_id: str, **fields: object) -> dict[str, object]:
    return {"github_id": github_id, "author": "alice", "body": "fix it", "ignored": None, **fields}


async def test_a_comment_is_recorded_once_and_ignored_ones_do_not_wait(
    conn: aiosqlite.Connection,
) -> None:
    task, _ = await make_task(conn)

    first = await db.record_feedback(
        conn, task.id, [comment("IC_1"), comment("IC_2", ignored="bot")]
    )
    second = await db.record_feedback(conn, task.id, [comment("IC_1"), comment("IC_3")])

    pending = await db.list_pending_feedback(conn, task.id)
    assert (first, second) == (1, 1)
    assert [event.payload["github_id"] for event in pending] == ["IC_1", "IC_3"]
    assert await db.list_feedback_ids(conn, task.id) == {"IC_1", "IC_2", "IC_3"}
    assert (await db.get_snapshot(conn, task.id)).pending_feedback == 2


async def test_feedback_run_takes_every_waiting_comment_exactly_once(
    conn: aiosqlite.Connection,
) -> None:
    task, run = await make_task(conn)
    await db.finish_run(conn, run.id, ExitStatus.SUCCESS)
    await db.record_feedback(conn, task.id, [comment("IC_1"), comment("IC_2")])

    def build(events: list[db.Event]) -> str:
        return ",".join(event.payload["github_id"] for event in events)

    created = await db.create_feedback_run(conn, task.id, build_prompt=build, condition=bool)
    assert created is not None
    await db.finish_run(conn, created.id, ExitStatus.SUCCESS)
    again = await db.create_feedback_run(conn, task.id, build_prompt=build, condition=bool)

    after = await db.get_task(conn, task.id)
    assert (created.trigger, created.input_text) == (RunTrigger.REVIEW_FEEDBACK, "IC_1,IC_2")
    assert again is None
    assert after.feedback_attempts == 1
    assert await db.list_pending_feedback(conn, task.id) == []


async def test_feedback_run_is_not_created_when_the_condition_fails(
    conn: aiosqlite.Connection,
) -> None:
    task, run = await make_task(conn)
    await db.finish_run(conn, run.id, ExitStatus.SUCCESS)
    await db.record_feedback(conn, task.id, [comment("IC_1")])

    created = await db.create_feedback_run(
        conn, task.id, build_prompt=lambda events: "x", condition=lambda snapshot: False
    )

    assert created is None
    assert len(await db.list_pending_feedback(conn, task.id)) == 1
    assert (await db.get_task(conn, task.id)).feedback_attempts == 0


async def test_user_message_resets_the_feedback_attempts(conn: aiosqlite.Connection) -> None:
    task, run = await make_task(conn)
    await db.finish_run(conn, run.id, ExitStatus.SUCCESS)
    await db.record_feedback(conn, task.id, [comment("IC_1")])
    created = await db.create_feedback_run(
        conn, task.id, build_prompt=lambda events: "x", condition=bool
    )
    assert created is not None
    await db.finish_run(conn, created.id, ExitStatus.SUCCESS)

    await db.create_run(conn, task.id, RunTrigger.USER_MESSAGE, "carry on")

    assert (await db.get_task(conn, task.id)).feedback_attempts == 0
