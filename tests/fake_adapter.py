"""Fake adapter for tests: emits prerecorded events from a script; never starts an agent."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
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
from agent_supervisor.tool_status import AuthKind, ToolStatus

READY = ToolStatus(installed=True, version="1.0.0", auth=AuthKind.API_KEY, auth_label="TEST_KEY")

FULL_CAPABILITIES = AdapterCapabilities(
    can_resume_session=True,
    can_limit_turns=True,
    can_limit_budget=True,
    can_gate_tool_calls=True,
    can_restrict_network=True,
    reports_cost=True,
)


def session_started(session_id: str = "session-1") -> AgentEvent:
    """Build a session_started event."""
    return AgentEvent(
        EventType.SESSION_STARTED, {"session_id": session_id, "process_marker": session_id}
    )


def message(text: str) -> AgentEvent:
    """Build an agent message event."""
    return AgentEvent(EventType.AGENT_MESSAGE, {"kind": "message", "text": text})


def result(
    exit_status: ExitStatus = ExitStatus.SUCCESS,
    *,
    session_id: str | None = "session-1",
    tokens_in: int | None = 100,
    tokens_out: int | None = 10,
    cost_usd: float | None = 0.5,
    usage_raw: dict[str, Any] | None = None,
) -> TurnResult:
    """Build a result."""
    return TurnResult(
        session_id=session_id,
        exit_status=exit_status,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        usage_raw=usage_raw if usage_raw is not None else {"total": 1},
    )


@dataclass
class Call:
    """Arguments received by one run_turn call."""

    prompt: str
    cwd: Path
    session_id: str | None
    model: str | None
    limits: RunLimits
    policy: PermissionPolicy
    previous_usage: dict[str, Any] | None


@dataclass
class FakeAdapter:
    """Each run_turn call takes the next script in order.

    A script is a list whose elements can be events, results, exceptions (which are raised),
    `asyncio.Event`s (the stream pauses there until the event is set),
    or functions (called with this call's arguments, to simulate the agent editing files
    in the working directory).
    """

    scripts: list[list[Any]]
    capabilities: AdapterCapabilities = FULL_CAPABILITIES
    status: ToolStatus = field(default_factory=lambda: READY)
    display_name: str = "Fake"
    calls: list[Call] = field(default_factory=list)
    closed_count: int = 0

    async def check(self) -> ToolStatus:
        """Report the status the test set."""
        return self.status

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
        """Emit events according to the script."""
        self.calls.append(Call(prompt, cwd, session_id, model, limits, policy, previous_usage))
        script = self.scripts.pop(0)
        try:
            for step in script:
                if isinstance(step, asyncio.Event):
                    await step.wait()
                    continue
                if isinstance(step, BaseException):
                    raise step
                if callable(step):
                    step(self.calls[-1])
                    continue
                yield step
        finally:
            self.closed_count += 1
