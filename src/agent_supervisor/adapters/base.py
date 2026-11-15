"""AgentAdapter 介面與相關型別。

Runner 只依賴本檔；各 agent 專屬的細節留在各自的 adapter。
介面以「一輪對話」為單位：送一段輸入，拿回一串事件，最後一筆是這一輪的結果。
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from agent_supervisor.enums import EventType, ExitStatus


class SandboxLevel(StrEnum):
    """Agent 可以寫入的範圍。"""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


@dataclass(frozen=True)
class AdapterCapabilities:
    """Adapter 如實宣告自己做得到什麼，Runner 依此決定要傳哪些限制。"""

    can_resume_session: bool
    can_limit_turns: bool
    can_limit_budget: bool
    can_gate_tool_calls: bool
    can_restrict_network: bool
    reports_cost: bool


@dataclass(frozen=True)
class RunLimits:
    """單一 run 在執行中由 adapter 擋下的上限；`None` 表示不設限。"""

    max_turns: int | None
    max_budget_usd: float | None


@dataclass(frozen=True)
class PermissionPolicy:
    """通用的權限政策。`tool_rules` 的內容由各 adapter 自行解讀。"""

    sandbox_level: SandboxLevel = SandboxLevel.WORKSPACE_WRITE
    allow_network: bool = True
    tool_rules: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentEvent:
    """Adapter 送出的有型別事件，`type` 對應 `events.type`。"""

    type: EventType
    payload: dict[str, Any]


@dataclass(frozen=True)
class TurnResult:
    """一輪對話的結果，是事件串流的最後一筆。

    用量與花費必須是「這一輪」的值；agent 給累計值時由 adapter 換算。
    未知的值填 `None`，不要填 0。
    """

    session_id: str | None
    exit_status: ExitStatus
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    usage_raw: dict[str, Any] | None
    detail: str | None = None


class AgentAdapter(Protocol):
    """控制單一種 agent 的介面。"""

    capabilities: AdapterCapabilities

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
        """執行一輪對話，依序送出事件，最後送出一筆 `TurnResult`。

        - `session_id` 為 `None` 表示開新 session。
        - 一拿到 session id 就要送出 `session_started` 事件。
        - `previous_usage` 是同一個任務上一輪的 `usage_raw`，供換算單輪用量。
        - 串流被關閉時，必須把底下的 agent 與它的子程序都停掉。
        """
        ...
