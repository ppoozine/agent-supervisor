"""The AgentAdapter interface and related types.

Runner depends only on this file; details specific to each agent stay in its own adapter.
The interface works in units of "one turn": send one input, get back a stream of events,
and the last one is the result of that turn.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from agent_supervisor.enums import EventType, ExitStatus
from agent_supervisor.tool_status import ToolStatus


class SandboxLevel(StrEnum):
    """What the agent is allowed to write to."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


@dataclass(frozen=True)
class AdapterCapabilities:
    """What the adapter truthfully declares it can do; Runner decides which limits to pass."""

    can_resume_session: bool
    can_limit_turns: bool
    can_limit_budget: bool
    can_gate_tool_calls: bool
    can_restrict_network: bool
    reports_cost: bool


@dataclass(frozen=True)
class RunLimits:
    """Limits for a single run that the adapter enforces while running; `None` means no limit."""

    max_turns: int | None
    max_budget_usd: float | None


@dataclass(frozen=True)
class PermissionPolicy:
    """Generic permission policy. Each adapter interprets the contents of `tool_rules` itself."""

    sandbox_level: SandboxLevel = SandboxLevel.WORKSPACE_WRITE
    allow_network: bool = True
    tool_rules: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentEvent:
    """A typed event emitted by an adapter; `type` maps to `events.type`."""

    type: EventType
    payload: dict[str, Any]


@dataclass(frozen=True)
class TurnResult:
    """The result of one turn; the last item in the event stream.

    Usage and cost must be the values for "this turn"; when the agent reports cumulative
    values, the adapter converts them. Fill unknown values with `None`, not 0.
    """

    session_id: str | None
    exit_status: ExitStatus
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    usage_raw: dict[str, Any] | None
    detail: str | None = None


class AgentAdapter(Protocol):
    """Interface for controlling one kind of agent."""

    display_name: str
    capabilities: AdapterCapabilities

    async def check(self) -> ToolStatus:
        """Whether this agent can be used: installed, its version, and how it is signed in.

        Must not spend any usage or handle credentials; see `tool_status`.
        """
        ...

    def run_turn(
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
        """Run one turn, yielding events in order and finally one `TurnResult`.

        - `session_id` of `None` means start a new session.
        - Emit a `session_started` event as soon as the session id is known.
        - `previous_usage` is the `usage_raw` of the same task's previous turn, used to
          derive this turn's usage.
        - When the stream is closed, the underlying agent and its child processes must be
          stopped.
        """
        ...
