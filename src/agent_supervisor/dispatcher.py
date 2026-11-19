"""Dispatcher：發現需要 agent 處理的任務，就交給 Runner 建立對應的 run。

兩種情況：PR 上有還沒處理的留言（review_feedback），或 CI 失敗（ci_failure）。
這裡只負責「找出候選」。條件的最終檢查、建立 run、更新防迴圈欄位
由 Runner 在任務鎖與同一個資料庫交易內完成，所以這裡看到的舊資料不會造成重複派工。
"""

from collections.abc import Callable, Collection

import aiosqlite

from agent_supervisor import db, status
from agent_supervisor.runner import Runner


class Dispatcher:
    """把有新留言或 CI 失敗的任務派給 Runner 處理。"""

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
        """檢查所有任務一次，回傳這次建立的 run。

        每個任務派工前都先問 `should_stop`；回答 True 就不再派剩下的任務。
        `hold_feedback` 裡的任務這一輪先不處理留言：留言可能還沒留完，等下一輪。
        留言優先於 CI 失敗：處理留言會產生新的 commit，舊 commit 的失敗就不用修了。
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
        if status.can_auto_fix(snapshot, self._limits):
            return await self._runner.start_ci_fix(task_id)
        return None
