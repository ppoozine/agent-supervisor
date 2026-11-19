"""常駐的 daemon：API、Runner、Poller、Dispatcher 跑在同一個 process。

啟動順序：先裝好訊號處理、拿到單一實例的鎖、做完重啟復原，才開始接受連線。
關閉時（Ctrl-C 或 SIGTERM）先停止接受連線，再把還在跑的 run 中止並標成 interrupted。
"""

import asyncio
import logging
import signal
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager

import uvicorn

from agent_supervisor import db
from agent_supervisor.adapters.base import AgentAdapter
from agent_supervisor.api.app import create_app
from agent_supervisor.api.feed import ChangeFeed
from agent_supervisor.config import Config
from agent_supervisor.github import GitHub
from agent_supervisor.recovery import (
    AlreadyRunningError,
    InstanceLock,
    ProcessTable,
    recover_open_runs,
)
from agent_supervisor.supervisor import Supervisor, build_supervisor

HOST = "127.0.0.1"
GRACEFUL_SHUTDOWN_SECONDS = 3
EXIT_OK, EXIT_ALREADY_RUNNING = 0, 2
logger = logging.getLogger("agent_supervisor.daemon")


class _Server(uvicorn.Server):
    """不讓 uvicorn 自己接管訊號：結束的時機由 daemon 決定，才能在之後收拾 run。"""

    @contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


async def serve(
    config: Config,
    adapters: Mapping[str, AgentAdapter],
    processes: ProcessTable,
    github: GitHub,
) -> int:
    """啟動 daemon 並執行到收到結束的訊號，回傳結束代碼。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    quit_requested = asyncio.Event()
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    # 先裝好訊號處理再拿鎖：`asv stop` 一看到鎖被拿走就可能送 SIGUSR1 過來，
    # 那時還沒有處理函式的話，預設行為會直接結束這個程序、留下 agent。
    handlers = {
        signal.SIGINT: quit_requested.set,
        signal.SIGTERM: quit_requested.set,
        signal.SIGUSR1: stop_requested.set,
    }
    for signal_number, handler in handlers.items():
        loop.add_signal_handler(signal_number, handler)
    try:
        return await _serve_with_lock(
            config, adapters, processes, github, quit_requested, stop_requested
        )
    finally:
        for signal_number in handlers:
            loop.remove_signal_handler(signal_number)


async def _serve_with_lock(
    config: Config,
    adapters: Mapping[str, AgentAdapter],
    processes: ProcessTable,
    github: GitHub,
    quit_requested: asyncio.Event,
    stop_requested: asyncio.Event,
) -> int:
    try:
        lock = InstanceLock.acquire(config.lock_path)
    except AlreadyRunningError:
        print("another asv instance is already running", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    conn = await db.connect(config.db_path)
    try:
        await db.init_schema(conn)
        report = await recover_open_runs(conn, processes)
        for run_id in report.interrupted_run_ids:
            logger.info("run %s from a previous session was marked interrupted", run_id)
        for run_id in report.held_run_ids:
            logger.error(
                "run %s still has a live agent process that could not be stopped;"
                " its task is blocked until you stop that process",
                run_id,
            )
        supervisor = build_supervisor(conn, config, adapters, github)
        try:
            await _run_until_quit(supervisor, quit_requested, stop_requested)
        finally:
            await supervisor.runner.shutdown()
    finally:
        await conn.close()
        lock.release()
    return EXIT_OK


async def _run_until_quit(
    supervisor: Supervisor, quit_requested: asyncio.Event, stop_requested: asyncio.Event
) -> None:
    config = supervisor.config
    feed = ChangeFeed(supervisor)
    await feed.start()
    server = _Server(
        uvicorn.Config(
            create_app(supervisor, feed, is_closing=quit_requested.is_set),
            host=HOST,
            port=config.api_port,
            log_level="info",
            # 不設的話 uvicorn 會一直等沒結束的連線；對方不再讀取的 SSE 連線
            # 會讓 daemon 關不掉，run 也就沒人收拾。
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
    )
    workers = [
        asyncio.create_task(feed.run()),
        asyncio.create_task(_stop_whenever_requested(supervisor, stop_requested)),
        asyncio.create_task(_exit_when_quitting(server, quit_requested)),
    ]
    polling = asyncio.create_task(_poll_until_quit(supervisor, quit_requested))
    try:
        await server.serve()
    finally:
        # 伺服器自己結束時（例如連接埠被占用）也要讓其他工作停下來。
        quit_requested.set()
        await asyncio.wait({polling})
        for worker in workers:
            worker.cancel()
        await asyncio.wait(workers)


async def _exit_when_quitting(server: uvicorn.Server, quit_requested: asyncio.Event) -> None:
    await quit_requested.wait()
    server.should_exit = True


async def _stop_whenever_requested(supervisor: Supervisor, stop_requested: asyncio.Event) -> None:
    """每收到一次 `asv stop` 就中止所有 run；daemon 繼續執行。"""
    while True:
        await stop_requested.wait()
        stop_requested.clear()
        await supervisor.runner.stop_all()


async def _poll_until_quit(supervisor: Supervisor, quit_requested: asyncio.Event) -> None:
    """依設定的間隔輪詢 GitHub 並派工。一輪出錯就記下來，下一輪照常進行。"""
    interval = supervisor.config.poll_interval_seconds
    while not quit_requested.is_set():
        try:
            await _poll_once(supervisor, quit_requested)
        except Exception:
            # 這個工作無聲地停掉的話，所有任務的狀態都不再更新、也不再自動修復。
            logger.exception("polling round failed; trying again in %gs", interval)
        try:
            async with asyncio.timeout(interval):
                await quit_requested.wait()
        except TimeoutError:
            continue


async def _poll_once(supervisor: Supervisor, quit_requested: asyncio.Event) -> None:
    report, runs = await supervisor.poll_and_dispatch(quit_requested)
    if report is None:
        return
    for change in report.changes:
        current = change.status
        logger.info(
            "task %s: pr=%s ci=%s review=%s head=%s",
            change.task_id,
            current.pr_state,
            current.ci_state,
            current.review_state,
            current.head_sha[:8],
        )
    for task_id, error in report.errors:
        logger.warning("task %s: polling failed: %s", task_id, error)
    for run in runs:
        logger.info("task %s run %s started (%s)", run.task_id, run.id, run.trigger)
