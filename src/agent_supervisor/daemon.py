"""The long-running daemon: the API, Runner, Poller, and Dispatcher run in one process.

Startup order: install signal handlers, take the single-instance lock, and finish restart recovery
before accepting connections.
On shutdown (Ctrl-C or SIGTERM): stop accepting connections first, then stop the runs still in
progress and mark them interrupted.
"""

import asyncio
import logging
import signal
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from agent_supervisor import db
from agent_supervisor.adapters.base import AgentAdapter
from agent_supervisor.api.app import create_app
from agent_supervisor.api.feed import ChangeFeed
from agent_supervisor.config import Config
from agent_supervisor.github import GitHub
from agent_supervisor.notifier import Notifier
from agent_supervisor.readiness import describe
from agent_supervisor.recovery import (
    AlreadyRunningError,
    InstanceLock,
    ProcessTable,
    recover_open_runs,
)
from agent_supervisor.supervisor import Supervisor, build_supervisor

HOST = "127.0.0.1"
GRACEFUL_SHUTDOWN_SECONDS = 3
# Output of the frontend's `npm run build`; if it hasn't been built, only the API is served.
WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"
EXIT_OK, EXIT_ALREADY_RUNNING = 0, 2
logger = logging.getLogger("agent_supervisor.daemon")


class _Server(uvicorn.Server):
    """Keep uvicorn off the signals: the daemon decides when to exit, so it can clean up runs."""

    @contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


async def serve(
    config: Config,
    adapters: Mapping[str, AgentAdapter],
    processes: ProcessTable,
    github: GitHub,
) -> int:
    """Start the daemon and run until an exit signal arrives; return the exit code."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    quit_requested = asyncio.Event()
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Install signal handlers before taking the lock: `asv stop` may send SIGUSR1 as soon as it sees
    # the lock taken, and with no handler in place yet, the default action would kill this process
    # and leave the agent behind.
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
        supervisor = build_supervisor(conn, config, adapters, github, processes=processes)
        await _log_readiness(supervisor)
        try:
            await _run_until_quit(supervisor, quit_requested, stop_requested)
        finally:
            await supervisor.runner.shutdown()
    finally:
        await conn.close()
        lock.release()
    return EXIT_OK


async def _log_readiness(supervisor: Supervisor) -> None:
    """Check which agents and whether gh can be used, and log it. Never logs credentials."""
    readiness = await supervisor.readiness.check()
    for agent_type, status in readiness.agents.items():
        name = supervisor.adapters[agent_type].display_name
        level = logging.INFO if status.usable else logging.WARNING
        logger.log(level, "%s (%s): %s", name, agent_type, describe(status))
    logger.log(
        logging.INFO if readiness.github.usable else logging.WARNING,
        "gh: %s",
        describe(readiness.github),
    )
    if not readiness.usable_agents:
        logger.warning(
            "no agent can be used; see http://%s:%s/#/settings for what is missing",
            HOST,
            supervisor.config.api_port,
        )


async def _run_until_quit(
    supervisor: Supervisor, quit_requested: asyncio.Event, stop_requested: asyncio.Event
) -> None:
    config = supervisor.config
    notifier = Notifier(enabled=config.notify_needs_you)
    feed = ChangeFeed(supervisor, on_needs_you=notifier.card_needs_you)
    await feed.start()
    server = _Server(
        uvicorn.Config(
            create_app(
                supervisor,
                feed,
                is_closing=quit_requested.is_set,
                static_dir=WEB_DIST if WEB_DIST.is_dir() else None,
            ),
            host=HOST,
            port=config.api_port,
            log_level="info",
            # Without this, uvicorn keeps waiting for connections that haven't closed; an SSE
            # connection whose client stopped reading would keep the daemon from shutting down, and
            # nobody would clean up the runs.
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
        # When the server exits on its own (e.g. the port is in use), the other work must stop too.
        quit_requested.set()
        await asyncio.wait({polling})
        for worker in workers:
            worker.cancel()
        await asyncio.wait(workers)


async def _exit_when_quitting(server: uvicorn.Server, quit_requested: asyncio.Event) -> None:
    await quit_requested.wait()
    server.should_exit = True


async def _stop_whenever_requested(supervisor: Supervisor, stop_requested: asyncio.Event) -> None:
    """Stop all runs each time `asv stop` arrives; the daemon keeps running."""
    while True:
        await stop_requested.wait()
        stop_requested.clear()
        await supervisor.runner.stop_all()


async def _poll_until_quit(supervisor: Supervisor, quit_requested: asyncio.Event) -> None:
    """Poll GitHub and dispatch at the configured interval. Log a failed round and go on."""
    interval = supervisor.config.poll_interval_seconds
    while not quit_requested.is_set():
        try:
            await _poll_once(supervisor, quit_requested)
        except Exception:
            # If this job stopped silently, no task's state would be updated or auto-fixed any more.
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
