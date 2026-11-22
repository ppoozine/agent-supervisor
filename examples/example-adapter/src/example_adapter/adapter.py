"""An agent-supervisor adapter around the fake agent in `example_adapter.agent`.

It is written the way an adapter for a real agent CLI would be: start one process per turn, put a
unique marker on its command line, turn each line it prints into an event, and make sure the
process is gone whenever the turn ends early. See docs/guide/adapters.md in the agent-supervisor
repository for what each part of the interface means.
"""

import asyncio
import json
import os
import signal
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from agent_supervisor.adapters.base import (
    AdapterCapabilities,
    AgentEvent,
    PermissionPolicy,
    RunLimits,
    SandboxLevel,
    TurnResult,
)
from agent_supervisor.enums import EventType, ExitStatus
from agent_supervisor.tool_status import AuthKind, ToolStatus

STOP_GRACE_SECONDS = 5.0
STDERR_TAIL_CHARS = 500


class ExampleAdapter:
    """Run the fake example agent; needs no account and spends nothing."""

    display_name = "Example agent"
    capabilities = AdapterCapabilities(
        # The agent keeps its own session state and accepts `--resume`.
        can_resume_session=True,
        # It has no options for limits, so none can be enforced while it runs; the Runner's run
        # time limit and the task's token limit still apply.
        can_limit_turns=False,
        can_limit_budget=False,
        # Its actions cannot be approved one by one, and it cannot be kept off the network.
        can_gate_tool_calls=False,
        can_restrict_network=False,
        # It reports tokens but not money.
        reports_cost=False,
    )

    def __init__(self, *, stop_grace_seconds: float = STOP_GRACE_SECONDS) -> None:
        self._stop_grace_seconds = stop_grace_seconds

    async def check(self) -> ToolStatus:
        """Always usable: the fake agent ships with this package and needs no sign-in."""
        try:
            installed_version: str | None = version("agent-supervisor-example-adapter")
        except PackageNotFoundError:
            installed_version = None
        return ToolStatus(
            installed=True,
            version=installed_version,
            auth=AuthKind.OTHER,
            auth_label="nothing; no account needed",
        )

    async def run_turn(
        self,
        *,
        prompt: str,
        cwd: Path,
        session_id: str | None,
        model: str | None,
        limits: RunLimits,
        policy: PermissionPolicy,
        previous_usage: dict[str, Any] | None,
    ) -> AsyncIterator[AgentEvent | TurnResult]:
        """Run one turn of the fake agent.

        `model` and `limits` are unused: the fake agent has neither. `previous_usage` is unused
        too, since the agent reports each turn's own token counts rather than session totals.
        """
        # Fixed before the process starts and put on its command line, so that after a restart
        # agent-supervisor can find the process with `pgrep -f` and stop it. Emit it first: if the
        # daemon dies right after starting the process, the marker must already be recorded.
        marker = f"example-agent-{uuid.uuid4().hex}"
        yield _session_event(session_id, marker)

        arguments = [sys.executable, "-m", "example_adapter.agent", "--marker", marker]
        if session_id is not None:
            arguments += ["--resume", session_id]
        if policy.sandbox_level is SandboxLevel.READ_ONLY:
            arguments.append("--read-only")
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Its own process group: Ctrl-C in the daemon's terminal must not reach the agent, and
            # stopping it can then signal the whole group.
            start_new_session=True,
        )
        assert process.stdin is not None and process.stdout is not None
        assert process.stderr is not None
        # Read stderr at the same time, or the agent blocks once that pipe fills up.
        reading_stderr = asyncio.create_task(process.stderr.read())
        current_session = session_id
        tokens: tuple[int, int] | None = None
        try:
            process.stdin.write(prompt.encode())
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.drain()
            process.stdin.close()

            async for line in process.stdout:
                message = _parse(line)
                if message is None:
                    continue
                if message.get("type") == "session":
                    current_session = str(message["id"])
                    yield _session_event(current_session, marker)
                elif message.get("type") == "done":
                    tokens = (int(message["input_tokens"]), int(message["output_tokens"]))
                else:
                    event = _to_event(message)
                    if event is not None:
                        yield event

            returncode = await process.wait()
            stderr = (await reading_stderr).decode(errors="replace")
            yield _result(current_session, returncode, tokens, stderr)
        finally:
            # The stream is closed early when the run is stopped or times out: the agent and
            # anything it started must be gone before this returns.
            await _stop(process, self._stop_grace_seconds)
            reading_stderr.cancel()


def _session_event(session_id: str | None, marker: str) -> AgentEvent:
    return AgentEvent(
        EventType.SESSION_STARTED, {"session_id": session_id, "process_marker": marker}
    )


def _parse(line: bytes) -> dict[str, Any] | None:
    """One line of output; anything that is not a JSON object (e.g. a warning) is skipped."""
    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _to_event(message: dict[str, Any]) -> AgentEvent | None:
    """Map one agent message to an event, using the payload fields the board knows how to show."""
    kind = message.get("type")
    finished = message.get("status") == "finished"
    phase = "finished" if finished else "started"
    if kind == "message":
        return AgentEvent(EventType.AGENT_MESSAGE, {"kind": "message", "text": message["text"]})
    if kind == "command":
        payload: dict[str, Any] = {
            "phase": phase,
            "tool_use_id": message["id"],
            "tool": "shell",
            "command": message["command"],
        }
        if finished:
            payload |= {
                "exit_code": message["exit_code"],
                "is_error": message["exit_code"] != 0,
                "output": message["output"],
            }
        return AgentEvent(EventType.COMMAND, payload)
    if kind == "file":
        payload = {
            "phase": phase,
            "tool_use_id": message["id"],
            "tool": "write_file",
            "input": {"path": message["path"]},
        }
        if finished:
            payload["is_error"] = False
        return AgentEvent(EventType.TOOL_CALL, payload)
    return None


def _result(
    session_id: str | None, returncode: int, tokens: tuple[int, int] | None, stderr: str
) -> TurnResult:
    tokens_in, tokens_out = tokens if tokens is not None else (None, None)
    finished = returncode == 0 and tokens is not None
    detail = None if finished else f"agent exited with {returncode}: {stderr[-STDERR_TAIL_CHARS:]}"
    return TurnResult(
        session_id=session_id,
        exit_status=ExitStatus.SUCCESS if finished else ExitStatus.ERROR,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        # The agent reports no money, so cost is unknown, not zero.
        cost_usd=None,
        usage_raw=None
        if tokens is None
        else {"input_tokens": tokens_in, "output_tokens": tokens_out},
        detail=detail,
    )


async def _stop(process: asyncio.subprocess.Process, grace_seconds: float) -> None:
    """Ask the agent to stop, then kill whatever is left of its process group."""
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.send_signal(signal.SIGINT)
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), grace_seconds)
    # Even once the agent itself has exited, children it started may still be running in its
    # group: kill the whole group every time.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), grace_seconds)
