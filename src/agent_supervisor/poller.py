"""Poller: periodically query tasks with an open PR and update the cached GitHub state.

PR state, latest commit, CI result, and review result are updated together as one set.
Querying GitHub takes time, and the supervisor may have just pushed a new commit meanwhile, so the
write compares against the `head_sha` seen before the query and discards the result if it differs.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiosqlite

from agent_supervisor import db, feedback
from agent_supervisor.enums import PrState
from agent_supervisor.github import GitHub, GitHubError, PullRequestStatus
from agent_supervisor.process import CommandTimeoutError

StopRun = Callable[[int], Awaitable[bool]]


@dataclass(frozen=True)
class PollChange:
    """A change in one task's state found during a poll."""

    task_id: int
    status: PullRequestStatus


@dataclass(frozen=True)
class PollReport:
    """One poll's result: changed tasks, failed queries, and tasks with new PR comments."""

    changes: tuple[PollChange, ...]
    errors: tuple[tuple[int, str], ...]
    new_feedback: tuple[int, ...] = ()


class Poller:
    """Query GitHub and update the tasks' cached state."""

    def __init__(self, conn: aiosqlite.Connection, github: GitHub, *, stop_run: StopRun) -> None:
        self._conn = conn
        self._github = github
        self._stop_run = stop_run

    async def poll_once(self) -> PollReport:
        """Query every task with an open PR once. One failed task query doesn't affect others."""
        changes: list[PollChange] = []
        errors: list[tuple[int, str]] = []
        new_feedback: list[int] = []
        for task in await db.list_tasks_with_open_pr(self._conn):
            project = await db.get_project(self._conn, task.project_id)
            if project.github_repo is None or task.pr_number is None:
                continue
            try:
                status = await self._github.get_pull_request_status(
                    project.github_repo, task.pr_number
                )
                change = await self._apply(task, status)
                if change is not None:
                    changes.append(change)
                if await self._record_feedback(task, project.github_repo, status):
                    new_feedback.append(task.id)
            except (GitHubError, CommandTimeoutError) as error:
                errors.append((task.id, str(error)))
        return PollReport(tuple(changes), tuple(errors), tuple(new_feedback))

    async def _record_feedback(self, task: db.Task, repo: str, status: PullRequestStatus) -> bool:
        """Fetch unrecorded PR comments as events; return whether there are new ones to handle.

        Archived tasks only track the PR outcome and don't fetch comments. Full comment contents
        need a separate query, so first use the IDs that come back with the state query to tell
        whether there are new ones, and skip the extra query if not.
        """
        assert task.pr_number is not None
        if task.archived_at is not None or status.pr_state is not PrState.OPEN:
            return False
        known = await db.list_feedback_ids(self._conn, task.id)
        if set(status.feedback_ids) <= known:
            return False
        items = await self._github.list_feedback(repo, task.pr_number)
        payloads = [feedback.to_payload(item) for item in items]
        return await db.record_feedback(self._conn, task.id, payloads) > 0

    async def _apply(self, task: db.Task, status: PullRequestStatus) -> PollChange | None:
        seen_head_sha = task.head_sha
        changed = await db.apply_github_state(
            self._conn,
            task.id,
            expected_head_sha=seen_head_sha,
            pr_state=status.pr_state,
            head_sha=status.head_sha,
            ci_state=status.ci_state,
            review_state=status.review_state,
            merge_state=status.merge_state,
        )
        if not changed:
            return None
        if status.pr_state is not PrState.OPEN:
            # The PR was merged or closed: the run still in progress is pointless now, so stop it
            # without wrap-up.
            await self._stop_run(task.id)
        return PollChange(task.id, status)
