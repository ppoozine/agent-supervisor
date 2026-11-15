"""最小的命令列介面，供階段 1 手動驗證用。

階段 2 之前沒有常駐的 daemon：`start` 與 `message` 會在前景執行到 run 結束。
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

from agent_supervisor import db
from agent_supervisor.adapters.base import AgentAdapter
from agent_supervisor.adapters.claude_code import ClaudeCodeAdapter
from agent_supervisor.config import DEFAULT_CONFIG_PATH, Config, load_config
from agent_supervisor.enums import EventType, ExitStatus
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

DEFAULT_AGENT = "claude_code"
MAX_LINE_CHARS = 200
EXIT_OK, EXIT_FAILED, EXIT_ALREADY_RUNNING = 0, 1, 2

Action = Callable[[Runner], Awaitable[db.Run]]


def main() -> None:
    """命令列進入點。"""
    adapters: dict[str, AgentAdapter] = {DEFAULT_AGENT: ClaudeCodeAdapter()}
    code = asyncio.run(
        run(sys.argv[1:], config=None, adapters=adapters, processes=SystemProcessTable())
    )
    sys.exit(code)


async def run(
    argv: Sequence[str],
    *,
    config: Config | None,
    adapters: Mapping[str, AgentAdapter],
    processes: ProcessTable,
) -> int:
    """解析參數並執行對應的指令，回傳結束代碼。`config` 為 None 時從設定檔讀取。"""
    args = _build_parser().parse_args(argv)
    if config is None:
        config = load_config(Path(args.config))
    if args.command == "stop":
        return _stop_running_instance(config)
    if args.command == "show":
        return await _show(config, args.task_id)
    if args.command == "start":
        cwd: Path = args.cwd

        async def action(runner: Runner) -> db.Run:
            return await runner.start_task(
                repo_path=str(cwd),
                title=args.title or args.prompt[:60],
                prompt=args.prompt,
                agent_type=args.agent,
                model=args.model,
                cwd=cwd,
            )
    else:

        async def action(runner: Runner) -> db.Run:
            return await runner.send_message(args.task_id, args.text)

    return await _run_in_foreground(config, adapters, processes, action)


def _absolute_path(value: str) -> Path:
    return Path(value).resolve()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asv", description="agent-supervisor 階段 1 的驗證工具")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="設定檔路徑")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="開一個任務並執行到結束")
    start.add_argument("prompt", help="給 agent 的任務描述")
    start.add_argument(
        "--cwd", type=_absolute_path, default=".", help="agent 的工作目錄（預設為目前目錄）"
    )
    start.add_argument("--title", help="任務標題（預設取描述的開頭）")
    start.add_argument("--agent", default=DEFAULT_AGENT, help="agent 種類")
    start.add_argument("--model", help="模型（預設用 agent 的預設值）")

    message = commands.add_parser("message", help="對既有任務的同一個 session 追加指令")
    message.add_argument("task_id", type=int)
    message.add_argument("text")

    commands.add_parser("stop", help="中止正在執行的 run")

    show = commands.add_parser("show", help="列出任務的 run 與事件")
    show.add_argument("task_id", type=int)
    return parser


async def _run_in_foreground(
    config: Config, adapters: Mapping[str, AgentAdapter], processes: ProcessTable, action: Action
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
        runner = Runner(conn, adapters, config, on_event=_print_event)
        try:
            run = await action(runner)
        except (
            db.NotFoundError,
            db.RunAlreadyActiveError,
            db.TaskNotRunnableError,
            BudgetExhaustedError,
            UnknownAgentError,
        ) as error:
            print(str(error), file=sys.stderr)
            return EXIT_FAILED
        print(f"task {run.task_id} run {run.id} started ({run.trigger})")
        finished = await _wait_with_stop_signal(runner, run)
        print(_format_run(finished))
        return EXIT_OK if finished.exit_status is ExitStatus.SUCCESS else EXIT_FAILED
    finally:
        await conn.close()
        lock.release()


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


async def _wait_with_stop_signal(runner: Runner, run: db.Run) -> db.Run:
    """等 run 結束；期間收到 SIGINT（Ctrl-C 或 `asv stop`）就中止它。"""
    loop = asyncio.get_running_loop()
    stopping: list[asyncio.Task[None]] = []

    def request_stop() -> None:
        if not stopping:
            stopping.append(asyncio.create_task(_stop_quietly(runner, run.task_id)))

    loop.add_signal_handler(signal.SIGINT, request_stop)
    try:
        finished = await runner.wait(run.id)
        if stopping:
            await asyncio.wait(stopping)
        return finished
    finally:
        loop.remove_signal_handler(signal.SIGINT)


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
        print(f"task {task.id}: {task.title}  agent={task.agent_type}  session={task.session_id}")
        for run in await db.list_runs(conn, task_id):
            print(_format_run(run))
        for event in await db.list_events(conn, task_id):
            print(f"  [{event.id}] run={event.run_id} {_format_event(event.type, event.payload)}")
        return EXIT_OK
    finally:
        await conn.close()


def _print_event(event_type: EventType, payload: dict[str, Any]) -> None:
    print(_format_event(event_type, payload))


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
