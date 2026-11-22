"""Codex adapter: wraps the non-interactive `codex exec` in a subprocess and reads JSONL events.

See `docs/design/codex-notes.md` for usage and what testing showed. All strings and parameters
specific to Codex appear only in this file.
"""

import asyncio
import json
import os
import signal
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from agent_supervisor.adapters.base import (
    AdapterCapabilities,
    AgentEvent,
    PermissionPolicy,
    RunLimits,
    TurnResult,
)
from agent_supervisor.enums import EventType, ExitStatus
from agent_supervisor.process import CommandTimeoutError, run_command
from agent_supervisor.tool_status import (
    AuthKind,
    CommandRunner,
    ToolMissingError,
    ToolStatus,
    read_version,
    run_check,
)

DEFAULT_EXECUTABLE = ("codex",)
INSTALL_COMMAND = "npm install -g @openai/codex"
SIGN_IN_COMMANDS = (
    "codex login",
    "export CODEX_API_KEY=<your API key>  # or this; then start agsv serve from this shell",
)
COMMAND_ITEM = "command_execution"
ERROR_ITEM = "error"
MESSAGE_KINDS = {"agent_message": "message", "reasoning": "reasoning"}
MAX_OUTPUT_CHARS = 20_000
# One event per line; a command's entire output sits on a single line, so the per-line limit
# must be larger than asyncio's default of 64 KB.
MAX_LINE_BYTES = 32 * 1024 * 1024
INTERRUPT_GRACE_SECONDS = 10.0
STDERR_TAIL_CHARS = 500


class CodexAdapter:
    """Start one `codex exec` process per turn."""

    display_name = "Codex"

    capabilities = AdapterCapabilities(
        can_resume_session=True,
        # `codex exec` has no options for turn or cost limits, and only reports token counts.
        can_limit_turns=False,
        can_limit_budget=False,
        # Non-interactive mode cannot approve calls one by one; only the sandbox level and network
        # access can be enforced.
        can_gate_tool_calls=False,
        can_restrict_network=True,
        reports_cost=False,
    )

    def __init__(
        self,
        *,
        executable: Sequence[str] = DEFAULT_EXECUTABLE,
        scratch_dir: Path | None = None,
        interrupt_grace_seconds: float = INTERRUPT_GRACE_SECONDS,
        command_runner: CommandRunner = run_command,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._executable = tuple(executable)
        self._scratch_dir = scratch_dir or Path(tempfile.gettempdir())
        self._interrupt_grace_seconds = interrupt_grace_seconds
        self._run = command_runner
        self._environ = os.environ if environ is None else environ

    async def check(self) -> ToolStatus:
        """Check the `codex` CLI and how it is signed in.

        `codex login status` only reads the saved login; it does not call the API.
        """
        version, problem = await read_version(self._run, self._executable, fix=(INSTALL_COMMAND,))
        if problem is not None:
            return problem
        if "CODEX_API_KEY" in self._environ:
            # `codex exec` uses it over the saved login, but `codex login status` does not show it.
            return ToolStatus(
                installed=True, version=version, auth=AuthKind.API_KEY, auth_label="CODEX_API_KEY"
            )
        try:
            result = await run_check(self._run, [*self._executable, "login", "status"])
        except (ToolMissingError, CommandTimeoutError):
            return ToolStatus(
                installed=True, version=version, detail="could not read `codex login status`"
            )
        auth, label = codex_auth(result.returncode, result.stdout + result.stderr)
        if auth is AuthKind.NONE:
            return ToolStatus(
                installed=True, version=version, detail="not signed in", fix=SIGN_IN_COMMANDS
            )
        return ToolStatus(installed=True, version=version, auth=auth, auth_label=label)

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
        """Run one turn. `limits` is unused: Codex has no limits that can be enforced mid-run."""
        # Codex's session id is only known after it starts, so prepare a separate string fixed
        # before start so restart recovery can find the process: putting it in the file name of
        # the "last message" output file makes it appear on the command line.
        # The file lives outside the worktree so it doesn't get committed during wrap-up.
        marker = f"asv-codex-{uuid.uuid4().hex}"
        last_message_path = self._scratch_dir / f"{marker}.txt"
        arguments = _build_arguments(session_id, cwd, model, policy, last_message_path)
        yield _session_event(session_id, marker)

        process = await asyncio.create_subprocess_exec(
            *self._executable,
            *arguments,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_LINE_BYTES,
            # Put it in its own session so Ctrl-C in the terminal doesn't send the signal straight
            # to Codex; Runner decides when to stop and how to record the result.
            start_new_session=True,
        )
        assert process.stdin is not None and process.stdout is not None
        assert process.stderr is not None
        # Drain stderr concurrently, otherwise Codex blocks once the pipe fills up.
        reading_stderr = asyncio.create_task(process.stderr.read())
        turn = _Turn(session_id, marker, previous_usage)
        try:
            await _send_prompt(process.stdin, prompt)
            async for line in process.stdout:
                for event in turn.handle(_parse_line(line)):
                    yield event
            returncode = await process.wait()
            stderr = (await reading_stderr).decode(errors="replace")
            yield turn.result(returncode, stderr)
        finally:
            if process.returncode is None:
                await _stop(process, self._interrupt_grace_seconds)
            reading_stderr.cancel()
            with suppress(OSError):
                last_message_path.unlink()


def _build_arguments(
    session_id: str | None,
    cwd: Path,
    model: str | None,
    policy: PermissionPolicy,
    last_message_path: Path,
) -> list[str]:
    """Build the args after `codex`. Always pass permissions explicitly, not via user config."""
    network = f"sandbox_workspace_write.network_access={str(policy.allow_network).lower()}"
    if session_id is None:
        arguments = ["exec", "--json", "-s", str(policy.sandbox_level), "-C", str(cwd)]
    else:
        # Resume has no `-s` or `-C`: the sandbox is set via a config override, and the working
        # directory comes from the process's own working directory.
        arguments = ["exec", "resume", session_id, "--json"]
        arguments += ["-c", f'sandbox_mode="{policy.sandbox_level}"']
    arguments += ["-c", network, "-o", str(last_message_path)]
    if model is not None:
        arguments += ["-m", model]
    # Send the instructions via stdin: they may be long (CI log) or start with a hyphen.
    return [*arguments, "-"]


async def _send_prompt(stdin: asyncio.StreamWriter, prompt: str) -> None:
    stdin.write(prompt.encode())
    with suppress(BrokenPipeError, ConnectionResetError):
        await stdin.drain()
    stdin.close()


def _parse_line(line: bytes) -> dict[str, Any] | None:
    """Parse one output line; return None for lines that are not JSON objects (e.g. warnings)."""
    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _session_event(session_id: str | None, process_marker: str) -> AgentEvent:
    return AgentEvent(
        EventType.SESSION_STARTED, {"session_id": session_id, "process_marker": process_marker}
    )


class _Turn:
    """State of one turn read so far: session, usage, error message."""

    def __init__(
        self, session_id: str | None, process_marker: str, previous_usage: dict[str, Any] | None
    ) -> None:
        self._session_id = session_id
        self._announced_session_id = session_id
        self._process_marker = process_marker
        self._previous_usage = previous_usage
        self._usage: dict[str, Any] | None = None
        self._is_completed = False
        self._error: str | None = None

    def handle(self, event: dict[str, Any] | None) -> list[AgentEvent]:
        """Convert one Codex event into our events; return an empty list if not recorded."""
        if event is None:
            return []
        kind = event.get("type")
        if kind == "thread.started":
            return self._session_started(event.get("thread_id"))
        if kind == "turn.completed":
            self._is_completed = True
            self._usage = event.get("usage")
        elif kind == "turn.failed":
            self._error = str((event.get("error") or {}).get("message") or "turn failed")
        elif kind == "error":
            self._error = str(event.get("message") or "error")
        elif kind in ("item.started", "item.completed") and isinstance(event.get("item"), dict):
            converted = _item_to_event(event["item"], is_finished=kind == "item.completed")
            return [] if converted is None else [converted]
        return []

    def _session_started(self, thread_id: Any) -> list[AgentEvent]:
        if not thread_id:
            return []
        self._session_id = str(thread_id)
        if self._session_id == self._announced_session_id:
            return []
        self._announced_session_id = self._session_id
        return [_session_event(self._session_id, self._process_marker)]

    def result(self, returncode: int, stderr: str) -> TurnResult:
        """Build this turn's result after the process exits."""
        if self._is_completed and returncode == 0:
            exit_status, detail = ExitStatus.SUCCESS, None
        else:
            tail = stderr.strip()[-STDERR_TAIL_CHARS:]
            exit_status = ExitStatus.ERROR
            detail = self._error or f"codex exited with code {returncode}: {tail}"
        tokens_in, tokens_out = self._turn_tokens()
        usage_raw = None
        if self._usage is not None:
            usage_raw = {"session_id": self._session_id, "usage": self._usage}
        return TurnResult(
            session_id=self._session_id,
            exit_status=exit_status,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=None,
            usage_raw=usage_raw,
            detail=detail,
        )

    def _turn_tokens(self) -> tuple[int | None, int | None]:
        """Codex reports cumulative values for the whole session; subtract the previous totals.

        `input_tokens` already includes cached input tokens.
        """
        if self._usage is None:
            return None, None
        previous: dict[str, Any] = {}
        if self._previous_usage and self._previous_usage.get("session_id") == self._session_id:
            previous = self._previous_usage.get("usage") or {}
        return (
            _delta(self._usage.get("input_tokens"), previous.get("input_tokens")),
            _delta(self._usage.get("output_tokens"), previous.get("output_tokens")),
        )


def _delta(total: Any, previous_total: Any) -> int | None:
    if not isinstance(total, int):
        return None
    if not isinstance(previous_total, int):
        return total
    return max(total - previous_total, 0)


def _item_to_event(item: dict[str, Any], *, is_finished: bool) -> AgentEvent | None:
    item_type = str(item.get("type"))
    if item_type in MESSAGE_KINDS:
        text = str(item.get("text") or "")
        if not is_finished or not text.strip():
            return None
        return AgentEvent(EventType.AGENT_MESSAGE, {"kind": MESSAGE_KINDS[item_type], "text": text})
    payload: dict[str, Any] = {
        "phase": "finished" if is_finished else "started",
        "tool_use_id": item.get("id"),
        "tool": item_type,
    }
    if item_type == COMMAND_ITEM:
        payload["command"] = item.get("command")
        if is_finished:
            payload["exit_code"] = item.get("exit_code")
            payload["is_error"] = item.get("exit_code") != 0
            payload["output"] = _truncate(item.get("aggregated_output"))
        return AgentEvent(EventType.COMMAND, payload)
    # Other item kinds (file changes, MCP tools, web search, etc.) vary in shape; keep them as-is.
    payload["item"] = _truncate(item)
    if is_finished:
        payload["is_error"] = (
            item_type == ERROR_ITEM or item.get("status") == "failed" or bool(item.get("error"))
        )
    return AgentEvent(EventType.TOOL_CALL, payload)


def _truncate(value: Any) -> Any:
    """Truncate overly long strings, including nested ones."""
    if isinstance(value, str):
        if len(value) <= MAX_OUTPUT_CHARS:
            return value
        return value[:MAX_OUTPUT_CHARS] + "\n[truncated]"
    if isinstance(value, dict):
        return {key: _truncate(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate(item) for item in value]
    return value


async def _stop(process: asyncio.subprocess.Process, grace_seconds: float) -> None:
    """Stop Codex and the child processes it started; return only once they have exited.

    In testing, SIGINT makes Codex exit together with its child processes, SIGTERM does not.
    If it doesn't exit in time, kill it; Codex puts child processes in their own process
    groups, so list the descendants first and then kill them one by one.
    """
    with suppress(ProcessLookupError):
        process.send_signal(signal.SIGINT)
    try:
        await asyncio.wait_for(process.wait(), grace_seconds)
    except TimeoutError:
        descendants = await asyncio.to_thread(_descendants, process.pid)
        for pid in [process.pid, *descendants]:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        await process.wait()


def _descendants(root_pid: int) -> list[int]:
    """List all current descendant processes of a process."""
    listing = subprocess.run(
        ["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, check=False
    ).stdout
    children: dict[int, list[int]] = {}
    for line in listing.splitlines():
        pid, _, parent = line.strip().partition(" ")
        if pid.isdigit() and parent.strip().isdigit():
            children.setdefault(int(parent.strip()), []).append(int(pid))
    found: list[int] = []
    pending = [root_pid]
    while pending:
        for child in children.get(pending.pop(), []):
            found.append(child)
            pending.append(child)
    return found


def codex_auth(returncode: int, output: str) -> tuple[AuthKind, str | None]:
    """Map the result of `codex login status` to how Codex is signed in.

    The output can contain part of a saved API key, so only fixed phrases are looked for.
    """
    if returncode != 0:
        return AuthKind.NONE, None
    if "ChatGPT" in output:
        return AuthKind.SUBSCRIPTION, "ChatGPT"
    if "API key" in output:
        return AuthKind.API_KEY, "saved API key"
    return AuthKind.OTHER, "codex login"
