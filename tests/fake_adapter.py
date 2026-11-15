"""測試用的假 adapter：依劇本送出預錄的事件，不啟動任何 agent。"""

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

FULL_CAPABILITIES = AdapterCapabilities(
    can_resume_session=True,
    can_limit_turns=True,
    can_limit_budget=True,
    can_gate_tool_calls=True,
    can_restrict_network=True,
    reports_cost=True,
)


def session_started(session_id: str = "session-1") -> AgentEvent:
    """建立一筆 session_started 事件。"""
    return AgentEvent(
        EventType.SESSION_STARTED, {"session_id": session_id, "process_marker": session_id}
    )


def message(text: str) -> AgentEvent:
    """建立一筆 agent 訊息事件。"""
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
    """建立一筆結果。"""
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
    """一次 run_turn 呼叫收到的參數。"""

    prompt: str
    cwd: Path
    session_id: str | None
    model: str | None
    limits: RunLimits
    policy: PermissionPolicy
    previous_usage: dict[str, Any] | None


@dataclass
class FakeAdapter:
    """每次 run_turn 依序取用一份劇本。

    劇本是一個清單，元素可以是事件、結果、例外（會被拋出），
    或 `asyncio.Event`（串流會停在那裡直到它被設定）。
    """

    scripts: list[list[Any]]
    capabilities: AdapterCapabilities = FULL_CAPABILITIES
    calls: list[Call] = field(default_factory=list)
    closed_count: int = 0

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
        """依劇本送出事件。"""
        self.calls.append(Call(prompt, cwd, session_id, model, limits, policy, previous_usage))
        script = self.scripts.pop(0)
        try:
            for step in script:
                if isinstance(step, asyncio.Event):
                    await step.wait()
                    continue
                if isinstance(step, BaseException):
                    raise step
                yield step
        finally:
            self.closed_count += 1
