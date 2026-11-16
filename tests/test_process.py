"""process.py 的測試：執行外部指令、逾時與取消時的清理。"""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from agent_supervisor.process import CommandTimeoutError, run_command


async def test_output_and_exit_code_are_captured(tmp_path: Path) -> None:
    result = await run_command(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ],
        cwd=tmp_path,
    )

    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (3, "out", "err")


async def test_input_text_is_passed_on_stdin() -> None:
    result = await run_command(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input_text="hello"
    )

    assert result.stdout.strip() == "HELLO"


async def test_command_exceeding_timeout_is_killed() -> None:
    marker = "asv-timeout-probe"

    with pytest.raises(CommandTimeoutError):
        await run_command(
            [sys.executable, "-c", f"import time; time.sleep(30)  # {marker}"], timeout=0.2
        )

    assert _count(marker) == 0


async def test_cancelled_command_is_killed() -> None:
    marker = "asv-cancel-probe"
    running = asyncio.create_task(
        run_command([sys.executable, "-c", f"import time; time.sleep(30)  # {marker}"])
    )
    async with asyncio.timeout(5):
        while _count(marker) == 0:  # noqa: ASYNC110  # 等外部程序出現，沒有事件可等
            await asyncio.sleep(0.02)

    running.cancel()
    await asyncio.wait({running})

    assert running.cancelled()
    assert _count(marker) == 0


def _count(marker: str) -> int:
    found = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, check=False)
    return len(found.stdout.split())
