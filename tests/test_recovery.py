"""recovery.py 的測試：單一 daemon 的鎖，以及啟動時處理未結束的 run。"""

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from agent_supervisor import db
from agent_supervisor.enums import EventType, ExitStatus
from agent_supervisor.recovery import AlreadyRunningError, InstanceLock, recover_open_runs


class FakeProcessTable:
    """假的程序表：記錄被要求終止的程序，並依設定決定它們會不會消失。"""

    def __init__(self, processes: dict[str, list[int]], *, survives_force: bool = False) -> None:
        self.processes = processes
        self.survives_force = survives_force
        self.terminated: list[tuple[int, bool]] = []

    def find(self, marker: str) -> list[int]:
        return list(self.processes.get(marker, []))

    def terminate(self, pid: int, *, force: bool) -> None:
        self.terminated.append((pid, force))
        if force and self.survives_force:
            return
        if force or pid % 2 == 0:
            for pids in self.processes.values():
                if pid in pids:
                    pids.remove(pid)


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    connection = await db.connect(tmp_path / "test.db")
    await db.init_schema(connection)
    yield connection
    await connection.close()


async def open_run(conn: aiosqlite.Connection, marker: str | None) -> db.Run:
    project = await db.upsert_project(conn, "/repos/demo")
    task, run = await db.create_task_with_initial_run(
        conn,
        project_id=project.id,
        title="t",
        prompt="p",
        agent_type="fake",
        model=None,
        worktree_path="/repos/demo",
    )
    if marker is not None:
        await db.append_event(
            conn,
            task.id,
            run.id,
            EventType.SESSION_STARTED,
            {"session_id": marker, "process_marker": marker},
        )
    return run


def test_second_instance_cannot_acquire_the_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "daemon.lock"
    first = InstanceLock.acquire(lock_path)

    with pytest.raises(AlreadyRunningError):
        InstanceLock.acquire(lock_path)

    first.release()


def test_lock_can_be_acquired_again_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "daemon.lock"
    InstanceLock.acquire(lock_path).release()

    second = InstanceLock.acquire(lock_path)

    assert InstanceLock.read_owner_pid(lock_path) is not None
    second.release()


async def test_open_run_without_leftover_process_is_marked_interrupted(
    conn: aiosqlite.Connection,
) -> None:
    run = await open_run(conn, "marker-1")

    report = await recover_open_runs(conn, FakeProcessTable({}), grace_seconds=0.05)

    stored = await db.get_run(conn, run.id)
    assert stored.exit_status == ExitStatus.INTERRUPTED
    assert stored.ended_at is not None
    assert report.interrupted_run_ids == [run.id]
    assert report.held_run_ids == []
    last_event = (await db.list_events(conn, run.task_id))[-1]
    assert last_event.type == EventType.STATE_CHANGE
    assert last_event.payload["exit_status"] == "interrupted"


async def test_leftover_process_is_terminated_before_run_is_marked(
    conn: aiosqlite.Connection,
) -> None:
    run = await open_run(conn, "marker-1")
    processes = FakeProcessTable({"marker-1": [42]})

    report = await recover_open_runs(conn, processes, grace_seconds=0.05)

    assert processes.terminated == [(42, False)]
    assert processes.find("marker-1") == []
    assert report.interrupted_run_ids == [run.id]


async def test_process_ignoring_polite_termination_is_killed(conn: aiosqlite.Connection) -> None:
    run = await open_run(conn, "marker-1")
    processes = FakeProcessTable({"marker-1": [43]})

    report = await recover_open_runs(conn, processes, grace_seconds=0.05)

    assert processes.terminated == [(43, False), (43, True)]
    assert report.interrupted_run_ids == [run.id]


async def test_run_stays_open_when_leftover_process_cannot_be_stopped(
    conn: aiosqlite.Connection,
) -> None:
    run = await open_run(conn, "marker-1")
    processes = FakeProcessTable({"marker-1": [43]}, survives_force=True)

    report = await recover_open_runs(conn, processes, grace_seconds=0.05)

    assert (await db.get_run(conn, run.id)).ended_at is None
    assert report.held_run_ids == [run.id]
    assert report.interrupted_run_ids == []


async def test_run_that_never_started_an_agent_is_marked_interrupted(
    conn: aiosqlite.Connection,
) -> None:
    run = await open_run(conn, None)
    processes = FakeProcessTable({})

    report = await recover_open_runs(conn, processes, grace_seconds=0.05)

    assert processes.terminated == []
    assert report.interrupted_run_ids == [run.id]


async def test_finished_runs_are_left_alone(conn: aiosqlite.Connection) -> None:
    run = await open_run(conn, "marker-1")
    await db.finish_run(conn, run.id, ExitStatus.SUCCESS)

    report = await recover_open_runs(conn, FakeProcessTable({"marker-1": [42]}), grace_seconds=0.05)

    assert report.interrupted_run_ids == []
    assert (await db.get_run(conn, run.id)).exit_status == ExitStatus.SUCCESS
