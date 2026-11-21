"""Assemble the Runner, Poller, and Dispatcher, plus the shared "poll once, then dispatch".

The foreground CLI commands and the long-running daemon both use this assembly, so their behavior
doesn't drift apart.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial

import aiosqlite

from agent_supervisor import db, status
from agent_supervisor.adapters.base import AgentAdapter
from agent_supervisor.config import Config
from agent_supervisor.dispatcher import Dispatcher
from agent_supervisor.github import GitHub
from agent_supervisor.poller import Poller, PollReport
from agent_supervisor.readiness import ReadinessChecker
from agent_supervisor.recovery import ProcessTable, finish_leftover_run
from agent_supervisor.runner import EventCallback, Runner
from agent_supervisor.stages import RunStages


@dataclass(frozen=True)
class Supervisor:
    """All the components a process uses while it runs."""

    config: Config
    conn: aiosqlite.Connection
    adapters: Mapping[str, AgentAdapter]
    runner: Runner
    poller: Poller
    dispatcher: Dispatcher
    limits: status.Limits
    readiness: ReadinessChecker

    def reports_cost(self, agent_type: str) -> bool:
        """Whether this kind of agent reports cost; if not, cost is "unknown" rather than zero."""
        adapter = self.adapters.get(agent_type)
        return adapter is not None and adapter.capabilities.reports_cost

    async def send_message(self, task_id: int, text: str) -> db.Run:
        """Send a follow-up instruction, first checking whether a closed PR was reopened.

        Closed PRs are only polled every few rounds, so without this check a message sent right
        after reopening the PR would still be refused.
        """
        await self.poller.check_reopened(task_id)
        return await self.runner.send_message(task_id, text)

    async def poll_and_dispatch(
        self, quit_requested: asyncio.Event
    ) -> tuple[PollReport | None, list[db.Run]]:
        """Query GitHub once, then dispatch eligible tasks for fixing.

        If a shutdown is requested midway: the query is abandoned outright (the returned report is
        None), and dispatch stops between tasks.
        Only the query can be cancelled midway (the `gh` child process is terminated). Dispatch
        cannot:
        being cancelled between creating a run and starting it would leave a run nobody owns.
        """
        report = await self._poll_or_quit(quit_requested)
        if report is None:
            return None, []
        # Once shutdown is requested, dispatch no more; otherwise the new run is stopped right away,
        # wasting a fix attempt.
        # Tasks whose new PR comments were first seen this round wait: when several comments arrive
        # in a row, handle them all at once after they stop.
        # Tasks whose query failed also wait, since we don't know whether they have new PR comments.
        hold_feedback = {*report.new_feedback, *(task_id for task_id, _ in report.errors)}
        runs = await self.dispatcher.dispatch_once(
            should_stop=quit_requested.is_set, hold_feedback=hold_feedback
        )
        return report, runs

    async def _poll_or_quit(self, quit_requested: asyncio.Event) -> PollReport | None:
        polling = asyncio.create_task(self.poller.poll_once())
        quitting = asyncio.create_task(quit_requested.wait())
        try:
            await asyncio.wait({polling, quitting}, return_when=asyncio.FIRST_COMPLETED)
            if polling.done():
                return polling.result()
            return None
        finally:
            quitting.cancel()
            if not polling.done():
                polling.cancel()
                await asyncio.wait({polling})


def build_supervisor(
    conn: aiosqlite.Connection,
    config: Config,
    adapters: Mapping[str, AgentAdapter],
    github: GitHub,
    *,
    on_event: EventCallback | None = None,
    processes: ProcessTable | None = None,
) -> Supervisor:
    """Build all components from a DB connection that is open and done with restart recovery.

    Without `processes`, runs left unfinished at startup cannot be finished with Stop.
    """
    stages = RunStages(conn, config, github)
    runner = Runner(
        conn,
        adapters,
        config,
        prepare=stages.prepare,
        finalize=stages.finalize,
        archive=stages.archive,
        on_event=on_event,
        finish_leftover=(
            None if processes is None else partial(finish_leftover_run, conn, processes=processes)
        ),
    )
    limits = status.Limits(config.task_cost_limit_usd, config.task_token_limit)
    return Supervisor(
        config=config,
        conn=conn,
        adapters=adapters,
        runner=runner,
        poller=Poller(conn, github, stop_run=runner.stop_if_running),
        dispatcher=Dispatcher(conn, runner, limits),
        limits=limits,
        readiness=ReadinessChecker(adapters, github),
    )
