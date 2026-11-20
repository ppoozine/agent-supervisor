"""A minimal command-line interface for manual verification in phase 1.

`serve` starts the long-running daemon (API, polling and auto-fix). `start`, `message`,
`archive` and `watch` are foreground modes left over from phase 1 that don't need the daemon.
The daemon and the foreground commands share one lock, so only one can run at a time.

- `watch` (or `start` / `message` with `--watch`) keeps polling GitHub and auto-fixing CI
  until Ctrl-C is pressed.
- `stop` tells the running process to stop all runs, but the process itself keeps watching.
- Ctrl-C stops all runs and exits the process.

Both signals only set an event that the command handles at the right moment, so signals
arriving during startup are not lost: Ctrl-C received before the run is created means it is
not created; a `stop` request is held until the run has started.
"""

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from agent_supervisor import daemon, db, status, worktree
from agent_supervisor.adapters.base import AgentAdapter
from agent_supervisor.adapters.claude_code import ClaudeCodeAdapter
from agent_supervisor.adapters.codex import CodexAdapter
from agent_supervisor.config import DEFAULT_CONFIG_PATH, Config, absolute_path, load_config
from agent_supervisor.enums import EventType, ExitStatus
from agent_supervisor.github import GitHub
from agent_supervisor.recovery import (
    AlreadyRunningError,
    InstanceLock,
    ProcessTable,
    SystemProcessTable,
    recover_open_runs,
)
from agent_supervisor.runner import BudgetExhaustedError, Runner, UnknownAgentError
from agent_supervisor.supervisor import Supervisor, build_supervisor

DEFAULT_AGENT = "claude_code"
WATCH_HELP = "keep running after the run starts, polling GitHub and auto-fixing CI until Ctrl-C"
MAX_LINE_CHARS = 200
EXIT_OK, EXIT_FAILED, EXIT_ALREADY_RUNNING = 0, 1, 2
EXPECTED_ERRORS = (
    db.NotFoundError,
    db.RunAlreadyActiveError,
    db.TaskNotRunnableError,
    BudgetExhaustedError,
    UnknownAgentError,
    worktree.GitError,
)


@dataclass(frozen=True)
class _Session:
    """All components a foreground process uses while it runs."""

    supervisor: Supervisor
    quit_requested: asyncio.Event
    stop_requested: asyncio.Event
    poll_interval_seconds: float

    @property
    def runner(self) -> Runner:
        """The component that runs and stops runs."""
        return self.supervisor.runner

    @property
    def conn(self) -> aiosqlite.Connection:
        """The database connection."""
        return self.supervisor.conn


ForegroundAction = Callable[[_Session], Awaitable[int]]


def main() -> None:
    """Command-line entry point."""
    # Watch mode runs for a long time; flush line by line even when output goes to a file or
    # pipe, otherwise progress is invisible.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    code = asyncio.run(
        run(
            sys.argv[1:],
            config=None,
            adapters=default_adapters(),
            processes=SystemProcessTable(),
            github=GitHub(),
        )
    )
    sys.exit(code)


def default_adapters() -> dict[str, AgentAdapter]:
    """Agent types `--agent` accepts and their adapters."""
    return {DEFAULT_AGENT: ClaudeCodeAdapter(), "codex": CodexAdapter()}


async def run(
    argv: Sequence[str],
    *,
    config: Config | None,
    adapters: Mapping[str, AgentAdapter],
    processes: ProcessTable,
    github: GitHub,
) -> int:
    """Parse the arguments, run the matching command and return the exit code.

    When `config` is None, it is read from the config file.
    """
    args = _build_parser().parse_args(argv)
    if config is None:
        config = load_config(Path(args.config))
    if args.command == "stop":
        return _stop_running_instance(config)
    if args.command == "serve":
        return await daemon.serve(config, adapters, processes, github)
    if args.command == "show":
        return await _show(config, args.task_id)
    if args.command == "list":
        return await _list(config)
    if args.command == "project":
        return await _add_project(config, args)

    actions: dict[str, ForegroundAction] = {
        "start": lambda session: _start(session, args),
        "message": lambda session: _message(session, args),
        "archive": lambda session: _archive(session.runner, args),
        "watch": _watch,
    }
    return await _in_foreground(config, adapters, processes, github, actions[args.command])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asv", description="agent-supervisor: phase 1 verification tool"
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH), help="path to the config file"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    project = commands.add_parser("project", help="manage registered repos")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    add = project_commands.add_parser("add", help="register a local repo or update its settings")
    add.add_argument("repo_path", type=absolute_path)
    add.add_argument("--github-repo", help="owner/name (defaults to the origin URL)")
    add.add_argument(
        "--default-branch",
        help="base branch for worktrees (defaults to the remote's default branch)",
    )
    add.add_argument(
        "--require-review",
        action="store_true",
        help="require an approving review before a PR counts as mergeable",
    )

    start = commands.add_parser("start", help="open a task and run it to completion")
    start.add_argument("prompt", help="task description for the agent")
    start.add_argument(
        "--project", type=absolute_path, required=True, help="path to the local repo"
    )
    start.add_argument("--title", help="task title (defaults to the start of the description)")
    start.add_argument("--agent", default=DEFAULT_AGENT, help="agent type: claude_code or codex")
    start.add_argument("--model", help="model (defaults to the agent's default)")
    start.add_argument("--watch", action="store_true", help=WATCH_HELP)

    message = commands.add_parser(
        "message", help="send a follow-up instruction to an existing task's session"
    )
    message.add_argument("task_id", type=int)
    message.add_argument("text")
    message.add_argument("--watch", action="store_true", help=WATCH_HELP)

    commands.add_parser("watch", help="keep polling GitHub and auto-fixing CI until Ctrl-C")
    commands.add_parser("serve", help="start the daemon (API, polling and auto-fix) until Ctrl-C")
    commands.add_parser("stop", help="stop all active runs (a watching process keeps watching)")
    commands.add_parser("list", help="list all tasks with their currently derived board column")

    archive = commands.add_parser("archive", help="archive a task and remove its worktree")
    archive.add_argument("task_id", type=int)

    show = commands.add_parser("show", help="show a task's runs and events")
    show.add_argument("task_id", type=int)
    return parser


async def _in_foreground(
    config: Config,
    adapters: Mapping[str, AgentAdapter],
    processes: ProcessTable,
    github: GitHub,
    action: ForegroundAction,
) -> int:
    """Acquire the single-instance lock, run restart recovery, then run the command."""
    # Install signal handlers before taking the lock: another process may send a signal as soon
    # as it sees the lock is taken, and without a handler by then, SIGUSR1's default action
    # would terminate this process outright.
    quit_requested = asyncio.Event()
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, quit_requested.set)
    loop.add_signal_handler(signal.SIGUSR1, stop_requested.set)
    try:
        return await _with_lock(
            config, adapters, processes, github, action, quit_requested, stop_requested
        )
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGUSR1)


async def _with_lock(
    config: Config,
    adapters: Mapping[str, AgentAdapter],
    processes: ProcessTable,
    github: GitHub,
    action: ForegroundAction,
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
        await _recover(conn, processes)
        supervisor = build_supervisor(conn, config, adapters, github, on_event=_print_event)
        runner = supervisor.runner
        session = _Session(
            supervisor=supervisor,
            quit_requested=quit_requested,
            stop_requested=stop_requested,
            poll_interval_seconds=config.poll_interval_seconds,
        )
        try:
            return await action(session)
        except EXPECTED_ERRORS as error:
            print(str(error), file=sys.stderr)
            return EXIT_FAILED
        finally:
            await runner.shutdown()
    finally:
        await conn.close()
        lock.release()


@asynccontextmanager
async def _stopping_on_request(session: _Session) -> AsyncIterator[None]:
    """While the block runs, stop all runs each time `asv stop` is received.

    A request that arrived before entering the block is still held in the event and is handled
    immediately on entry. So only enter this block once the run has started, otherwise the
    request would be used up while there is no run.
    """
    stopper = asyncio.create_task(_stop_whenever_requested(session))
    try:
        yield
    finally:
        stopper.cancel()


async def _stop_whenever_requested(session: _Session) -> None:
    while True:
        await session.stop_requested.wait()
        session.stop_requested.clear()
        await session.runner.stop_all()


def _refuse_to_start() -> int:
    print("interrupted before the run was created", file=sys.stderr)
    return EXIT_FAILED


async def _start(session: _Session, args: argparse.Namespace) -> int:
    project = await _project_for(session.conn, args.project)
    if session.quit_requested.is_set():
        return _refuse_to_start()
    run = await session.runner.start_task(
        project_id=project.id,
        title=args.title or args.prompt[:60],
        prompt=args.prompt,
        agent_type=args.agent,
        model=args.model,
    )
    return await _follow(session, run, watch=args.watch)


async def _message(session: _Session, args: argparse.Namespace) -> int:
    if session.quit_requested.is_set():
        return _refuse_to_start()
    run = await session.runner.send_message(args.task_id, args.text)
    return await _follow(session, run, watch=args.watch)


async def _archive(runner: Runner, args: argparse.Namespace) -> int:
    await runner.archive(args.task_id)
    print(f"task {args.task_id} archived")
    return EXIT_OK


async def _follow(session: _Session, run: db.Run, *, watch: bool) -> int:
    print(f"task {run.task_id} run {run.id} started ({run.trigger})")
    if watch:
        return await _watch(session)
    async with _stopping_on_request(session):
        finished = await _wait_or_quit(session, run)
    print(_format_run(finished))
    return EXIT_OK if finished.exit_status is ExitStatus.SUCCESS else EXIT_FAILED


async def _wait_or_quit(session: _Session, run: db.Run) -> db.Run:
    """Wait for the run to finish; stop it on Ctrl-C, even if Ctrl-C came while it was created."""
    waiting = asyncio.create_task(session.runner.wait(run.id))
    quitting = asyncio.create_task(session.quit_requested.wait())
    try:
        await asyncio.wait({waiting, quitting}, return_when=asyncio.FIRST_COMPLETED)
        if not waiting.done():
            await session.runner.stop_if_running(run.task_id)
        return await waiting
    finally:
        quitting.cancel()


async def _watch(session: _Session) -> int:
    """Keep polling and dispatching until Ctrl-C; stop any active run before exiting."""
    print(f"watching (every {session.poll_interval_seconds:g}s); press Ctrl-C to quit")
    async with _stopping_on_request(session):
        while not session.quit_requested.is_set():
            await _poll_and_dispatch(session)
            try:
                async with asyncio.timeout(session.poll_interval_seconds):
                    await session.quit_requested.wait()
            except TimeoutError:
                continue
    await session.runner.stop_all()
    return EXIT_OK


async def _poll_and_dispatch(session: _Session) -> None:
    report, runs = await session.supervisor.poll_and_dispatch(session.quit_requested)
    if report is None:
        return
    for change in report.changes:
        current = change.status
        print(
            f"task {change.task_id}: pr={current.pr_state} ci={current.ci_state}"
            f" review={current.review_state} head={current.head_sha[:8]}"
        )
    for task_id, error in report.errors:
        print(f"task {task_id}: polling failed: {error}", file=sys.stderr)
    for run in runs:
        print(f"task {run.task_id} run {run.id} started ({run.trigger})")


async def _project_for(conn: aiosqlite.Connection, repo_path: Path) -> db.Project:
    """Get the registered project; if not registered, register it with detected settings."""
    existing = await db.get_project_by_path(conn, str(repo_path))
    if existing is not None and existing.github_repo is not None:
        return existing
    if existing is not None:
        # Registered but the GitHub repo is unknown (e.g. registered by an older version); fill in
        # the detected value.
        return await db.upsert_project(
            conn,
            str(repo_path),
            github_repo=await worktree.detect_github_repo(repo_path),
            default_branch=existing.default_branch,
            require_review=existing.require_review,
        )
    return await db.upsert_project(
        conn,
        str(repo_path),
        github_repo=await worktree.detect_github_repo(repo_path),
        default_branch=await worktree.detect_default_branch(repo_path),
    )


async def _add_project(config: Config, args: argparse.Namespace) -> int:
    conn = await db.connect(config.db_path)
    try:
        await db.init_schema(conn)
        project = await db.upsert_project(
            conn,
            str(args.repo_path),
            github_repo=args.github_repo or await worktree.detect_github_repo(args.repo_path),
            default_branch=args.default_branch
            or await worktree.detect_default_branch(args.repo_path),
            require_review=args.require_review,
        )
        print(
            f"project {project.id}: {project.repo_path}  github={project.github_repo}"
            f"  base={project.default_branch}  require_review={project.require_review}"
        )
        return EXIT_OK
    finally:
        await conn.close()


async def _recover(conn: aiosqlite.Connection, processes: ProcessTable) -> None:
    report = await recover_open_runs(conn, processes)
    for run_id in report.interrupted_run_ids:
        print(f"run {run_id} from a previous session was marked interrupted")
    for run_id in report.held_run_ids:
        print(
            f"run {run_id} still has a live agent process that could not be stopped; "
            "its task is blocked until you stop that process",
            file=sys.stderr,
        )


def _stop_running_instance(config: Config) -> int:
    try:
        InstanceLock.acquire(config.lock_path).release()
    except AlreadyRunningError:
        pid = InstanceLock.read_owner_pid(config.lock_path)
        if pid is not None:
            os.kill(pid, signal.SIGUSR1)
            print(f"stop requested (pid {pid})")
            return EXIT_OK
    print("nothing is running", file=sys.stderr)
    return EXIT_FAILED


async def _show(config: Config, task_id: int) -> int:
    conn = await db.connect(config.db_path)
    try:
        await db.init_schema(conn)
        try:
            task = await db.get_task(conn, task_id)
        except db.NotFoundError as error:
            print(str(error), file=sys.stderr)
            return EXIT_FAILED
        print(_format_task(task))
        for run in await db.list_runs(conn, task_id):
            print(_format_run(run))
        for event in await db.list_events(conn, task_id):
            print(f"  [{event.id}] run={event.run_id} {_format_event(event.type, event.payload)}")
        return EXIT_OK
    finally:
        await conn.close()


async def _list(config: Config) -> int:
    conn = await db.connect(config.db_path)
    try:
        await db.init_schema(conn)
        limits = status.Limits(config.task_cost_limit_usd, config.task_token_limit)
        for snapshot in await db.list_snapshots(conn):
            print(_format_snapshot(snapshot, status.board_status(snapshot, limits)))
        return EXIT_OK
    finally:
        await conn.close()


def _format_snapshot(snapshot: db.TaskSnapshot, board: status.BoardStatus) -> str:
    task = snapshot.task
    pull_request = "no PR" if task.pr_number is None else f"PR #{task.pr_number}"
    estimate = "" if snapshot.usage.is_complete else " (incomplete)"
    return (
        f"task {task.id}  [{board.column} / {board.label}]  {pull_request}  ci={task.ci_state}"
        f"  fixes={task.ci_fix_attempts}/{status.MAX_CI_FIX_ATTEMPTS}"
        f"  cost~${snapshot.usage.cost_usd:.2f}{estimate}  {task.title}"
    )


def _print_event(event_type: EventType, payload: dict[str, Any]) -> None:
    print(_format_event(event_type, payload))


def _format_task(task: db.Task) -> str:
    pull_request = "no PR" if task.pr_number is None else f"PR #{task.pr_number} {task.pr_state}"
    archived = "  archived" if task.archived_at else ""
    return (
        f"task {task.id}: {task.title}  agent={task.agent_type}  session={task.session_id}\n"
        f"  branch={task.branch}  {pull_request}  ci={task.ci_state}{archived}\n"
        f"  worktree={task.worktree_path}"
    )


def _format_run(run: db.Run) -> str:
    return (
        f"run {run.id} ({run.trigger}): {run.exit_status or 'running'}"
        f"  tokens_in={run.tokens_in} tokens_out={run.tokens_out} cost_usd={run.cost_usd}"
    )


def _format_event(event_type: EventType | str, payload: dict[str, Any]) -> str:
    """Format one event as a single line, truncating overly long content."""
    if event_type == EventType.AGENT_MESSAGE:
        body = f"({payload.get('kind')}) {payload.get('text')}"
    elif event_type in (EventType.COMMAND, EventType.TOOL_CALL):
        subject = payload.get("command") or payload.get("tool") or payload.get("output")
        body = f"{payload.get('phase')} {subject}"
    elif event_type == EventType.PERMISSION_DENIED:
        body = f"{payload.get('tool')} {payload.get('input')} -> {payload.get('reason')}"
    elif event_type == EventType.STATE_CHANGE:
        body = f"{payload.get('change')} {payload.get('exit_status')} {payload.get('detail') or ''}"
    else:
        body = str(payload)
    line = f"{event_type}: {body}".replace("\n", " ")
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"
