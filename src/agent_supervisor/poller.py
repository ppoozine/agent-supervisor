"""Poller：定期查詢有開 PR 的任務，更新對 GitHub 狀態的快取。

PR 狀態、最新 commit、CI 結果、review 結果當成一組一起更新。
查詢 GitHub 需要時間，期間 supervisor 可能剛 push 了新的 commit，
所以寫入時會比對查詢前看到的 `head_sha`，不一樣就丟棄這次結果。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiosqlite

from agent_supervisor import db
from agent_supervisor.enums import PrState
from agent_supervisor.github import GitHub, GitHubError, PullRequestStatus
from agent_supervisor.process import CommandTimeoutError

StopRun = Callable[[int], Awaitable[bool]]


@dataclass(frozen=True)
class PollChange:
    """一次輪詢中，某個任務的狀態有了變化。"""

    task_id: int
    status: PullRequestStatus


@dataclass(frozen=True)
class PollReport:
    """一次輪詢的結果：哪些任務有變化、哪些查詢失敗。"""

    changes: tuple[PollChange, ...]
    errors: tuple[tuple[int, str], ...]


class Poller:
    """查詢 GitHub 並更新任務的狀態快取。"""

    def __init__(self, conn: aiosqlite.Connection, github: GitHub, *, stop_run: StopRun) -> None:
        self._conn = conn
        self._github = github
        self._stop_run = stop_run

    async def poll_once(self) -> PollReport:
        """查詢所有 PR 開著的任務一次。單一任務查詢失敗不影響其他任務。"""
        changes: list[PollChange] = []
        errors: list[tuple[int, str]] = []
        for task in await db.list_tasks_with_open_pr(self._conn):
            try:
                change = await self._poll_task(task)
            except (GitHubError, CommandTimeoutError) as error:
                errors.append((task.id, str(error)))
                continue
            if change is not None:
                changes.append(change)
        return PollReport(tuple(changes), tuple(errors))

    async def _poll_task(self, task: db.Task) -> PollChange | None:
        project = await db.get_project(self._conn, task.project_id)
        if project.github_repo is None or task.pr_number is None:
            return None
        seen_head_sha = task.head_sha
        status = await self._github.get_pull_request_status(project.github_repo, task.pr_number)
        changed = await db.apply_github_state(
            self._conn,
            task.id,
            expected_head_sha=seen_head_sha,
            pr_state=status.pr_state,
            head_sha=status.head_sha,
            ci_state=status.ci_state,
            review_state=status.review_state,
        )
        if not changed:
            return None
        if status.pr_state is not PrState.OPEN:
            # PR 已經合併或關閉：還在跑的 run 沒有意義了，中止它，不做收尾。
            await self._stop_run(task.id)
        return PollChange(task.id, status)
