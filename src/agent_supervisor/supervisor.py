"""把 Runner、Poller、Dispatcher 組裝在一起，以及「輪詢一輪再派工」這個共用的動作。

前景的 CLI 指令與常駐的 daemon 都用這裡的組裝，兩邊的行為才不會走樣。
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

import aiosqlite

from agent_supervisor import db, status
from agent_supervisor.adapters.base import AgentAdapter
from agent_supervisor.config import Config
from agent_supervisor.dispatcher import Dispatcher
from agent_supervisor.github import GitHub
from agent_supervisor.poller import Poller, PollReport
from agent_supervisor.runner import EventCallback, Runner
from agent_supervisor.stages import RunStages


@dataclass(frozen=True)
class Supervisor:
    """一個程序在執行期間用到的所有元件。"""

    config: Config
    conn: aiosqlite.Connection
    adapters: Mapping[str, AgentAdapter]
    runner: Runner
    poller: Poller
    dispatcher: Dispatcher
    limits: status.Limits

    def reports_cost(self, agent_type: str) -> bool:
        """這種 agent 會不會回報花費；不會的話，花費是「未知」而不是零。"""
        adapter = self.adapters.get(agent_type)
        return adapter is not None and adapter.capabilities.reports_cost

    async def poll_and_dispatch(
        self, quit_requested: asyncio.Event
    ) -> tuple[PollReport | None, list[db.Run]]:
        """查詢 GitHub 一輪，再把符合條件的任務派去修復。

        途中收到結束的要求時：查詢直接放棄（回傳的報告是 None），派工則在任務之間停下。
        只有查詢可以中途取消（`gh` 的子程序會被終止）。派工不行：
        建立 run 與開始執行之間被取消的話，會留下一個沒有人負責的 run。
        """
        report = await self._poll_or_quit(quit_requested)
        if report is None:
            return None, []
        # 收到結束的要求之後不再派工，否則新的 run 馬上被中止、白扣一次修復。
        runs = await self.dispatcher.dispatch_once(should_stop=quit_requested.is_set)
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
) -> Supervisor:
    """用一條已經開好、做完重啟復原的資料庫連線組出所有元件。"""
    stages = RunStages(conn, config, github)
    runner = Runner(
        conn,
        adapters,
        config,
        prepare=stages.prepare,
        finalize=stages.finalize,
        archive=stages.archive,
        on_event=on_event,
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
    )
