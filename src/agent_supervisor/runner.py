"""Runner：負責一個 run 從建立到結案的整個生命週期。

一個 run 分成四段：建立、準備、agent 執行、收尾。四段由同一個可取消的
asyncio 工作負責，全部做完才結案。Runner 只透過 `AgentAdapter` 介面控制 agent。
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

from agent_supervisor import db, status
from agent_supervisor.adapters.base import AgentAdapter, AgentEvent, RunLimits, TurnResult
from agent_supervisor.config import Config
from agent_supervisor.enums import EventType, ExitStatus, RunTrigger

EventWriter = Callable[[EventType, dict[str, Any]], Awaitable[None]]
RunStage = Callable[[db.Task, db.Run, EventWriter], Awaitable[None]]
ArchiveStage = Callable[[db.Task], Awaitable[None]]
EventCallback = Callable[[EventType, dict[str, Any]], None]


class UnknownAgentError(ValueError):
    """指定的 agent 種類沒有對應的 adapter。"""


class NoActiveRunError(RuntimeError):
    """任務沒有正在執行的 run，沒有東西可以中止。"""


class BudgetExhaustedError(RuntimeError):
    """任務的花費或 token 已達上限，不能再建立 run。"""


class NoWorkingDirectoryError(RuntimeError):
    """準備階段結束後任務仍然沒有工作目錄，agent 無處執行。"""


@dataclass
class _Job:
    """一個執行中的 run，以及它目前為止的結果。"""

    run: db.Run
    work: asyncio.Task[None] | None = None
    result: TurnResult | None = None
    cancel_status: ExitStatus = ExitStatus.INTERRUPTED
    is_cancel_requested: bool = False
    is_closing: bool = False
    has_started: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class _Outcome:
    exit_status: ExitStatus
    detail: str | None = None


async def _no_stage(task: db.Task, run: db.Run, record: EventWriter) -> None:
    """什麼都不做的準備或收尾階段。"""


async def _no_archive(task: db.Task) -> None:
    """什麼都不做的封存階段。"""


class Runner:
    """建立、執行、中止 run，以及封存任務。"""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        adapters: Mapping[str, AgentAdapter],
        config: Config,
        *,
        prepare: RunStage = _no_stage,
        finalize: RunStage = _no_stage,
        archive: ArchiveStage = _no_archive,
        on_event: EventCallback | None = None,
    ) -> None:
        self._conn = conn
        self._adapters = adapters
        self._config = config
        self._prepare = prepare
        self._finalize = finalize
        self._archive = archive
        self._on_event = on_event
        self._jobs: dict[int, _Job] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._creation_lock = asyncio.Lock()

    async def start_task(
        self, *, project_id: int, title: str, prompt: str, agent_type: str, model: str | None
    ) -> db.Run:
        """建立任務與它的 initial run，並開始執行。"""
        if agent_type not in self._adapters:
            raise UnknownAgentError(f"no adapter for agent type {agent_type!r}")
        async with self._creation_lock:
            task, run = await db.create_task_with_initial_run(
                self._conn,
                project_id=project_id,
                title=title,
                prompt=prompt,
                agent_type=agent_type,
                model=model,
            )
            self._launch(task, run)
        return run

    async def send_message(self, task_id: int, text: str) -> db.Run:
        """對既有任務的同一個 session 追加指令，建立一個新的 run。"""
        async with self._lock_for(task_id):
            task = await db.get_task(self._conn, task_id)
            await self._require_remaining_budget(task_id)
            run = await db.create_run(self._conn, task_id, RunTrigger.USER_MESSAGE, text)
            self._launch(task, run)
        return run

    async def start_ci_fix(self, task_id: int) -> db.Run | None:
        """任務符合自動修復條件時建立 ci_failure run 並開始執行；不符合就回傳 None。

        條件在任務鎖與資料庫交易內重新檢查，所以不會和使用者的訊息搶同一個 session。
        失敗的 CI log 在準備階段才抓，那時 run 已經存在、可以被中止。
        """
        limits = status.Limits(self._config.task_cost_limit_usd, self._config.task_token_limit)
        async with self._lock_for(task_id):
            run = await db.create_ci_failure_run(
                self._conn,
                task_id,
                "CI failed; the failure log is fetched before the agent starts.",
                condition=lambda snapshot: status.can_auto_fix(snapshot, limits),
            )
            if run is None:
                return None
            self._launch(await db.get_task(self._conn, task_id), run)
        return run

    async def stop(self, task_id: int) -> None:
        """中止任務正在執行的 run，等它完全停下並結案後才返回。"""
        async with self._lock_for(task_id):
            job = self._jobs.get(task_id)
            if job is None or job.work is None or job.work.done():
                raise NoActiveRunError(f"task {task_id} has no active run")
            await self._cancel(job, ExitStatus.STOPPED)

    async def stop_if_running(self, task_id: int) -> bool:
        """任務有 run 在執行就中止它，回傳是否真的中止了一個 run。"""
        try:
            await self.stop(task_id)
        except NoActiveRunError:
            return False
        return True

    async def stop_all(self) -> None:
        """中止所有正在執行的 run。"""
        for task_id in list(self._jobs):
            await self.stop_if_running(task_id)

    async def archive(self, task_id: int) -> None:
        """封存任務。有 run 在執行時拒絕；封存階段失敗時任務維持未封存。"""
        async with self._lock_for(task_id):
            task = await db.get_task(self._conn, task_id)
            if task.archived_at is not None:
                raise db.TaskNotRunnableError(f"task {task_id} is already archived")
            if await db.get_open_run(self._conn, task_id) is not None:
                raise db.RunAlreadyActiveError(f"task {task_id} has an active run; stop it first")
            await self._archive(task)
            await db.archive_task(self._conn, task_id)

    async def wait(self, run_id: int) -> db.Run:
        """等指定的 run 結案，回傳結案後的資料。"""
        for job in list(self._jobs.values()):
            if job.run.id == run_id and job.work is not None:
                await asyncio.wait({job.work})
        return await db.get_run(self._conn, run_id)

    async def shutdown(self) -> None:
        """daemon 正常關閉時，把還在執行的 run 中止並標成 interrupted。"""
        for job in list(self._jobs.values()):
            if job.work is not None and not job.work.done():
                await self._cancel(job, ExitStatus.INTERRUPTED)

    def _lock_for(self, task_id: int) -> asyncio.Lock:
        return self._locks.setdefault(task_id, asyncio.Lock())

    def _launch(self, task: db.Task, run: db.Run) -> None:
        job = _Job(run=run)
        self._jobs[task.id] = job
        deadline = asyncio.get_running_loop().time() + self._config.run_timeout_seconds
        job.work = asyncio.create_task(self._execute(task, job, deadline), name=f"run-{run.id}")

    async def _cancel(self, job: _Job, status: ExitStatus) -> None:
        assert job.work is not None
        # 工作還沒開始執行就被取消的話，它連結案的程式都不會跑到，run 會永遠不結束。
        await job.has_started.wait()
        # 已經在結案的 run 不能再取消，否則結案寫到一半會被打斷、run 永遠不結束。
        # 已經要求過取消的也不能再取消一次：第二次會打斷 agent 正在做的收拾。
        # 上一個呼叫端等到一半自己被取消時（例如程序正要結束），就會有第二個人來中止。
        if not job.is_closing and not job.is_cancel_requested:
            job.is_cancel_requested = True
            job.cancel_status = status
            job.work.cancel()
        await asyncio.wait({job.work})

    async def _require_remaining_budget(self, task_id: int) -> None:
        usage = await db.get_task_usage(self._conn, task_id)
        if usage.cost_usd >= self._config.task_cost_limit_usd:
            raise BudgetExhaustedError(f"task {task_id} reached its cost limit")
        if usage.tokens >= self._config.task_token_limit:
            raise BudgetExhaustedError(f"task {task_id} reached its token limit")

    async def _execute(self, task: db.Task, job: _Job, deadline: float) -> None:
        """執行一個 run 的準備、agent、收尾三段，最後一定會結案。"""

        async def record(event_type: EventType, payload: dict[str, Any]) -> None:
            await self._write_event(task.id, job.run.id, event_type, payload)

        job.has_started.set()
        try:
            async with asyncio.timeout_at(deadline):
                await self._prepare(task, job.run, record)
                outcome = await self._run_agent(task, job)
                if outcome.exit_status is ExitStatus.SUCCESS:
                    await self._finalize(task, job.run, record)
        except TimeoutError:
            outcome = _Outcome(ExitStatus.LIMIT_REACHED, "run exceeded its time limit")
        except asyncio.CancelledError:
            # 取消一律來自 Runner 自己（Stop 或關閉）。這裡吞掉取消，
            # 讓這個工作能把 run 結案後正常結束。
            outcome = _Outcome(job.cancel_status, "run was cancelled")
        except Exception as error:
            outcome = _Outcome(ExitStatus.ERROR, f"{type(error).__name__}: {error}")
        job.is_closing = True
        await self._close(task, job, outcome)

    async def _run_agent(self, task: db.Task, job: _Job) -> _Outcome:
        adapter = self._adapters[task.agent_type]
        capabilities = adapter.capabilities
        current = await db.get_task(self._conn, task.id)
        if current.worktree_path is None:
            raise NoWorkingDirectoryError(f"task {task.id} has no working directory")
        # 準備階段可能改寫了這次要送的內容（ci_failure run 的 CI log），所以重新讀取。
        run = await db.get_run(self._conn, job.run.id)
        stream = adapter.run_turn(
            prompt=run.input_text,
            cwd=Path(current.worktree_path),
            session_id=current.session_id if capabilities.can_resume_session else None,
            model=current.model,
            limits=await self._limits_for(task.id, adapter),
            policy=self._config.permission,
            previous_usage=await db.get_last_usage_raw(self._conn, task.id),
        )
        async with aclosing(stream):  # type: ignore[type-var]
            async for item in stream:
                if isinstance(item, TurnResult):
                    job.result = item
                    break
                await self._record(task.id, job.run.id, item)
        if job.result is None:
            return _Outcome(ExitStatus.ERROR, "agent stream ended without a result")
        return _Outcome(job.result.exit_status, job.result.detail)

    async def _limits_for(self, task_id: int, adapter: AgentAdapter) -> RunLimits:
        capabilities = adapter.capabilities
        usage = await db.get_task_usage(self._conn, task_id)
        remaining = max(self._config.task_cost_limit_usd - usage.cost_usd, 0.0)
        return RunLimits(
            max_turns=self._config.max_turns_per_run if capabilities.can_limit_turns else None,
            max_budget_usd=remaining if capabilities.can_limit_budget else None,
        )

    async def _record(self, task_id: int, run_id: int, event: AgentEvent) -> None:
        await self._write_event(task_id, run_id, event.type, event.payload)
        session_id = event.payload.get("session_id")
        if event.type is EventType.SESSION_STARTED and session_id:
            await db.set_task_session_id(self._conn, task_id, session_id)

    async def _close(self, task: db.Task, job: _Job, outcome: _Outcome) -> None:
        result = job.result
        await db.finish_run(
            self._conn,
            job.run.id,
            outcome.exit_status,
            tokens_in=result.tokens_in if result else None,
            tokens_out=result.tokens_out if result else None,
            cost_usd=result.cost_usd if result else None,
        )
        await self._write_event(
            task.id,
            job.run.id,
            EventType.STATE_CHANGE,
            {
                "change": "run_ended",
                "exit_status": str(outcome.exit_status),
                "detail": outcome.detail,
                "usage_raw": result.usage_raw if result else None,
            },
        )

    async def _write_event(
        self, task_id: int, run_id: int, event_type: EventType, payload: dict[str, Any]
    ) -> None:
        await db.append_event(self._conn, task_id, run_id, event_type, payload)
        if self._on_event is not None:
            self._on_event(event_type, payload)
