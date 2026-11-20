"""Recovery at daemon startup: ensure a single daemon, and handle runs left unfinished.

Querying and terminating OS processes is concentrated in `SystemProcessTable`, so tests can swap it
out wholesale.
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
# How long a process gets to exit after a polite request, and again after a forced one.
GRACE_SECONDS = 5.0


class AlreadyRunningError(RuntimeError):
    """Another daemon is already running."""


class ProcessStillRunningError(RuntimeError):
    """A leftover run's agent process could not be stopped, so the run cannot be finished."""


class InstanceLock:
    """Use a file lock so only one daemon runs at a time. The lock file holds the owner's PID."""

    def __init__(self, file: IO[str]) -> None:
        self._file = file

    @classmethod
    def acquire(cls, path: Path) -> Self:
        """Acquire the lock; raise `AlreadyRunningError` if another process holds it."""
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
        """Read the PID in the lock file; None if the file is missing or its content is invalid."""
        try:
            return int(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    def release(self) -> None:
        """Release the lock."""
        fcntl.flock(self._file, fcntl.LOCK_UN)
        self._file.close()


class ProcessTable(Protocol):
    """Interface for querying and terminating OS processes."""

    def find(self, marker: str) -> list[int]:
        """Return the PIDs of processes whose command line contains marker."""
        ...

    def terminate(self, pid: int, *, force: bool) -> None:
        """Ask a process to exit; terminate it forcibly when `force` is true."""
        ...


class SystemProcessTable:
    """Operate on real processes with `pgrep` and signals."""

    def find(self, marker: str) -> list[int]:
        """Return the PIDs of processes whose command line contains marker, excluding itself."""
        completed = subprocess.run(
            ["pgrep", "-f", "--", marker], capture_output=True, text=True, check=False
        )
        return [int(pid) for pid in completed.stdout.split() if int(pid) != os.getpid()]

    def terminate(self, pid: int, *, force: bool) -> None:
        """Send SIGINT (so the agent wraps up together with its children) or SIGKILL."""
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGINT)
        except ProcessLookupError:
            return


@dataclass
class RecoveryReport:
    """Recovery result: runs marked interrupted, and runs kept because they could not stop."""

    interrupted_run_ids: list[int] = field(default_factory=list)
    held_run_ids: list[int] = field(default_factory=list)


async def recover_open_runs(
    conn: aiosqlite.Connection, processes: ProcessTable, *, grace_seconds: float | None = None
) -> RecoveryReport:
    """Handle unfinished runs left over from the previous startup.

    First stop leftover agent processes; mark a run interrupted only once they are confirmed
    stopped.
    Runs whose processes can't be stopped stay unfinished, so their task can't create new runs.
    Nothing is rerun automatically.
    """
    report = RecoveryReport()
    for run in await db.list_open_runs(conn):
        if await _stop_run_processes(
            conn, run, processes, GRACE_SECONDS if grace_seconds is None else grace_seconds
        ):
            report.held_run_ids.append(run.id)
            continue
        await _finish_interrupted(conn, run, "daemon restarted while the run was active")
        report.interrupted_run_ids.append(run.id)
    return report


async def finish_leftover_run(
    conn: aiosqlite.Connection,
    run: db.Run,
    processes: ProcessTable,
    *,
    grace_seconds: float | None = None,
) -> None:
    """Finish a run left unfinished at startup, at the user's request.

    Its agent process is stopped again first, the same way as at startup: finishing the run while
    the process may still be editing the worktree would let a new run work there at the same time.
    Raise `ProcessStillRunningError` if it still cannot be stopped; the user can stop it by hand and
    try again.
    """
    remaining = await _stop_run_processes(
        conn, run, processes, GRACE_SECONDS if grace_seconds is None else grace_seconds
    )
    if remaining:
        pids = ", ".join(str(pid) for pid in remaining)
        raise ProcessStillRunningError(
            f"the agent process of run {run.id} is still running (pid {pids});"
            " stop it and try again"
        )
    await _finish_interrupted(conn, run, "stopped by the user after the daemon restarted")


async def _stop_run_processes(
    conn: aiosqlite.Connection, run: db.Run, processes: ProcessTable, grace_seconds: float
) -> list[int]:
    """Stop the run's agent processes; return the pids still running (empty when all stopped)."""
    marker = await _process_marker(conn, run)
    if marker is None:
        return []
    return await _stop_processes(processes, marker, grace_seconds)


async def _finish_interrupted(conn: aiosqlite.Connection, run: db.Run, detail: str) -> None:
    await db.finish_run(conn, run.id, ExitStatus.INTERRUPTED)
    await db.append_event(
        conn,
        run.task_id,
        run.id,
        EventType.STATE_CHANGE,
        {
            "change": "run_ended",
            "exit_status": str(ExitStatus.INTERRUPTED),
            "detail": detail,
            "usage_raw": None,
        },
    )


async def _process_marker(conn: aiosqlite.Connection, run: db.Run) -> str | None:
    """Find a string that identifies this run's agent process; None if the agent never started."""
    events = await db.list_events(conn, run.task_id, run.id)
    markers = [
        event.payload.get("process_marker")
        for event in events
        if event.type == EventType.SESSION_STARTED
    ]
    return next((marker for marker in reversed(markers) if marker), None)


async def _stop_processes(processes: ProcessTable, marker: str, grace_seconds: float) -> list[int]:
    """Ask politely to exit, then force-terminate after a timeout. Return the pids still running."""
    for force in (False, True):
        pids = processes.find(marker)
        if not pids:
            return []
        for pid in pids:
            processes.terminate(pid, force=force)
        if await _wait_until_gone(processes, marker, grace_seconds):
            return []
    return processes.find(marker)


async def _wait_until_gone(processes: ProcessTable, marker: str, grace_seconds: float) -> bool:
    deadline = asyncio.get_running_loop().time() + grace_seconds
    while processes.find(marker):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(POLL_SECONDS)
    return True
