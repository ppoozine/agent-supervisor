"""recovery.py 的測試：單一 daemon 的鎖，以及啟動時處理未結束的 run。"""

import asyncio
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import aiosqlite
import pytest

from agent_supervisor import db
from agent_supervisor.enums import EventType, ExitStatus
from agent_supervisor.recovery import (
    AlreadyRunningError,
    InstanceLock,
    SystemProcessTable,
    recover_open_runs,
)


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


# 以下測試操作真的作業系統程序與檔案鎖，用來確認 `pgrep`、訊號與 `flock`
# 在 macOS 與 Linux 上的行為一致（CI 兩個平台都跑）。

_SLEEPER = """
import signal, sys, time
if sys.argv[1] == "ignore":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
print("ready", flush=True)
time.sleep(30)
"""


def start_sleeper(marker: str, *, ignore_interrupt: bool = False) -> subprocess.Popen[str]:
    """啟動一個命令列帶著 marker 的程序，等它準備好（訊號處理已設定）才返回。"""
    mode = "ignore" if ignore_interrupt else "default"
    process = subprocess.Popen(
        [sys.executable, "-c", _SLEEPER, mode, marker], stdout=subprocess.PIPE, text=True
    )
    assert process.stdout is not None
    assert process.stdout.readline() == "ready\n"
    return process


@pytest.fixture
def marker() -> str:
    return f"asv-recovery-probe-{uuid.uuid4().hex}"


@pytest.fixture
def sleepers() -> Iterator[list[subprocess.Popen[str]]]:
    started: list[subprocess.Popen[str]] = []
    yield started
    for process in started:
        process.kill()
        process.wait()


def test_system_table_finds_processes_by_their_command_line(
    marker: str, sleepers: list[subprocess.Popen[str]]
) -> None:
    sleepers.append(start_sleeper(marker))

    assert SystemProcessTable().find(marker) == [sleepers[0].pid]
    assert SystemProcessTable().find(f"{marker}-other") == []


def test_system_table_interrupt_ends_a_process_that_handles_it(
    marker: str, sleepers: list[subprocess.Popen[str]]
) -> None:
    sleepers.append(start_sleeper(marker))
    table = SystemProcessTable()

    table.terminate(sleepers[0].pid, force=False)

    assert sleepers[0].wait(timeout=5) != 0
    assert table.find(marker) == []


def test_system_table_force_ends_a_process_that_ignores_the_interrupt(
    marker: str, sleepers: list[subprocess.Popen[str]]
) -> None:
    sleepers.append(start_sleeper(marker, ignore_interrupt=True))
    table = SystemProcessTable()

    table.terminate(sleepers[0].pid, force=False)
    with pytest.raises(subprocess.TimeoutExpired):
        sleepers[0].wait(timeout=0.3)
    table.terminate(sleepers[0].pid, force=True)

    assert sleepers[0].wait(timeout=5) == -9
    assert table.find(marker) == []


def test_system_table_ignores_a_process_that_is_already_gone(marker: str) -> None:
    process = start_sleeper(marker)
    process.kill()
    process.wait()

    SystemProcessTable().terminate(process.pid, force=True)


async def test_recovery_stops_a_real_leftover_process(
    conn: aiosqlite.Connection, marker: str, sleepers: list[subprocess.Popen[str]]
) -> None:
    sleepers.append(start_sleeper(marker, ignore_interrupt=True))
    run = await open_run(conn, marker)

    # 真正的遺留程序屬於已經結束的 daemon，由 init 回收；這裡由測試自己回收。
    reaping = asyncio.create_task(asyncio.to_thread(sleepers[0].wait))
    report = await recover_open_runs(conn, SystemProcessTable(), grace_seconds=0.3)
    await reaping

    assert report.interrupted_run_ids == [run.id]
    assert SystemProcessTable().find(marker) == []


_LOCK_HOLDER = """
import sys, time
from pathlib import Path
from agent_supervisor.recovery import InstanceLock
lock = InstanceLock.acquire(Path(sys.argv[1]))
print("locked", flush=True)
time.sleep(30)
"""


def test_lock_held_by_another_process_is_refused_and_names_its_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "daemon.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(lock_path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "locked\n"

        with pytest.raises(AlreadyRunningError):
            InstanceLock.acquire(lock_path)
        assert InstanceLock.read_owner_pid(lock_path) == holder.pid
    finally:
        holder.kill()
        holder.wait()

    # 持有者結束後，作業系統會釋放它的鎖。
    InstanceLock.acquire(lock_path).release()
