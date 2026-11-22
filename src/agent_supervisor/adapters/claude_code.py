"""Claude Code adapter: controls Claude Code through the official Python Agent SDK.

See `docs/design/claude-sdk-notes.md` for how the SDK is actually used and what testing showed.
All strings and parameters specific to Claude Code appear only in this file.
"""

import asyncio
import json
import os
import re
import shutil
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from pathlib import Path
from typing import Any, Protocol

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from agent_supervisor.adapters.base import (
    AdapterCapabilities,
    AgentEvent,
    PermissionPolicy,
    RunLimits,
    SandboxLevel,
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

COMMAND_TOOL = "Bash"
FILE_WRITING_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
WRITING_TOOLS = FILE_WRITING_TOOLS | {COMMAND_TOOL}
PATH_KEYS = ("file_path", "notebook_path")
LIMIT_SUBTYPES = frozenset({"error_max_turns", "error_max_budget_usd"})
INPUT_TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
COMMAND_SEPARATORS = re.compile(r"&&|\|\||[;|\n]")
MAX_OUTPUT_CHARS = 20_000
INTERRUPT_TIMEOUT_SECONDS = 5.0

INSTALL_COMMAND = "curl -fsSL https://claude.ai/install.sh | bash"
SIGN_IN_COMMANDS = (
    "export ANTHROPIC_API_KEY=<your API key>  # then start agsv serve from this shell",
    "claude auth login  # or sign in with a Claude subscription instead",
)
# Quoted from https://code.claude.com/docs/en/agent-sdk/overview (checked 2026-11-21).
SUBSCRIPTION_NOTICE = (
    "Claude Code is signed in with a Claude subscription. The Agent SDK documentation says:"
    ' "Unless previously approved, Anthropic does not allow third party developers to offer'
    " claude.ai login or rate limits for their products, including agents built on the Claude"
    ' Agent SDK. Use the API key authentication methods described in the Quickstart instead."'
    " It does not say whether a tool that reuses the login you made yourself on this computer"
    " counts as offering it, or how to get approval. agent-supervisor never signs you in and"
    " never stores credentials; whether this use is fine for you is your call. To use an API key"
    " instead, set ANTHROPIC_API_KEY before starting agsv serve; it takes precedence over the"
    " subscription login."
)


class SdkClient(Protocol):
    """The SDK client interface this adapter uses; replaced with a fake in tests."""

    def query(self, prompt: str) -> Any: ...

    def receive_response(self) -> AsyncIterator[Any]: ...

    def interrupt(self) -> Any: ...


ClientFactory = Callable[[ClaudeAgentOptions], AbstractAsyncContextManager[SdkClient]]


def evaluate_tool_call(
    tool_name: str, tool_input: dict[str, Any], policy: PermissionPolicy, *, cwd: Path
) -> str | None:
    """Check one tool call against the policy; return None if allowed, else the reason.

    This is a basic safeguard, not a security boundary: commands are only prefix-matched,
    so indirect approaches are not blocked.
    """
    rules = policy.tool_rules
    allowed_tools = rules.get("allowed_tools")
    if allowed_tools is not None and tool_name not in allowed_tools:
        return f"tool {tool_name} is not in the allowed tools"
    if policy.sandbox_level is SandboxLevel.READ_ONLY and tool_name in WRITING_TOOLS:
        return f"tool {tool_name} can write and the sandbox level is read-only"
    if tool_name in FILE_WRITING_TOOLS and not _is_inside(tool_input, cwd):
        return f"tool {tool_name} may only write inside the working directory"
    if tool_name != COMMAND_TOOL:
        return None
    if tool_input.get("run_in_background"):
        # A run is a single turn. Background commands are killed when the turn ends, yet the
        # agent would treat them as handed off and done, so always require the foreground.
        return "background execution is not allowed; run the command in the foreground"
    command = str(tool_input.get("command", ""))
    for segment in COMMAND_SEPARATORS.split(command):
        for denied in rules.get("denied_commands", ()):
            if _starts_with_command(segment.strip(), denied):
                return f"command starting with {denied!r} is not allowed"
    return None


def _is_inside(tool_input: dict[str, Any], cwd: Path) -> bool:
    """Whether a write tool's target path is inside the working directory.

    Paths are only normalized as strings; symbolic links are not resolved.
    """
    for key in PATH_KEYS:
        target = tool_input.get(key)
        if target is None:
            continue
        resolved = Path(os.path.normpath(os.path.join(cwd, str(target))))
        if not resolved.is_relative_to(cwd):
            return False
    return True


def _starts_with_command(segment: str, denied: str) -> bool:
    return segment == denied or segment.startswith(denied + " ")


class ClaudeCodeAdapter:
    """Run one turn with the Claude Agent SDK."""

    display_name = "Claude Code"

    capabilities = AdapterCapabilities(
        can_resume_session=True,
        can_limit_turns=True,
        can_limit_budget=True,
        can_gate_tool_calls=True,
        # In testing the sandbox settings had no effect; until that is understood, do not claim
        # network restriction.
        can_restrict_network=False,
        reports_cost=True,
    )

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        setting_sources: tuple[str, ...] = ("project",),
        command_runner: CommandRunner = run_command,
        cli_path: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._client_factory: ClientFactory = client_factory or _default_client_factory
        self._setting_sources = setting_sources
        self._run = command_runner
        self._cli_path = cli_path
        self._environ = os.environ if environ is None else environ

    async def check(self) -> ToolStatus:
        """Check the Claude Code CLI the SDK runs, and how it is signed in.

        `claude auth status` reads the configured credentials without calling the API.
        """
        cli = self._cli_path or find_sdk_cli()
        if cli is None:
            return ToolStatus(installed=False, detail="not installed", fix=(INSTALL_COMMAND,))
        version, problem = await read_version(self._run, [str(cli)], fix=(INSTALL_COMMAND,))
        if problem is not None:
            return problem
        try:
            result = await run_check(self._run, [str(cli), "auth", "status", "--json"])
            # Signed out exits with 1 but still prints the JSON.
            data = json.loads(result.stdout)
        except (ToolMissingError, CommandTimeoutError, json.JSONDecodeError):
            return ToolStatus(
                installed=True, version=version, detail="could not read `claude auth status`"
            )
        auth, label = claude_auth(data, self._environ)
        if auth is AuthKind.NONE:
            return ToolStatus(
                installed=True, version=version, detail="not signed in", fix=SIGN_IN_COMMANDS
            )
        notice = SUBSCRIPTION_NOTICE if auth is AuthKind.SUBSCRIPTION else None
        return ToolStatus(
            installed=True, version=version, auth=auth, auth_label=label, notice=notice
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
        """Run one turn. A new session's id is chosen here up front, so it can be emitted early."""
        is_new_session = session_id is None
        active_session_id = session_id or str(uuid.uuid4())
        denials: list[AgentEvent] = []
        options = ClaudeAgentOptions(
            cwd=cwd,
            model=model,
            permission_mode="default",
            # Both gates use the same policy. The hook runs for every tool call, and before the
            # allow rules in project settings and the auto-approved read-only commands; the
            # approval callback is only called when permission must be asked, and is what lets the
            # call through.
            hooks={"PreToolUse": [HookMatcher(hooks=[_make_hook(policy, cwd, denials)])]},
            can_use_tool=_make_gate(policy, cwd, denials),
            setting_sources=list(self._setting_sources),  # type: ignore[arg-type]
            max_turns=limits.max_turns,
            max_budget_usd=limits.max_budget_usd,
            session_id=active_session_id if is_new_session else None,
            resume=None if is_new_session else active_session_id,
        )
        # For a new session, do not publish the session id until the agent confirms it: if
        # startup fails, later runs then won't try to resume a session that doesn't exist.
        # process_marker, however, must be emitted first so that restart recovery can find a
        # process that has already started.
        yield _session_event(None if is_new_session else active_session_id, active_session_id)

        tool_event_types: dict[str, EventType] = {}
        is_finished = False
        async with self._client_factory(options) as client:
            try:
                await client.query(prompt)
                async for message in client.receive_response():
                    while denials:
                        yield denials.pop(0)
                    if is_new_session and _is_init(message):
                        yield _session_event(active_session_id, active_session_id)
                        continue
                    if isinstance(message, ResultMessage):
                        is_finished = True
                        yield _to_turn_result(message, active_session_id, previous_usage)
                        return
                    for event in _to_events(message, tool_event_types):
                        yield event
            finally:
                if not is_finished:
                    await _interrupt_quietly(client)


def find_sdk_cli() -> Path | None:
    """The Claude Code CLI the SDK runs: the copy bundled with it, otherwise `claude` on PATH."""
    name = "claude.exe" if sys.platform == "win32" else "claude"
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    if bundled.is_file():
        return bundled
    found = shutil.which("claude")
    return None if found is None else Path(found)


def claude_auth(data: Mapping[str, Any], environ: Mapping[str, str]) -> tuple[AuthKind, str | None]:
    """Map the output of `claude auth status --json` to how Claude Code is signed in.

    Only the method and its source are read; the account's email and organization are left out.
    """
    if not data.get("loggedIn"):
        return AuthKind.NONE, None
    method = data.get("authMethod")
    if method == "claude.ai":
        plan = data.get("subscriptionType")
        return AuthKind.SUBSCRIPTION, f"claude.ai ({plan})" if plan else "claude.ai"
    if method == "api_key":
        return AuthKind.API_KEY, str(data.get("apiKeySource") or "API key")
    if method == "oauth_token":
        # Both a gateway's ANTHROPIC_AUTH_TOKEN and a subscription's CLAUDE_CODE_OAUTH_TOKEN (from
        # `claude setup-token`) report this; the gateway token takes precedence when both are set.
        if "ANTHROPIC_AUTH_TOKEN" in environ:
            return AuthKind.API_KEY, "ANTHROPIC_AUTH_TOKEN"
        return AuthKind.SUBSCRIPTION, "CLAUDE_CODE_OAUTH_TOKEN"
    if method == "third_party":
        return AuthKind.CLOUD_PROVIDER, str(data.get("apiProvider") or "cloud provider")
    return AuthKind.OTHER, str(method)


def _default_client_factory(options: ClaudeAgentOptions) -> ClaudeSDKClient:
    return ClaudeSDKClient(options=options)


def _session_event(session_id: str | None, process_marker: str) -> AgentEvent:
    return AgentEvent(
        EventType.SESSION_STARTED, {"session_id": session_id, "process_marker": process_marker}
    )


def _is_init(message: Any) -> bool:
    return isinstance(message, SystemMessage) and message.subtype == "init"


def _check(
    tool_name: str,
    tool_input: dict[str, Any],
    policy: PermissionPolicy,
    cwd: Path,
    denials: list[AgentEvent],
) -> str | None:
    """Check the policy; if denied, log permission_denied and return a message for the agent."""
    reason = evaluate_tool_call(tool_name, tool_input, policy, cwd=cwd)
    if reason is None:
        return None
    denials.append(
        AgentEvent(
            EventType.PERMISSION_DENIED, {"tool": tool_name, "input": tool_input, "reason": reason}
        )
    )
    return f"Denied by supervisor policy: {reason}"


def _make_hook(policy: PermissionPolicy, cwd: Path, denials: list[AgentEvent]) -> Any:
    """Build the PreToolUse hook: deny calls that violate the policy, otherwise stay neutral."""

    async def pre_tool_use(hook_input: Any, tool_use_id: str | None, context: Any) -> Any:
        message = _check(hook_input["tool_name"], hook_input["tool_input"], policy, cwd, denials)
        if message is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            }
        }

    return pre_tool_use


def _make_gate(policy: PermissionPolicy, cwd: Path, denials: list[AgentEvent]) -> Any:
    """Build the approval callback: allow or deny calls that need asking, per the policy."""

    async def gate(
        tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> PermissionResultAllow | PermissionResultDeny:
        message = _check(tool_name, tool_input, policy, cwd, denials)
        if message is None:
            return PermissionResultAllow()
        return PermissionResultDeny(message=message)

    return gate


def _to_events(message: Any, tool_event_types: dict[str, EventType]) -> list[AgentEvent]:
    """Convert one SDK message into typed events; return an empty list if it is not recorded."""
    if isinstance(message, AssistantMessage):
        return [
            event
            for block in message.content
            if (event := _assistant_block_to_event(block, tool_event_types)) is not None
        ]
    if isinstance(message, UserMessage) and not isinstance(message.content, str):
        return [
            _tool_result_to_event(block, tool_event_types)
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
    return []


def _assistant_block_to_event(
    block: Any, tool_event_types: dict[str, EventType]
) -> AgentEvent | None:
    if isinstance(block, TextBlock):
        return _message_event("message", block.text)
    if isinstance(block, ThinkingBlock):
        return _message_event("reasoning", block.thinking)
    if not isinstance(block, ToolUseBlock):
        return None
    payload: dict[str, Any] = {"phase": "started", "tool_use_id": block.id, "tool": block.name}
    event_type = EventType.TOOL_CALL
    if block.name == COMMAND_TOOL:
        event_type = EventType.COMMAND
        payload["command"] = block.input.get("command")
    payload["input"] = block.input
    tool_event_types[block.id] = event_type
    return AgentEvent(event_type, payload)


def _message_event(kind: str, text: str) -> AgentEvent | None:
    """Skip empty messages (the SDK sometimes sends thinking blocks with no text)."""
    if not text.strip():
        return None
    return AgentEvent(EventType.AGENT_MESSAGE, {"kind": kind, "text": text})


def _tool_result_to_event(
    block: ToolResultBlock, tool_event_types: dict[str, EventType]
) -> AgentEvent:
    output = block.content
    if isinstance(output, str) and len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n[truncated]"
    return AgentEvent(
        tool_event_types.get(block.tool_use_id, EventType.TOOL_CALL),
        {
            "phase": "finished",
            "tool_use_id": block.tool_use_id,
            "is_error": bool(block.is_error),
            "output": output,
        },
    )


def _to_turn_result(
    message: ResultMessage, session_id: str, previous_usage: dict[str, Any] | None
) -> TurnResult:
    usage = message.usage or {}
    has_usage = any(key in usage for key in INPUT_TOKEN_KEYS)
    if message.subtype == "success":
        exit_status, detail = ExitStatus.SUCCESS, None
    elif message.subtype in LIMIT_SUBTYPES:
        exit_status, detail = ExitStatus.LIMIT_REACHED, message.subtype
    else:
        exit_status, detail = ExitStatus.ERROR, message.subtype
    return TurnResult(
        session_id=session_id,
        exit_status=exit_status,
        tokens_in=sum(usage.get(key, 0) for key in INPUT_TOKEN_KEYS) if has_usage else None,
        tokens_out=usage.get("output_tokens"),
        cost_usd=_turn_cost(message.total_cost_usd, session_id, previous_usage),
        usage_raw={
            "session_id": session_id,
            "total_cost_usd": message.total_cost_usd,
            "usage": usage,
            "subtype": message.subtype,
            "num_turns": message.num_turns,
            "terminal_reason": message.terminal_reason,
        },
        detail=detail,
    )


def _turn_cost(
    total_cost_usd: float | None, session_id: str, previous_usage: dict[str, Any] | None
) -> float | None:
    """The SDK reports cumulative session cost; subtract the previous total to get this turn's."""
    if total_cost_usd is None:
        return None
    if previous_usage is None or previous_usage.get("session_id") != session_id:
        return total_cost_usd
    previous_total = previous_usage.get("total_cost_usd")
    if previous_total is None:
        return total_cost_usd
    return max(total_cost_usd - float(previous_total), 0.0)


async def _interrupt_quietly(client: SdkClient) -> None:
    """Ask the agent to stop when the stream closes early. Never raises: we disconnect next."""
    with suppress(Exception):
        await asyncio.wait_for(client.interrupt(), INTERRUPT_TIMEOUT_SECONDS)
