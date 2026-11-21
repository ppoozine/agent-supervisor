"""Runner: owns the whole lifecycle of a run, from creation to finish.

A run has four stages: create, prepare, agent execution, wrap-up. All four are handled by the same
cancellable asyncio task, and the run is finished only after all of them are done. The Runner
controls agents only through the `AgentAdapter` interface.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

from agent_supervisor import db, feedback, status
from agent_supervisor.adapters.base import AgentAdapter, AgentEvent, RunLimits, TurnResult
from agent_supervisor.config import Config
from agent_supervisor.enums import EventType, ExitStatus, RunTrigger

EventWriter = Callable[[EventType, dict[str, Any]], Awaitable[None]]
RunStage = Callable[[db.Task, db.Run, EventWriter], Awaitable[None]]
ArchiveStage = Callable[[db.Task], Awaitable[None]]
LeftoverFinisher = Callable[[db.Run], Awaitable[None]]
EventCallback = Callable[[EventType, dict[str, Any]], None]


class UnknownAgentError(ValueError):
    """No adapter exists for the requested agent kind."""


class NoActiveRunError(RuntimeError):
    """The task has no run in progress, so there is nothing to stop."""


class NoWorkingDirectoryError(RuntimeError):
    """The task still has no working directory after the prepare stage; the agent cannot run."""


@dataclass
class _Job:
    """A run in progress and its results so far."""

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
    """A prepare or wrap-up stage that does nothing."""


async def _no_archive(task: db.Task) -> None:
    """An archive stage that does nothing."""


class Runner:
    """Create, execute, and stop runs, and archive tasks."""

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
        finish_leftover: LeftoverFinisher | None = None,
    ) -> None:
        self._conn = conn
        self._adapters = adapters
        self._config = config
        self._prepare = prepare
        self._finalize = finalize
        self._archive = archive
        self._on_event = on_event
        self._finish_leftover = finish_leftover
        self._jobs: dict[int, _Job] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._creation_lock = asyncio.Lock()

    async def start_task(
        self, *, project_id: int, title: str, prompt: str, agent_type: str, model: str | None
    ) -> db.Run:
        """Create a task and its initial run, and start executing it."""
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
        """Send a follow-up instruction to the same session of an existing task as a new run.

        Never refused for the task's cost or token limit: a message starts a new budget.
        """
        async with self._lock_for(task_id):
            task = await db.get_task(self._conn, task_id)
            run = await db.create_run(self._conn, task_id, RunTrigger.USER_MESSAGE, text)
            self._launch(task, run)
        return run

    async def start_ci_fix(self, task_id: int) -> db.Run | None:
        """Create and start a ci_failure run if the task meets the auto-fix conditions, else None.

        The conditions are rechecked under the task lock and inside a DB transaction, so this never
        competes with the user's message for the same session.
        The failed CI log is fetched in the prepare stage, when the run already exists and can be
        stopped.
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

    async def start_conflict_fix(self, task_id: int) -> db.Run | None:
        """Create and start a merge_conflict run if the PR conflicts with its base, else None.

        The merge starts in the prepare stage, when the run already exists and can be stopped.
        """
        limits = status.Limits(self._config.task_cost_limit_usd, self._config.task_token_limit)
        async with self._lock_for(task_id):
            run = await db.create_conflict_run(
                self._conn,
                task_id,
                "The pull request conflicts with its base branch;"
                " the merge is started before the agent runs.",
                condition=lambda snapshot: status.can_resolve_conflict(snapshot, limits),
            )
            if run is None:
                return None
            self._launch(await db.get_task(self._conn, task_id), run)
        return run

    async def start_feedback(self, task_id: int) -> db.Run | None:
        """Create and start a review_feedback run if the PR has unhandled comments, else None.

        The PR comments are read in the same transaction that creates the run, so none are missed or
        duplicated.
        """
        limits = status.Limits(self._config.task_cost_limit_usd, self._config.task_token_limit)
        async with self._lock_for(task_id):
            run = await db.create_feedback_run(
                self._conn,
                task_id,
                build_prompt=lambda events: feedback.build_prompt(
                    [event.payload for event in events]
                ),
                condition=lambda snapshot: status.can_address_feedback(snapshot, limits),
            )
            if run is None:
                return None
            self._launch(await db.get_task(self._conn, task_id), run)
        return run

    async def stop(self, task_id: int) -> None:
        """Stop the task's running run; return only after it has fully stopped and finished.

        A run left unfinished at startup has no job here, because its agent process could not be
        confirmed stopped. Stopping it hands it to `finish_leftover`, which tries again: this is
        the only way to finish such a run from within the tool.
        """
        async with self._lock_for(task_id):
            if await self._stop_job(task_id):
                return
            run = await db.get_open_run(self._conn, task_id)
            if run is None or self._finish_leftover is None:
                raise NoActiveRunError(f"task {task_id} has no active run")
            await self._finish_leftover(run)

    async def stop_if_running(self, task_id: int) -> bool:
        """Stop the task's run if one is running; return whether a run was actually stopped.

        Leftover runs are not touched: callers are automatic (e.g. the PR closed), and must not
        wait on, or fail because of, a process that may not stop.
        """
        async with self._lock_for(task_id):
            return await self._stop_job(task_id)

    async def stop_all(self) -> None:
        """Stop all running runs."""
        for task_id in list(self._jobs):
            await self.stop_if_running(task_id)

    async def archive(self, task_id: int) -> None:
        """Archive a task. Refused while a run is running; stays unarchived if archiving fails."""
        async with self._lock_for(task_id):
            task = await db.get_task(self._conn, task_id)
            if task.archived_at is not None:
                raise db.TaskNotRunnableError(f"task {task_id} is already archived")
            if await db.get_open_run(self._conn, task_id) is not None:
                raise db.RunAlreadyActiveError(f"task {task_id} has an active run; stop it first")
            await self._archive(task)
            await db.archive_task(self._conn, task_id)

    async def wait(self, run_id: int) -> db.Run:
        """Wait for the given run to finish and return its finished data."""
        for job in list(self._jobs.values()):
            if job.run.id == run_id and job.work is not None:
                await asyncio.wait({job.work})
        return await db.get_run(self._conn, run_id)

    async def shutdown(self) -> None:
        """On normal daemon shutdown, stop the runs still running and mark them interrupted."""
        for job in list(self._jobs.values()):
            if job.work is not None and not job.work.done():
                await self._cancel(job, ExitStatus.INTERRUPTED)

    async def _stop_job(self, task_id: int) -> bool:
        """Stop the run this Runner is executing for the task; return whether there was one."""
        job = self._jobs.get(task_id)
        if job is None or job.work is None or job.work.done():
            return False
        await self._cancel(job, ExitStatus.STOPPED)
        return True

    def _lock_for(self, task_id: int) -> asyncio.Lock:
        return self._locks.setdefault(task_id, asyncio.Lock())

    def _launch(self, task: db.Task, run: db.Run) -> None:
        job = _Job(run=run)
        self._jobs[task.id] = job
        deadline = asyncio.get_running_loop().time() + self._config.run_timeout_seconds
        job.work = asyncio.create_task(self._execute(task, job, deadline), name=f"run-{run.id}")

    async def _cancel(self, job: _Job, status: ExitStatus) -> None:
        assert job.work is not None
        # If the asyncio task is cancelled before it starts executing, it never even reaches the
        # code that finishes the run, so the run would never finish.
        await job.has_started.wait()
        # A run that is already finishing must not be cancelled, or the finishing write is cut off
        # halfway and the run never finishes.
        # One whose cancellation was already requested must not be cancelled again either: the
        # second cancel would interrupt the cleanup the agent is doing.
        # A second stopper shows up when the previous caller was itself cancelled while waiting
        # (e.g. the process is about to exit).
        if not job.is_closing and not job.is_cancel_requested:
            job.is_cancel_requested = True
            job.cancel_status = status
            job.work.cancel()
        await asyncio.wait({job.work})

    async def _execute(self, task: db.Task, job: _Job, deadline: float) -> None:
        """Execute a run's prepare, agent, and wrap-up stages; the run is always finished."""

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
            # Cancellation always comes from the Runner itself (Stop or shutdown). Swallow it here
            # so this asyncio task can finish the run and then exit normally.
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
        # The prepare stage may have rewritten what is sent this time (the CI log of a ci_failure
        # run), so reread it.
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
        usage = await db.get_budget_usage(self._conn, task_id)
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
