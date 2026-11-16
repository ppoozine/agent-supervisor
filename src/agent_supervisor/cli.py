"""最小的命令列介面，供階段 1 手動驗證用。

階段 2 之前沒有常駐的 daemon：`start`、`message`、`archive` 會在前景執行到結束。
同一時間只能有一個這樣的程序。`stop` 會通知正在執行的那一個停下來。
"""

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import aiosqlite

from agent_supervisor import db, worktree
from agent_supervisor.adapters.base import AgentAdapter
from agent_supervisor.adapters.claude_code import ClaudeCodeAdapter
from agent_supervisor.config import DEFAULT_CONFIG_PATH, Config, load_config
from agent_supervisor.enums import EventType, ExitStatus
from agent_supervisor.github import GitHub
from agent_supervisor.recovery import (
    AlreadyRunningError,
    InstanceLock,
    ProcessTable,
    SystemProcessTable,
    recover_open_runs,
)
from agent_supervisor.runner import (
    BudgetExhaustedError,
    NoActiveRunError,
    Runner,
    UnknownAgentError,
)
from agent_supervisor.stages import RunStages

DEFAULT_AGENT = "claude_code"
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

ForegroundAction = Callable[[Runner, aiosqlite.Connection, asyncio.Event], Awaitable[int]]


def main() -> None:
    """命令列進入點。"""
    adapters: dict[str, AgentAdapter] = {DEFAULT_AGENT: ClaudeCodeAdapter()}
    code = asyncio.run(
        run(
            sys.argv[1:],
            config=None,
            adapters=adapters,
            processes=SystemProcessTable(),
            github=GitHub(),
        )
    )
    sys.exit(code)


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
    if args.command == "show":
        return await _show(config, args.task_id)
    if args.command == "project":
        return await _add_project(config, args)

    actions: dict[str, ForegroundAction] = {
        "start": lambda runner, conn, stop: _start(runner, conn, stop, args),
        "message": lambda runner, conn, stop: _message(runner, stop, args),
        "archive": lambda runner, conn, stop: _archive(runner, args),
    }
    return await _in_foreground(config, adapters, processes, github, actions[args.command])


def _absolute_path(value: str) -> Path:
    return Path(value).resolve()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asv", description="agent-supervisor 階段 1 的驗證工具")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="設定檔路徑")
    commands = parser.add_subparsers(dest="command", required=True)

    project = commands.add_parser("project", help="管理已登記的 repo")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    add = project_commands.add_parser("add", help="登記一個本機 repo，或更新它的設定")
    add.add_argument("repo_path", type=_absolute_path)
    add.add_argument("--github-repo", help="owner/name（預設從 origin 的網址讀出）")
    add.add_argument("--default-branch", help="worktree 的基底分支（預設讀遠端的預設分支）")
    add.add_argument("--require-review", action="store_true", help="要有 review 核准才算可合併")

    start = commands.add_parser("start", help="開一個任務並執行到結束")
    start.add_argument("prompt", help="給 agent 的任務描述")
    start.add_argument("--project", type=_absolute_path, required=True, help="本機 repo 的路徑")
    start.add_argument("--title", help="任務標題（預設取描述的開頭）")
    start.add_argument("--agent", default=DEFAULT_AGENT, help="agent 種類")
    start.add_argument("--model", help="模型（預設用 agent 的預設值）")

    message = commands.add_parser("message", help="對既有任務的同一個 session 追加指令")
    message.add_argument("task_id", type=int)
    message.add_argument("text")

    commands.add_parser("stop", help="中止正在執行的 run")

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
    try:
        lock = InstanceLock.acquire(config.lock_path)
    except AlreadyRunningError:
        print("another asv instance is already running", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    # 一拿到鎖就開始接收停止訊號（Ctrl-C 或 `asv stop`），
    # 這樣訊號在 run 剛建立、還沒開始等待的那一刻送到也不會漏掉。
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop_requested.set)
    conn = await db.connect(config.db_path)
    try:
        await db.init_schema(conn)
        await _recover(conn, processes)
        stages = RunStages(conn, config, github)
        runner = Runner(
            conn,
            adapters,
            config,
            prepare=stages.prepare,
            finalize=stages.finalize,
            archive=stages.archive,
            on_event=_print_event,
        )
        try:
            return await action(runner, conn, stop_requested)
        except EXPECTED_ERRORS as error:
            print(str(error), file=sys.stderr)
            return EXIT_FAILED
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        await conn.close()
        lock.release()


async def _start(
    runner: Runner, conn: aiosqlite.Connection, stop: asyncio.Event, args: argparse.Namespace
) -> int:
    project = await _project_for(conn, args.project)
    run = await runner.start_task(
        project_id=project.id,
        title=args.title or args.prompt[:60],
        prompt=args.prompt,
        agent_type=args.agent,
        model=args.model,
    )
    return await _follow(runner, run, stop)


async def _message(runner: Runner, stop: asyncio.Event, args: argparse.Namespace) -> int:
    return await _follow(runner, await runner.send_message(args.task_id, args.text), stop)


async def _archive(runner: Runner, args: argparse.Namespace) -> int:
    await runner.archive(args.task_id)
    print(f"task {args.task_id} archived")
    return EXIT_OK


async def _follow(runner: Runner, run: db.Run, stop: asyncio.Event) -> int:
    print(f"task {run.task_id} run {run.id} started ({run.trigger})")
    finished = await _wait_or_stop(runner, run, stop)
    print(_format_run(finished))
    return EXIT_OK if finished.exit_status is ExitStatus.SUCCESS else EXIT_FAILED


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


async def _wait_or_stop(runner: Runner, run: db.Run, stop: asyncio.Event) -> db.Run:
    """等 run 結束；期間收到停止訊號就中止它。"""
    waiting = asyncio.create_task(runner.wait(run.id))
    stop_signal = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({waiting, stop_signal}, return_when=asyncio.FIRST_COMPLETED)
        if stop_signal.done():
            await _stop_quietly(runner, run.task_id)
        return await waiting
    finally:
        stop_signal.cancel()


async def _stop_quietly(runner: Runner, task_id: int) -> None:
    try:
        await runner.stop(task_id)
    except NoActiveRunError:
        return


def _stop_running_instance(config: Config) -> int:
    try:
        InstanceLock.acquire(config.lock_path).release()
    except AlreadyRunningError:
        pid = InstanceLock.read_owner_pid(config.lock_path)
        if pid is not None:
            os.kill(pid, signal.SIGINT)
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
