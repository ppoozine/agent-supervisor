"""A terminal in a task's worktree, for the person to run commands in (decision 173).

The shell never controls the agent: it does not write to the agent's session or go through an
adapter. Each task has at most one shell; it keeps running while nobody is watching, so the page
can reconnect and see recent output. It ends on Restart, when the task is archived, when the
daemon stops, or when the shell exits by itself.

The shell runs in a pseudo-terminal whose slave side is its controlling terminal, so Ctrl-C and
job control work. Making it the controlling terminal needs `login_tty()` in the child; a short
Python launcher does that and then execs the shell, instead of `preexec_fn`, which the
`subprocess` docs call unsafe in a process with threads.
"""

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
import pty
import signal
import struct
import sys
import termios
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SCROLLBACK_BYTES = 256 * 1024
READ_CHUNK_BYTES = 64 * 1024
DEFAULT_SIZE = (24, 80)
# A page that falls this many chunks behind is dropped; it reconnects and gets the scrollback.
LISTENER_BACKLOG = 512
# After a hangup, how long the shell gets to exit before it is killed.
HANGUP_GRACE_SECONDS = 2.0
DEFAULT_SHELL = "/bin/sh"

# Run in the child: make the pty (stdin) the controlling terminal of a new session, then become
# the shell.
_LAUNCHER = "import os, sys; os.login_tty(0); os.execvp(sys.argv[1], sys.argv[1:])"


class ShellClosedError(RuntimeError):
    """The shell has ended; nothing more can be written to it."""


@dataclass(frozen=True)
class Closed:
    """Sent to listeners when the shell ends. `reason`: exited, restarted, archived, shutdown."""

    reason: str
    exit_code: int | None


@dataclass(frozen=True)
class Lagged:
    """Sent instead of output to a listener that fell too far behind; it has been dropped."""


Listener = asyncio.Queue[bytes | Closed | Lagged]


def default_shell() -> str:
    """The user's login shell from `$SHELL`, or `/bin/sh`."""
    return os.environ.get("SHELL") or DEFAULT_SHELL


class Shell:
    """One shell process on a pty: output fans out to listeners and is kept as scrollback."""

    def __init__(self, process: asyncio.subprocess.Process, master_fd: int, name: str) -> None:
        self.name = name
        self._process = process
        self._master = master_fd
        self._loop = asyncio.get_running_loop()
        self._scrollback = bytearray()
        self._listeners: set[Listener] = set()
        self._pending = bytearray()  # input not yet accepted by the pty
        self._closed: Closed | None = None
        self._exit_task: asyncio.Task[None] | None = None
        self.ended = asyncio.Event()
        os.set_blocking(master_fd, False)
        self._loop.add_reader(master_fd, self._on_readable)
        # The pty only reaches end-of-file when nothing holds it any more; a background job the
        # shell left behind keeps it open, so also watch the shell process itself.
        self._watcher = self._loop.create_task(self._watch_process())

    @classmethod
    async def start(
        cls,
        cwd: Path,
        *,
        command: str | None = None,
        env: Mapping[str, str] | None = None,
        size: tuple[int, int] = DEFAULT_SIZE,
    ) -> "Shell":
        """Start `command` (default: the user's shell) in `cwd` on a new pty."""
        program = command or default_shell()
        master, slave = pty.openpty()
        try:
            _set_size(master, *size)
            shell_env = {**os.environ, **(env or {}), "TERM": "xterm-256color"}
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _LAUNCHER,
                program,
                cwd=cwd,
                env=shell_env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        return cls(process, master, Path(program).name)

    @property
    def pid(self) -> int:
        """Process id of the shell, which is also its session and process group id."""
        return self._process.pid

    @property
    def scrollback(self) -> bytes:
        """The most recent output, for a page that connects late."""
        return bytes(self._scrollback)

    def subscribe(self) -> Listener:
        """Receive output from now on; a `Closed` item ends the stream."""
        listener: Listener = asyncio.Queue(LISTENER_BACKLOG)
        if self._closed is not None:
            listener.put_nowait(self._closed)
        else:
            self._listeners.add(listener)
        return listener

    def unsubscribe(self, listener: Listener) -> None:
        """Stop receiving output; the shell keeps running."""
        self._listeners.discard(listener)

    def write(self, data: bytes) -> None:
        """Send keystrokes or pasted text. Never blocks: what the pty cannot take yet waits."""
        if self._closed is not None:
            raise ShellClosedError("the shell has ended")
        if not self._pending:
            written = self._try_write(data)
            data = data[written:]
            if not data:
                return
            self._loop.add_writer(self._master, self._flush_pending)
        self._pending += data

    def resize(self, rows: int, cols: int) -> None:
        """Change the terminal size; the kernel tells the foreground job with SIGWINCH."""
        if self._closed is None:
            _set_size(self._master, rows, cols)

    async def close(self, reason: str) -> None:
        """End the shell and everything in its foreground: hang up, then kill if needed."""
        if self._closed is None:
            # A hangup goes to the shell, which passes it on to its jobs; closing the pty then
            # hangs up the terminal, which reaches whatever job is in the foreground.
            _signal_group(self.pid, signal.SIGHUP)
            self._finish(reason)
        try:
            await asyncio.wait_for(self._process.wait(), HANGUP_GRACE_SECONDS)
        except TimeoutError:
            _signal_group(self.pid, signal.SIGKILL)
            await self._process.wait()
        self.ended.set()

    def _on_readable(self) -> None:
        try:
            data = os.read(self._master, READ_CHUNK_BYTES)
        except BlockingIOError:
            return
        except OSError as error:
            # EIO: every process holding the slave side has gone, i.e. the shell exited.
            if error.errno != errno.EIO:
                logger.warning("reading the shell's terminal failed: %s", error)
            data = b""
        if not data:
            if self._exit_task is None:
                self._exit_task = self._loop.create_task(self._exited())
            return
        self._scrollback += data
        if len(self._scrollback) > SCROLLBACK_BYTES:
            del self._scrollback[: len(self._scrollback) - SCROLLBACK_BYTES]
        for listener in list(self._listeners):
            try:
                listener.put_nowait(data)
            except asyncio.QueueFull:
                self._drop(listener)

    def _drop(self, listener: Listener) -> None:
        """Stop feeding a listener that cannot keep up, so memory stays bounded."""
        self._listeners.discard(listener)
        # What it has not read is stale anyway: on reconnecting it gets the scrollback.
        while not listener.empty():
            listener.get_nowait()
        listener.put_nowait(Lagged())

    async def _watch_process(self) -> None:
        await self._process.wait()
        if self._exit_task is None:
            self._exit_task = asyncio.current_task()
            await self._exited()

    async def _exited(self) -> None:
        if self._closed is not None:
            return
        self._finish("exited")
        await self._process.wait()
        self._closed = Closed("exited", self._process.returncode)
        for listener in self._listeners:
            _put_last(listener, self._closed)
        self._listeners.clear()
        self.ended.set()

    def _finish(self, reason: str) -> None:
        """Stop reading and writing, close the pty and tell the listeners."""
        self._loop.remove_reader(self._master)
        if self._pending:
            self._loop.remove_writer(self._master)
            self._pending.clear()
        with contextlib.suppress(OSError):
            os.close(self._master)
        self._closed = Closed(reason, None)
        if reason != "exited":
            for listener in self._listeners:
                _put_last(listener, self._closed)
            self._listeners.clear()

    def _try_write(self, data: bytes) -> int:
        try:
            return os.write(self._master, data)
        except BlockingIOError:
            return 0

    def _flush_pending(self) -> None:
        written = self._try_write(bytes(self._pending))
        del self._pending[:written]
        if not self._pending:
            self._loop.remove_writer(self._master)


class ShellManager:
    """The daemon's shells, at most one per task."""

    def __init__(self, start: Callable[..., Awaitable["Shell"]] | None = None) -> None:
        self._shells: dict[int, Shell] = {}
        self._lock = asyncio.Lock()
        self._start = start or Shell.start

    async def open(self, task_id: int, cwd: Path, size: tuple[int, int] = DEFAULT_SIZE) -> Shell:
        """The task's shell, started in `cwd` if there is none or the last one has exited."""
        async with self._lock:
            shell = self._shells.get(task_id)
            if shell is None or shell.ended.is_set():
                shell = await self._start(cwd, size=size)
                self._shells[task_id] = shell
                logger.info("started a shell for task %s (pid %s)", task_id, shell.pid)
            return shell

    def get(self, task_id: int) -> Shell | None:
        """The task's shell if it is still running."""
        shell = self._shells.get(task_id)
        return None if shell is None or shell.ended.is_set() else shell

    async def close(self, task_id: int, reason: str) -> bool:
        """End the task's shell; return whether there was one running."""
        async with self._lock:
            shell = self._shells.pop(task_id, None)
        if shell is None:
            return False
        await shell.close(reason)
        return True

    async def close_all(self) -> None:
        """End every shell, e.g. when the daemon stops."""
        async with self._lock:
            shells = list(self._shells.values())
            self._shells.clear()
        await asyncio.gather(*(shell.close("shutdown") for shell in shells))


def _put_last(listener: Listener, item: Closed) -> None:
    """Give a listener its final item, discarding unread output if its queue is full."""
    while listener.full():
        listener.get_nowait()
    listener.put_nowait(item)


def _set_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _signal_group(pgid: int, signal_number: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal_number)
