"""Codex adapter：用 subprocess 包 `codex exec` 的非互動模式，逐行讀 JSONL 事件。

用法與實測結果見 `docs/codex-notes.md`。所有 Codex 專屬的字串與參數都只出現在這個檔案。
"""

import asyncio
import json
import os
import signal
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator, Sequence
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

DEFAULT_EXECUTABLE = ("codex",)
COMMAND_ITEM = "command_execution"
ERROR_ITEM = "error"
MESSAGE_KINDS = {"agent_message": "message", "reasoning": "reasoning"}
MAX_OUTPUT_CHARS = 20_000
# 一個事件一行；指令的輸出整份放在同一行裡，所以每行的上限要比 asyncio 預設的 64 KB 大。
MAX_LINE_BYTES = 32 * 1024 * 1024
INTERRUPT_GRACE_SECONDS = 10.0
STDERR_TAIL_CHARS = 500


class CodexAdapter:
    """每一輪啟動一個 `codex exec` 程序。"""

    capabilities = AdapterCapabilities(
        can_resume_session=True,
        # `codex exec` 沒有輪數與花費上限的選項，也只回報 token 數。
        can_limit_turns=False,
        can_limit_budget=False,
        # 非互動模式無法逐次核准；能落實的只有 sandbox 等級與是否連網。
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
    ) -> None:
        self._executable = tuple(executable)
        self._scratch_dir = scratch_dir or Path(tempfile.gettempdir())
        self._interrupt_grace_seconds = interrupt_grace_seconds

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
        """執行一輪對話。`limits` 用不到：Codex 沒有可以在執行中擋下的上限。"""
        # Codex 的 session id 要等它啟動後才知道，所以另外準備一個啟動前就確定的字串
        # 讓重啟復原找得到程序：把它放進「最後一則訊息」輸出檔的檔名，就會出現在命令列上。
        # 檔案放在 worktree 之外，才不會被收尾時一起 commit。
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
            # 放進自己的 session：在終端機按 Ctrl-C 時訊號才不會直接送到 Codex，
            # 由 Runner 決定何時中止、結果怎麼記。
            start_new_session=True,
        )
        assert process.stdin is not None and process.stdout is not None
        assert process.stderr is not None
        # stderr 要同時讀掉，否則管線滿了 Codex 會卡住。
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
    """組出 `codex` 之後的參數。權限設定每次都明確傳入，不依賴使用者自己的設定檔。"""
    network = f"sandbox_workspace_write.network_access={str(policy.allow_network).lower()}"
    if session_id is None:
        arguments = ["exec", "--json", "-s", str(policy.sandbox_level), "-C", str(cwd)]
    else:
        # 續接沒有 `-s` 與 `-C`：sandbox 用設定覆寫，工作目錄靠程序本身的工作目錄。
        arguments = ["exec", "resume", session_id, "--json"]
        arguments += ["-c", f'sandbox_mode="{policy.sandbox_level}"']
    arguments += ["-c", network, "-o", str(last_message_path)]
    if model is not None:
        arguments += ["-m", model]
    # 指示從 stdin 送：內容可能很長（CI log），也可能以連字號開頭。
    return [*arguments, "-"]


async def _send_prompt(stdin: asyncio.StreamWriter, prompt: str) -> None:
    stdin.write(prompt.encode())
    with suppress(BrokenPipeError, ConnectionResetError):
        await stdin.drain()
    stdin.close()


def _parse_line(line: bytes) -> dict[str, Any] | None:
    """解析一行輸出；不是 JSON 物件的行（例如警告訊息）回傳 None。"""
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
    """一輪對話讀到目前為止的狀態：session、用量、錯誤訊息。"""

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
        """把一筆 Codex 事件轉成我們的事件；不需要記錄的回傳空清單。"""
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
        """程序結束後組出這一輪的結果。"""
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
        """Codex 回報的是整個 session 的累計值，減掉上一輪的累計才是這一輪的用量。

        `input_tokens` 已經包含快取的輸入 token。
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
    # 其他種類的項目（檔案變更、MCP 工具、網頁搜尋等）形狀各不相同，原樣保留。
    payload["item"] = _truncate(item)
    if is_finished:
        payload["is_error"] = (
            item_type == ERROR_ITEM or item.get("status") == "failed" or bool(item.get("error"))
        )
    return AgentEvent(EventType.TOOL_CALL, payload)


def _truncate(value: Any) -> Any:
    """把過長的字串截短，巢狀的內容也一樣處理。"""
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
    """停掉 Codex 與它啟動的子程序，確認結束後才返回。

    實測 SIGINT 會讓 Codex 連同子程序一起結束，SIGTERM 不會。等不到的話強制終止；
    Codex 把子程序放在各自的程序群組，所以要先列出子孫程序再逐一終止。
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
    """列出一個程序目前所有的子孫程序。"""
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
