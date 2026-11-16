"""執行外部指令的共用底層。逾時或被取消時一定會把子程序終止。

各個外部工具的指令（`git`、`gh`）仍集中在各自的模組，這裡只負責執行。
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class CommandResult:
    """一次指令執行的結果。"""

    returncode: int
    stdout: str
    stderr: str


class CommandTimeoutError(RuntimeError):
    """指令執行超過時間上限，已被終止。"""


async def run_command(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109  # 逾時後要先終止子程序才能拋出
) -> CommandResult:
    """執行一個外部指令並等它結束；不檢查結束代碼，由呼叫端決定如何處理。"""
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    payload = None if input_text is None else input_text.encode()
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(payload), timeout)
    except TimeoutError as error:
        await _kill(process)
        raise CommandTimeoutError(f"{args[0]} did not finish within {timeout:g}s") from error
    except asyncio.CancelledError:
        await _kill(process)
        raise
    assert process.returncode is not None
    return CommandResult(
        process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    )


async def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.kill()
    await process.wait()
