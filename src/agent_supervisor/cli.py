"""最小的命令列介面，供階段 1 手動驗證用。

`serve` 啟動常駐的 daemon（API、輪詢與自動修復）。`start`、`message`、`archive`、`watch`
是階段 1 留下來的前景模式，不需要 daemon。daemon 與前景指令共用同一把鎖，
同一時間只能有一個在執行。

- `watch`（或 `start` / `message` 加上 `--watch`）會持續輪詢 GitHub 並自動修復 CI，
  直到按 Ctrl-C。
- `stop` 通知正在執行的那一個程序中止所有 run，但程序本身繼續監看。
- Ctrl-C 中止所有 run 並結束程序。

兩種訊號都只是設定一個事件，由指令在適當的時機處理，所以啟動途中送到的也不會漏掉：
run 建立前收到 Ctrl-C 就不建立；`stop` 的請求留到 run 開始之後才處理。
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
WATCH_HELP = "run 開始後不退出，接著輪詢 GitHub 並自動修復 CI，直到按 Ctrl-C"
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
    """一個前景程序在執行期間用到的所有元件。"""

    supervisor: Supervisor
    quit_requested: asyncio.Event
    stop_requested: asyncio.Event
    poll_interval_seconds: float

    @property
    def runner(self) -> Runner:
        """執行與中止 run 的元件。"""
        return self.supervisor.runner

    @property
    def conn(self) -> aiosqlite.Connection:
        """資料庫連線。"""
        return self.supervisor.conn


ForegroundAction = Callable[[_Session], Awaitable[int]]


def main() -> None:
    """命令列進入點。"""
    # 監看模式會跑很久，輸出導到檔案或管線時也要逐行寫出，不然看不到進度。
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
    """`--agent` 可以指定的 agent 種類與各自的 adapter。"""
    return {DEFAULT_AGENT: ClaudeCodeAdapter(), "codex": CodexAdapter()}


async def run(
    argv: Sequence[str],
    *,
    config: Config | None,
    adapters: Mapping[str, AgentAdapter],
    processes: ProcessTable,
    github: GitHub,
) -> int:
    """解析參數並執行對應的指令，回傳結束代碼。`config` 為 None 時從設定檔讀取。"""
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
    parser = argparse.ArgumentParser(prog="asv", description="agent-supervisor 階段 1 的驗證工具")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="設定檔路徑")
    commands = parser.add_subparsers(dest="command", required=True)

    project = commands.add_parser("project", help="管理已登記的 repo")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    add = project_commands.add_parser("add", help="登記一個本機 repo，或更新它的設定")
    add.add_argument("repo_path", type=absolute_path)
    add.add_argument("--github-repo", help="owner/name（預設從 origin 的網址讀出）")
    add.add_argument("--default-branch", help="worktree 的基底分支（預設讀遠端的預設分支）")
    add.add_argument("--require-review", action="store_true", help="要有 review 核准才算可合併")

    start = commands.add_parser("start", help="開一個任務並執行到結束")
    start.add_argument("prompt", help="給 agent 的任務描述")
    start.add_argument("--project", type=absolute_path, required=True, help="本機 repo 的路徑")
    start.add_argument("--title", help="任務標題（預設取描述的開頭）")
    start.add_argument("--agent", default=DEFAULT_AGENT, help="agent 種類：claude_code 或 codex")
    start.add_argument("--model", help="模型（預設用 agent 的預設值）")
    start.add_argument("--watch", action="store_true", help=WATCH_HELP)

    message = commands.add_parser("message", help="對既有任務的同一個 session 追加指令")
    message.add_argument("task_id", type=int)
    message.add_argument("text")
    message.add_argument("--watch", action="store_true", help=WATCH_HELP)

    commands.add_parser("watch", help="持續輪詢 GitHub 並自動修復 CI，直到按 Ctrl-C")
    commands.add_parser("serve", help="啟動常駐的 daemon：API、輪詢與自動修復，直到按 Ctrl-C")
    commands.add_parser("stop", help="中止所有正在執行的 run（監看中的程序會繼續監看）")
    commands.add_parser("list", help="列出所有任務與目前推導出的看板欄位")

    archive = commands.add_parser("archive", help="封存任務並清掉它的 worktree")
    archive.add_argument("task_id", type=int)

    show = commands.add_parser("show", help="列出任務的 run 與事件")
    show.add_argument("task_id", type=int)
    return parser


async def _in_foreground(
    config: Config,
    adapters: Mapping[str, AgentAdapter],
    processes: ProcessTable,
    github: GitHub,
    action: ForegroundAction,
) -> int:
    """取得單一實例的鎖、做完重啟復原，再執行指令。"""
    # 先裝好訊號處理再拿鎖：別的程序一看到鎖被拿走就可能送訊號過來，
    # 那時還沒有處理函式的話，SIGUSR1 的預設行為會直接結束這個程序。
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
    """區塊執行期間，每收到一次 `asv stop` 就中止所有 run。

    進入區塊之前就送到的請求還留在事件裡，一進來就會處理。所以要等 run 已經開始
    才進入這個區塊，否則請求會在沒有任何 run 的時候被用掉。
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
    """等 run 結案；收到 Ctrl-C 就中止它。Ctrl-C 在 run 建立途中就送到也一樣有效。"""
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
    """持續輪詢與派工，直到收到 Ctrl-C；結束前中止還在跑的 run。"""
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
    """取得已登記的專案；沒登記過就用偵測到的設定登記。"""
    existing = await db.get_project_by_path(conn, str(repo_path))
    if existing is not None and existing.github_repo is not None:
        return existing
    if existing is not None:
        # 登記過但還不知道 GitHub repo（例如較早的版本登記的），補上偵測到的值。
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
    """把一筆事件排成一行，內容過長時截斷。"""
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
