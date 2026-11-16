"""ClaudeCodeAdapter 的測試。用假的 SDK client 餵預錄訊息，不啟動 agent。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Any, Self

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from agent_supervisor.adapters.base import (
    AgentEvent,
    PermissionPolicy,
    RunLimits,
    SandboxLevel,
    TurnResult,
)
from agent_supervisor.adapters.claude_code import ClaudeCodeAdapter, evaluate_tool_call
from agent_supervisor.enums import EventType, ExitStatus

CWD = Path("/work/tree")
POLICY = PermissionPolicy(
    tool_rules={"allowed_tools": ["Read", "Write", "Bash"], "denied_commands": ["git push", "gh"]}
)


class Ask:
    """劇本步驟：模擬 SDK 就一次工具呼叫詢問核准回呼。"""

    def __init__(
        self, tool_name: str, tool_input: dict[str, Any], *, via: str = "callback"
    ) -> None:
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.via = via
        self.decision: Any = None


class FakeClient:
    """假的 SDK client：依劇本送出訊息，並記錄被怎麼使用。"""

    def __init__(self, options: ClaudeAgentOptions, script: list[Any]) -> None:
        self.options = options
        self.script = script
        self.prompts: list[str] = []
        self.interrupted = False
        self.exited = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.exited = True

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def receive_response(self) -> AsyncIterator[Any]:
        for step in self.script:
            if isinstance(step, asyncio.Event):
                await step.wait()
            elif isinstance(step, BaseException):
                raise step
            elif isinstance(step, Ask) and step.via == "hook":
                assert self.options.hooks is not None
                hook = self.options.hooks["PreToolUse"][0].hooks[0]
                hook_input = {
                    "hook_event_name": "PreToolUse",
                    "tool_name": step.tool_name,
                    "tool_input": step.tool_input,
                }
                step.decision = await hook(hook_input, "tool-use-id", None)  # type: ignore[arg-type]
            elif isinstance(step, Ask):
                assert self.options.can_use_tool is not None
                step.decision = await self.options.can_use_tool(
                    step.tool_name, step.tool_input, None
                )
            else:
                yield step


class Harness:
    """建立 adapter 並保留它建立的假 client，方便檢查。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.clients: list[FakeClient] = []
        self.adapter = ClaudeCodeAdapter(client_factory=self._make_client)

    def _make_client(self, options: ClaudeAgentOptions) -> FakeClient:
        client = FakeClient(options, self.script)
        self.clients.append(client)
        return client

    async def run(
        self,
        *,
        session_id: str | None = None,
        limits: RunLimits = RunLimits(max_turns=5, max_budget_usd=2.0),  # noqa: B008
        policy: PermissionPolicy = POLICY,
        previous_usage: dict[str, Any] | None = None,
        model: str | None = "some-model",
    ) -> list[AgentEvent | TurnResult]:
        stream = self.adapter.run_turn(
            prompt="do it",
            cwd=Path("/work/tree"),
            session_id=session_id,
            model=model,
            limits=limits,
            policy=policy,
            previous_usage=previous_usage,
        )
        return [item async for item in stream]

    @property
    def options(self) -> ClaudeAgentOptions:
        return self.clients[0].options


def assistant(*blocks: Any) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="some-model")


def tool_result(tool_use_id: str, content: str, *, is_error: bool = False) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id, content, is_error)])


def sdk_result(
    subtype: str = "success",
    *,
    session_id: str = "ignored",
    total_cost_usd: float | None = 0.4,
    usage: dict[str, Any] | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=10,
        duration_api_ms=8,
        is_error=subtype != "success",
        num_turns=2,
        session_id=session_id,
        total_cost_usd=total_cost_usd,
        usage=usage
        if usage is not None
        else {
            "input_tokens": 6,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 3000,
            "output_tokens": 40,
        },
    )


def turn_result(items: list[AgentEvent | TurnResult]) -> TurnResult:
    last = items[-1]
    assert isinstance(last, TurnResult)
    return last


def events_of(items: list[AgentEvent | TurnResult], event_type: EventType) -> list[AgentEvent]:
    return [item for item in items if isinstance(item, AgentEvent) and item.type == event_type]


async def test_new_session_announces_its_process_before_the_session_is_confirmed() -> None:
    harness = Harness([sdk_result()])

    items = await harness.run()

    first = items[0]
    assert isinstance(first, AgentEvent)
    assert first.type == EventType.SESSION_STARTED
    marker = first.payload["process_marker"]
    assert len(marker) == 36
    assert first.payload["session_id"] is None
    assert harness.options.session_id == marker
    assert harness.options.resume is None


async def test_new_session_id_is_announced_once_the_agent_confirms_it() -> None:
    harness = Harness([SystemMessage("init", {"session_id": "whatever"}), sdk_result()])

    items = await harness.run()

    started = events_of(items, EventType.SESSION_STARTED)
    assigned = harness.options.session_id
    assert [event.payload["session_id"] for event in started] == [None, assigned]
    assert turn_result(items).session_id == assigned


async def test_session_that_never_starts_leaves_no_session_id_to_resume() -> None:
    harness = Harness([RuntimeError("working directory does not exist")])

    with pytest.raises(RuntimeError):
        await harness.run()

    assert harness.clients[0].exited is True


async def test_every_tool_call_is_checked_by_a_hook_that_project_rules_cannot_bypass() -> None:
    ask = Ask("Bash", {"command": "gh pr create"}, via="hook")
    harness = Harness([ask, sdk_result()])

    denied = events_of(await harness.run(), EventType.PERMISSION_DENIED)

    decision = ask.decision["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert "gh" in decision["permissionDecisionReason"]
    assert len(denied) == 1
    assert denied[0].payload["tool"] == "Bash"


async def test_hook_has_no_opinion_on_calls_the_policy_allows() -> None:
    ask = Ask("Bash", {"command": "ls"}, via="hook")
    harness = Harness([ask, sdk_result()])

    items = await harness.run()

    assert ask.decision == {}
    assert events_of(items, EventType.PERMISSION_DENIED) == []


async def test_existing_session_is_resumed() -> None:
    harness = Harness([sdk_result()])

    items = await harness.run(session_id="existing-session")

    assert harness.options.resume == "existing-session"
    assert harness.options.session_id is None
    assert items[0].payload["session_id"] == "existing-session"  # type: ignore[union-attr]
    assert turn_result(items).session_id == "existing-session"


async def test_options_carry_cwd_model_limits_and_isolation() -> None:
    harness = Harness([sdk_result()])

    await harness.run(limits=RunLimits(max_turns=9, max_budget_usd=1.25))

    options = harness.options
    assert options.cwd == Path("/work/tree")
    assert options.model == "some-model"
    assert options.max_turns == 9
    assert options.max_budget_usd == 1.25
    assert options.permission_mode == "default"
    assert options.setting_sources == ["project"]
    assert options.can_use_tool is not None
    assert harness.clients[0].prompts == ["do it"]


async def test_text_and_thinking_become_agent_messages() -> None:
    harness = Harness(
        [assistant(ThinkingBlock("let me think", "sig"), TextBlock("all done")), sdk_result()]
    )

    messages = events_of(await harness.run(), EventType.AGENT_MESSAGE)

    assert [event.payload for event in messages] == [
        {"kind": "reasoning", "text": "let me think"},
        {"kind": "message", "text": "all done"},
    ]


async def test_bash_tool_use_becomes_command_events_with_result() -> None:
    harness = Harness(
        [
            assistant(ToolUseBlock("tool-1", "Bash", {"command": "ls -la"})),
            tool_result("tool-1", "file.txt"),
            sdk_result(),
        ]
    )

    commands = events_of(await harness.run(), EventType.COMMAND)

    assert commands[0].payload == {
        "phase": "started",
        "tool_use_id": "tool-1",
        "tool": "Bash",
        "command": "ls -la",
        "input": {"command": "ls -la"},
    }
    assert commands[1].payload == {
        "phase": "finished",
        "tool_use_id": "tool-1",
        "is_error": False,
        "output": "file.txt",
    }


async def test_other_tool_use_becomes_tool_call_events() -> None:
    harness = Harness(
        [
            assistant(ToolUseBlock("tool-2", "Write", {"file_path": "a.txt", "content": "x"})),
            tool_result("tool-2", "boom", is_error=True),
            sdk_result(),
        ]
    )

    tool_calls = events_of(await harness.run(), EventType.TOOL_CALL)

    assert tool_calls[0].payload["phase"] == "started"
    assert tool_calls[0].payload["tool"] == "Write"
    assert tool_calls[1].payload["phase"] == "finished"
    assert tool_calls[1].payload["is_error"] is True


async def test_other_system_messages_are_not_recorded_as_events() -> None:
    harness = Harness([SystemMessage("vcs_state_changed", {"branch": "x"}), sdk_result()])

    items = await harness.run()

    assert [type(item).__name__ for item in items] == ["AgentEvent", "TurnResult"]


async def test_resumed_session_is_announced_only_once() -> None:
    harness = Harness([SystemMessage("init", {"session_id": "existing-session"}), sdk_result()])

    items = await harness.run(session_id="existing-session")

    assert len(events_of(items, EventType.SESSION_STARTED)) == 1


async def test_result_counts_cached_tokens_as_input() -> None:
    harness = Harness([sdk_result()])

    result = turn_result(await harness.run())

    assert result.exit_status == ExitStatus.SUCCESS
    assert result.tokens_in == 6 + 200 + 3000
    assert result.tokens_out == 40


async def test_first_turn_cost_is_the_reported_total() -> None:
    harness = Harness([sdk_result(total_cost_usd=0.4)])

    result = turn_result(await harness.run())

    assert result.cost_usd == pytest.approx(0.4)
    assert result.usage_raw is not None
    assert result.usage_raw["total_cost_usd"] == 0.4


async def test_resumed_turn_cost_is_the_difference_from_previous_total() -> None:
    harness = Harness([sdk_result(total_cost_usd=0.55)])
    previous = {"session_id": "existing-session", "total_cost_usd": 0.4}

    result = turn_result(await harness.run(session_id="existing-session", previous_usage=previous))

    assert result.cost_usd == pytest.approx(0.15)


async def test_previous_usage_from_another_session_is_ignored() -> None:
    harness = Harness([sdk_result(total_cost_usd=0.55)])
    previous = {"session_id": "some-other-session", "total_cost_usd": 0.4}

    result = turn_result(await harness.run(session_id="existing-session", previous_usage=previous))

    assert result.cost_usd == pytest.approx(0.55)


async def test_missing_cost_and_usage_stay_unknown() -> None:
    harness = Harness([sdk_result(total_cost_usd=None, usage={})])

    result = turn_result(await harness.run())

    assert result.cost_usd is None
    assert result.tokens_in is None
    assert result.tokens_out is None


@pytest.mark.parametrize(
    ("subtype", "expected"),
    [
        ("error_max_turns", ExitStatus.LIMIT_REACHED),
        ("error_max_budget_usd", ExitStatus.LIMIT_REACHED),
        ("error_during_execution", ExitStatus.ERROR),
    ],
)
async def test_result_subtype_maps_to_exit_status(subtype: str, expected: ExitStatus) -> None:
    harness = Harness([sdk_result(subtype)])

    result = turn_result(await harness.run())

    assert result.exit_status == expected
    assert result.detail == subtype


async def test_denied_tool_call_is_refused_and_recorded() -> None:
    ask = Ask("Bash", {"command": "git push origin main"})
    harness = Harness([ask, assistant(TextBlock("could not push")), sdk_result()])

    denied = events_of(await harness.run(), EventType.PERMISSION_DENIED)

    assert ask.decision.behavior == "deny"
    assert len(denied) == 1
    assert denied[0].payload["tool"] == "Bash"
    assert denied[0].payload["input"] == {"command": "git push origin main"}
    assert "git push" in denied[0].payload["reason"]


async def test_allowed_tool_call_is_approved_without_an_event() -> None:
    ask = Ask("Bash", {"command": "pytest -q"})
    harness = Harness([ask, sdk_result()])

    items = await harness.run()

    assert ask.decision.behavior == "allow"
    assert events_of(items, EventType.PERMISSION_DENIED) == []


async def test_closing_the_stream_early_interrupts_the_agent() -> None:
    harness = Harness([assistant(TextBlock("working")), asyncio.Event(), sdk_result()])
    stream = harness.adapter.run_turn(
        prompt="do it",
        cwd=Path("/work/tree"),
        session_id=None,
        model=None,
        limits=RunLimits(None, None),
        policy=POLICY,
        previous_usage=None,
    )

    async with aclosing(stream):  # type: ignore[type-var]
        async for item in stream:
            if isinstance(item, AgentEvent) and item.type == EventType.AGENT_MESSAGE:
                break

    client = harness.clients[0]
    assert client.interrupted is True
    assert client.exited is True


async def test_finished_turn_does_not_interrupt_the_agent() -> None:
    harness = Harness([sdk_result()])

    await harness.run()

    assert harness.clients[0].interrupted is False
    assert harness.clients[0].exited is True


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "is_allowed"),
    [
        ("Read", {"file_path": "a.py"}, True),
        ("Bash", {"command": "pytest -q"}, True),
        ("Bash", {"command": "git status && git diff"}, True),
        ("WebFetch", {"url": "https://example.com"}, False),
        ("Bash", {"command": "git push"}, False),
        ("Bash", {"command": "git push origin HEAD"}, False),
        ("Bash", {"command": "pytest -q && git push"}, False),
        ("Bash", {"command": "echo hi; gh pr create"}, False),
        ("Bash", {"command": "git pushd-not-a-push"}, True),
        ("Bash", {"command": "ghost --version"}, True),
    ],
)
def test_tool_rules(tool_name: str, tool_input: dict[str, Any], is_allowed: bool) -> None:
    reason = evaluate_tool_call(tool_name, tool_input, POLICY, cwd=CWD)

    assert (reason is None) is is_allowed


@pytest.mark.parametrize("tool_name", ["Write", "Edit", "Bash"])
def test_read_only_sandbox_denies_tools_that_can_write(tool_name: str) -> None:
    policy = PermissionPolicy(
        sandbox_level=SandboxLevel.READ_ONLY,
        tool_rules={"allowed_tools": ["Read", "Write", "Edit", "Bash"]},
    )

    assert evaluate_tool_call(tool_name, {"command": "ls"}, policy, cwd=CWD) is not None
    assert evaluate_tool_call("Read", {"file_path": "a"}, policy, cwd=CWD) is None


def test_capabilities_match_what_was_verified() -> None:
    capabilities = ClaudeCodeAdapter.capabilities

    assert capabilities.can_resume_session is True
    assert capabilities.can_gate_tool_calls is True
    assert capabilities.reports_cost is True
    assert capabilities.can_restrict_network is False


def test_background_execution_is_denied() -> None:
    foreground = evaluate_tool_call("Bash", {"command": "pytest -q"}, POLICY, cwd=CWD)
    explicit_foreground = evaluate_tool_call(
        "Bash", {"command": "pytest -q", "run_in_background": False}, POLICY, cwd=CWD
    )
    background = evaluate_tool_call(
        "Bash", {"command": "pytest -q", "run_in_background": True}, POLICY, cwd=CWD
    )

    assert foreground is None
    assert explicit_foreground is None
    assert background is not None
    assert "background" in background


async def test_empty_reasoning_and_text_blocks_are_not_recorded() -> None:
    harness = Harness(
        [assistant(ThinkingBlock("", "sig"), TextBlock("  \n"), TextBlock("real")), sdk_result()]
    )

    messages = events_of(await harness.run(), EventType.AGENT_MESSAGE)

    assert [event.payload for event in messages] == [{"kind": "message", "text": "real"}]


@pytest.mark.parametrize(
    ("tool_name", "path_key", "path", "is_allowed"),
    [
        ("Write", "file_path", "/work/tree/src/app.py", True),
        ("Write", "file_path", "notes.txt", True),
        ("Write", "file_path", "/work/tree", True),
        ("Write", "file_path", "/etc/hosts", False),
        ("Write", "file_path", "/work/tree-other/app.py", False),
        ("Write", "file_path", "../outside.txt", False),
        ("Write", "file_path", "/work/tree/../outside.txt", False),
        ("Edit", "file_path", "/home/someone/.zshrc", False),
        ("NotebookEdit", "notebook_path", "/elsewhere/nb.ipynb", False),
        ("Read", "file_path", "/etc/hosts", True),
    ],
)
def test_writing_tools_are_limited_to_the_working_directory(
    tool_name: str, path_key: str, path: str, is_allowed: bool
) -> None:
    policy = PermissionPolicy(
        tool_rules={"allowed_tools": ["Read", "Write", "Edit", "NotebookEdit"]}
    )

    reason = evaluate_tool_call(tool_name, {path_key: path}, policy, cwd=CWD)

    assert (reason is None) is is_allowed


async def test_write_outside_the_working_directory_is_denied_through_the_hook() -> None:
    ask = Ask("Write", {"file_path": "/etc/hosts", "content": "x"}, via="hook")
    harness = Harness([ask, sdk_result()])

    denied = events_of(await harness.run(), EventType.PERMISSION_DENIED)

    assert ask.decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "working directory" in denied[0].payload["reason"]
