"""Dispatcher: find tasks that need the agent and hand them to the Runner to create a run.

Three cases, in priority order: unhandled PR comments (review_feedback), conflicts between the PR
and the base branch (merge_conflict), and CI failure (ci_failure).
This module only "finds candidates". The final condition check, creating the run, and updating the
loop guard columns are done by the Runner under the task lock in one DB transaction, so stale data
seen here never causes duplicate dispatch.
"""

from collections.abc import Callable, Collection

import aiosqlite

from agent_supervisor import db, status
from agent_supervisor.runner import Runner


class Dispatcher:
    """Dispatch tasks with new PR comments or CI failures to the Runner."""

    def __init__(self, conn: aiosqlite.Connection, runner: Runner, limits: status.Limits) -> None:
        self._conn = conn
        self._runner = runner
        self._limits = limits

    async def dispatch_once(
        self,
        *,
        should_stop: Callable[[], bool] = lambda: False,
        hold_feedback: Collection[int] = (),
    ) -> list[db.Run]:
        """Check every task once and return the runs created this time.

        Ask `should_stop` before dispatching each task; once it answers True, skip the remaining
        tasks.
        Tasks in `hold_feedback` don't get their PR comments handled this round: more comments may
        still be coming, so wait for the next round.
        PR comments take priority over conflicts and CI failures: handling them produces a new
        commit, so the old commit's failures no longer need fixing.
        Conflicts take priority over CI failures: CI does not run on a PR with conflicts, so they
        must be resolved before the CI result is known.
        This order matches the order in which `status.board_status` decides the Working label.
        """
        started: list[db.Run] = []
        for snapshot in await db.list_snapshots(self._conn):
            if should_stop():
                break
            run = await self._dispatch(snapshot, hold_feedback)
            if run is not None:
                started.append(run)
        return started

    async def _dispatch(
        self, snapshot: db.TaskSnapshot, hold_feedback: Collection[int]
    ) -> db.Run | None:
        task_id = snapshot.task.id
        if status.can_address_feedback(snapshot, self._limits):
            if task_id in hold_feedback:
                return None
            return await self._runner.start_feedback(task_id)
        if status.can_resolve_conflict(snapshot, self._limits):
            return await self._runner.start_conflict_fix(task_id)
        if status.can_auto_fix(snapshot, self._limits):
            return await self._runner.start_ci_fix(task_id)
        return None
