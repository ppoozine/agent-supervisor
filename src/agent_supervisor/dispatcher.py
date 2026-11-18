"""Dispatcher：發現符合自動修復條件的任務，就交給 Runner 建立 ci_failure run。

這裡只負責「找出候選」。條件的最終檢查、建立 run、更新防迴圈欄位
由 Runner 在任務鎖與同一個資料庫交易內完成，所以這裡看到的舊資料不會造成重複派工。
"""

from collections.abc import Callable

import aiosqlite

from agent_supervisor import db, status
from agent_supervisor.runner import Runner


class Dispatcher:
    """把 CI 失敗的任務派給 Runner 修復。"""

    def __init__(self, conn: aiosqlite.Connection, runner: Runner, limits: status.Limits) -> None:
        self._conn = conn
        self._runner = runner
        self._limits = limits

    async def dispatch_once(
        self, *, should_stop: Callable[[], bool] = lambda: False
    ) -> list[db.Run]:
        """檢查所有任務一次，回傳這次建立的 run。

        每個任務派工前都先問 `should_stop`；回答 True 就不再派剩下的任務。
        """
        started: list[db.Run] = []
        for snapshot in await db.list_snapshots(self._conn):
            if should_stop():
                break
            if not status.can_auto_fix(snapshot, self._limits):
                continue
            run = await self._runner.start_ci_fix(snapshot.task.id)
            if run is not None:
                started.append(run)
        return started
