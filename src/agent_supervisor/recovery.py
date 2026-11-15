"""Daemon 啟動時的復原：確保只有一個 daemon，並處理上次留下的未結束 run。

對作業系統程序的查詢與終止集中在 `SystemProcessTable`，測試時可以整個換掉。
"""

import asyncio
import fcntl
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol, Self

import aiosqlite

from agent_supervisor import db
from agent_supervisor.enums import EventType, ExitStatus

POLL_SECONDS = 0.02


class AlreadyRunningError(RuntimeError):
    """已經有另一個 daemon 在執行。"""


class InstanceLock:
    """用檔案鎖確保同一時間只有一個 daemon。鎖檔內容是持有者的程序編號。"""

    def __init__(self, file: IO[str]) -> None:
        self._file = file

    @classmethod
    def acquire(cls, path: Path) -> Self:
        """取得鎖；已被其他程序持有時拋出 `AlreadyRunningError`。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        file = path.open("a+")
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            file.close()
            raise AlreadyRunningError(f"another instance holds {path}") from error
        file.seek(0)
        file.truncate()
        file.write(str(os.getpid()))
        file.flush()
        return cls(file)

    @staticmethod
    def read_owner_pid(path: Path) -> int | None:
        """讀出鎖檔記錄的程序編號；沒有鎖檔或內容不合法時回傳 None。"""
        try:
            return int(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    def release(self) -> None:
        """釋放鎖。"""
        fcntl.flock(self._file, fcntl.LOCK_UN)
        self._file.close()


class ProcessTable(Protocol):
    """查詢與終止作業系統程序的介面。"""

    def find(self, marker: str) -> list[int]:
        """回傳命令列包含 marker 的程序編號。"""
        ...

    def terminate(self, pid: int, *, force: bool) -> None:
        """要求程序結束；`force` 為真時強制終止。"""
        ...


class SystemProcessTable:
    """用 `pgrep` 與訊號操作真正的程序。"""

    def find(self, marker: str) -> list[int]:
        """回傳命令列包含 marker 的程序編號，不含自己。"""
        completed = subprocess.run(
            ["pgrep", "-f", "--", marker], capture_output=True, text=True, check=False
        )
        return [int(pid) for pid in completed.stdout.split() if int(pid) != os.getpid()]

    def terminate(self, pid: int, *, force: bool) -> None:
        """送出 SIGINT（讓 agent 連同子程序一起收尾）或 SIGKILL。"""
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGINT)
        except ProcessLookupError:
            return


@dataclass
class RecoveryReport:
    """復原的結果：哪些 run 已標成 interrupted，哪些因為程序停不掉而保留。"""

    interrupted_run_ids: list[int] = field(default_factory=list)
    held_run_ids: list[int] = field(default_factory=list)


async def recover_open_runs(
    conn: aiosqlite.Connection, processes: ProcessTable, *, grace_seconds: float = 5.0
) -> RecoveryReport:
    """處理上次啟動留下的未結束 run。

    先停掉遺留的 agent 程序，確認停了才把 run 標成 interrupted。
    停不掉的 run 維持未結束，該任務因此建不了新的 run。不自動重跑。
    """
    report = RecoveryReport()
    for run in await db.list_open_runs(conn):
        marker = await _process_marker(conn, run)
        if marker is not None and not await _stop_processes(processes, marker, grace_seconds):
            report.held_run_ids.append(run.id)
            continue
        await db.finish_run(conn, run.id, ExitStatus.INTERRUPTED)
        await db.append_event(
            conn,
            run.task_id,
            run.id,
            EventType.STATE_CHANGE,
            {
                "change": "run_ended",
                "exit_status": str(ExitStatus.INTERRUPTED),
                "detail": "daemon restarted while the run was active",
                "usage_raw": None,
            },
        )
        report.interrupted_run_ids.append(run.id)
    return report


async def _process_marker(conn: aiosqlite.Connection, run: db.Run) -> str | None:
    """找出這個 run 的 agent 程序可以用什麼字串辨識；agent 還沒啟動過則回傳 None。"""
    events = await db.list_events(conn, run.task_id, run.id)
    markers = [
        event.payload.get("process_marker")
        for event in events
        if event.type == EventType.SESSION_STARTED
    ]
    return next((marker for marker in reversed(markers) if marker), None)


async def _stop_processes(processes: ProcessTable, marker: str, grace_seconds: float) -> bool:
    """先客氣地要求結束，逾時再強制終止。回傳是否確認全部結束。"""
    for force in (False, True):
        pids = processes.find(marker)
        if not pids:
            return True
        for pid in pids:
            processes.terminate(pid, force=force)
        if await _wait_until_gone(processes, marker, grace_seconds):
            return True
    return False


async def _wait_until_gone(processes: ProcessTable, marker: str, grace_seconds: float) -> bool:
    deadline = asyncio.get_running_loop().time() + grace_seconds
    while processes.find(marker):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(POLL_SECONDS)
    return True
