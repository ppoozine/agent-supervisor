"""The task shell: a real `/bin/sh` on a pty, so these run the same on macOS and Linux CI."""

import asyncio
import contextlib
import os
import re
import signal
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent_supervisor import shell as shell_module
from agent_supervisor.shell import Closed, Lagged, Listener, Shell, ShellManager

pytestmark = pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs /bin/sh")


def resolved(path: Path) -> Path:
    """The path with symlinks resolved (macOS's /tmp is /private/tmp), as `pwd` prints it."""
    return path.resolve()


async def start_sh(cwd: Path, **kwargs: object) -> Shell:
    return await Shell.start(cwd, command="/bin/sh", **kwargs)  # type: ignore[arg-type]


@pytest.fixture
async def manager() -> AsyncIterator[ShellManager]:
    made = ShellManager(start=start_sh)
    yield made
    await made.close_all()


async def read_until(listener: Listener, pattern: str, timeout: float = 10.0) -> str:  # noqa: ASYNC109
    """Collect output until `pattern` (a regex) matches it; return everything collected."""
    seen = ""
    async with asyncio.timeout(timeout):
        while not re.search(pattern, seen):
            item = await listener.get()
            if isinstance(item, Closed):
                raise AssertionError(f"shell closed ({item}) before {pattern!r}; got {seen!r}")
            seen += item.decode(errors="replace")
    return seen


async def wait_until_gone(pid: int, timeout: float = 5.0) -> None:  # noqa: ASYNC109
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"process {pid} is still running")


async def test_runs_commands_in_the_worktree(manager: ShellManager, tmp_path: Path) -> None:
    shell = await manager.open(7, tmp_path)
    output = shell.subscribe()

    shell.write(b"echo sum-$((1+1)); pwd\n")

    seen = await read_until(output, r"sum-2[\s\S]*" + re.escape(resolved(tmp_path).name))
    assert str(resolved(tmp_path)) in seen.replace("\r", "")


async def test_ctrl_c_interrupts_the_foreground_command(
    manager: ShellManager, tmp_path: Path
) -> None:
    shell = await manager.open(7, tmp_path)
    output = shell.subscribe()
    shell.write(b"echo started-$((1+1)); sleep 30\n")
    await read_until(output, r"started-2")
    await asyncio.sleep(0.2)  # let sleep start after the echo

    shell.write(b"\x03")
    shell.write(b"echo after-$((2+2))\n")

    # Without the interrupt, the next command would wait for the 30 second sleep.
    await read_until(output, r"after-4", timeout=5.0)


async def test_resizing_reaches_the_terminal(manager: ShellManager, tmp_path: Path) -> None:
    shell = await manager.open(7, tmp_path)
    output = shell.subscribe()

    shell.resize(40, 100)
    shell.write(b"stty size\n")

    await read_until(output, r"40 100")


async def test_one_shell_per_task_and_late_pages_see_recent_output(
    manager: ShellManager, tmp_path: Path
) -> None:
    shell = await manager.open(7, tmp_path)
    first = shell.subscribe()
    shell.write(b"echo early-$((3+3))\n")
    await read_until(first, r"early-6")

    again = await manager.open(7, tmp_path)

    assert again is shell
    assert "early-6" in again.scrollback.decode()


async def test_closing_ends_the_shell_and_its_foreground_job(
    manager: ShellManager, tmp_path: Path
) -> None:
    shell = await manager.open(7, tmp_path)
    output = shell.subscribe()
    shell.write(b"sh -c 'echo CHILD:$$; exec sleep 300'\n")
    child = int(re.findall(r"CHILD:(\d+)", await read_until(output, r"CHILD:\d+"))[-1])

    assert await manager.close(7, "archived")

    assert await output.get() == Closed("archived", None)
    await wait_until_gone(child)
    await wait_until_gone(shell.pid)
    assert manager.get(7) is None


async def test_a_shell_that_exits_tells_its_pages_and_the_next_open_starts_a_new_one(
    manager: ShellManager, tmp_path: Path
) -> None:
    shell = await manager.open(7, tmp_path)
    output = shell.subscribe()

    shell.write(b"exit 3\n")

    async with asyncio.timeout(5.0):
        while not isinstance(item := await output.get(), Closed):
            pass
    assert item == Closed("exited", 3)
    await asyncio.wait_for(shell.ended.wait(), 5.0)
    assert manager.get(7) is None
    replacement = await manager.open(7, tmp_path)
    assert replacement is not shell


async def test_a_large_paste_does_not_block(manager: ShellManager, tmp_path: Path) -> None:
    shell = await manager.open(7, tmp_path)
    output = shell.subscribe()
    shell.write(b"cat > /dev/null\n")
    await asyncio.sleep(0.2)

    started = time.monotonic()
    shell.write((b"x" * 99 + b"\n") * 3_000)  # 300 KB of pasted lines

    assert time.monotonic() - started < 0.5
    shell.write(b"\x04")  # end of input for cat
    shell.write(b"echo pasted-$((5+5))\n")
    await read_until(output, r"pasted-10", timeout=20.0)


async def test_closing_every_shell_on_shutdown(manager: ShellManager, tmp_path: Path) -> None:
    shells = [await manager.open(task_id, tmp_path) for task_id in (1, 2)]

    await manager.close_all()

    for shell in shells:
        await wait_until_gone(shell.pid)


async def test_a_shell_that_exits_is_noticed_even_if_a_background_job_holds_the_terminal(
    manager: ShellManager, tmp_path: Path
) -> None:
    shell = await manager.open(7, tmp_path)
    output = shell.subscribe()
    # Interactive bash expands "!" from history; turn that off first (dash has no -H).
    shell.write(b"set +H 2>/dev/null\n")
    shell.write(b"sleep 100 & echo BG:$!; exit 4\n")
    background = int(re.findall(r"BG:(\d+)", await read_until(output, r"BG:\d+"))[-1])
    try:
        async with asyncio.timeout(5.0):
            while not isinstance(item := await output.get(), Closed):
                pass
        assert item == Closed("exited", 4)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(background, signal.SIGKILL)


async def test_a_page_that_falls_behind_is_dropped_instead_of_growing_memory(
    manager: ShellManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_module, "LISTENER_BACKLOG", 4)
    shell = await manager.open(7, tmp_path)
    slow = shell.subscribe()  # never read while output pours in

    shell.write(b"yes | head -c 2000000; echo done-$((1+2))\n")
    keeping_up = shell.subscribe()
    await read_until(keeping_up, r"done-3", timeout=20.0)

    items = [slow.get_nowait() for _ in range(slow.qsize())]
    assert items == [Lagged()]
