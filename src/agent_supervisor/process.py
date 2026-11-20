"""Shared base for running external commands. Always kills the child on timeout or cancel.

Commands for each external tool (`git`, `gh`) still live in their own modules; this module only runs
them.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class CommandResult:
    """The result of one command execution."""

    returncode: int
    stdout: str
    stderr: str


class CommandTimeoutError(RuntimeError):
    """The command exceeded its time limit and was terminated."""


async def run_command(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109  # kill the child before raising
) -> CommandResult:
    """Run an external command and wait for it; the exit code is left for the caller to check."""
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
