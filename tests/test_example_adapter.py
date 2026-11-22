"""The example adapter in examples/example-adapter: one turn, resuming, stopping, a whole task.

It runs its real fake agent as a child process, so these also check what the adapter guide asks of
every adapter: the marker comes first, and no process is left behind when a turn ends early.
"""

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest

from agent_supervisor import db
from agent_supervisor.adapters.base import (
    AgentEvent,
    PermissionPolicy,
    RunLimits,
    SandboxLevel,
    TurnResult,
)
from agent_supervisor.config import Config
from agent_supervisor.enums import EventType, ExitStatus
from agent_supervisor.recovery import SystemProcessTable
from agent_supervisor.supervisor import build_supervisor
from tests.conftest import GitRepo
from tests.test_stages import FakeGitHub

EXAMPLE_SOURCE = Path(__file__).parents[1] / "examples" / "example-adapter" / "src"
NO_LIMITS = RunLimits(max_turns=None, max_budget_usd=None)


@pytest.fixture
def example(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The adapter class, importable here and in the agent process it starts."""
    monkeypatch.syspath_prepend(str(EXAMPLE_SOURCE))
    monkeypatch.setenv("PYTHONPATH", str(EXAMPLE_SOURCE))
    monkeypatch.setenv("EXAMPLE_AGENT_STEP_SECONDS", "0")
    from example_adapter import ExampleAdapter

    yield ExampleAdapter


async def run_turn(
    adapter: Any,
    cwd: Path,
    *,
    session_id: str | None = None,
    policy: PermissionPolicy | None = None,
) -> tuple[list[AgentEvent], TurnResult]:
    events: list[AgentEvent] = []
    result: TurnResult | None = None
    stream: AsyncIterator[AgentEvent | TurnResult] = adapter.run_turn(
        prompt="add a greeting\nmore details",
        cwd=cwd,
        session_id=session_id,
        model=None,
        limits=NO_LIMITS,
        policy=policy or PermissionPolicy(),
        previous_usage=None,
    )
    async with aclosing(stream):
        async for item in stream:
            if isinstance(item, TurnResult):
                result = item
            else:
                events.append(item)
    assert result is not None
    return events, result


async def test_one_turn_emits_the_marker_first_and_reports_tokens(
    example: Any, tmp_path: Path
) -> None:
    events, result = await run_turn(example(), tmp_path)

    first, second = events[0], events[1]
    assert first.type is EventType.SESSION_STARTED
    assert first.payload["session_id"] is None
    assert first.payload["process_marker"].startswith("example-agent-")
    assert second.type is EventType.SESSION_STARTED
    assert second.payload["session_id"] == result.session_id
    assert [event.type for event in events[2:]] == [
        EventType.AGENT_MESSAGE,
        EventType.COMMAND,
        EventType.COMMAND,
        EventType.TOOL_CALL,
        EventType.TOOL_CALL,
        EventType.AGENT_MESSAGE,
    ]
    assert (result.exit_status, result.tokens_out, result.cost_usd) == (
        ExitStatus.SUCCESS,
        40,
        None,
    )
    assert (tmp_path / "EXAMPLE_AGENT_NOTES.md").read_text() == "- turn 1: add a greeting\n"


async def test_resuming_continues_the_same_session(example: Any, tmp_path: Path) -> None:
    _, first = await run_turn(example(), tmp_path)

    events, second = await run_turn(example(), tmp_path, session_id=first.session_id)

    assert second.session_id == first.session_id
    assert "Turn 2 of this session" in events[2].payload["text"]


async def test_read_only_policy_writes_nothing(example: Any, tmp_path: Path) -> None:
    policy = PermissionPolicy(sandbox_level=SandboxLevel.READ_ONLY)

    events, result = await run_turn(example(), tmp_path, policy=policy)

    assert result.exit_status is ExitStatus.SUCCESS
    assert not (tmp_path / "EXAMPLE_AGENT_NOTES.md").exists()
    assert EventType.TOOL_CALL not in [event.type for event in events]


async def test_closing_the_stream_early_leaves_no_process_behind(
    example: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXAMPLE_AGENT_STEP_SECONDS", "30")
    stream = example(stop_grace_seconds=5.0).run_turn(
        prompt="take your time",
        cwd=tmp_path,
        session_id=None,
        model=None,
        limits=NO_LIMITS,
        policy=PermissionPolicy(),
        previous_usage=None,
    )
    async with aclosing(stream):
        marker = (await anext(stream)).payload["process_marker"]
        await anext(stream)  # the agent is running: it has reported its session

    # The same lookup the daemon uses after a restart to find leftover processes.
    assert await asyncio.to_thread(SystemProcessTable().find, marker) == []


async def test_stopping_also_kills_children_the_agent_leaves_behind(example: Any) -> None:
    from example_adapter.adapter import _stop

    # An agent that exits at once on SIGINT but leaves a child running in its process group. The
    # child must not hold the agent's stdout: waiting for the agent also waits for that pipe.
    script = (
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(130))\n"
        "subprocess.Popen(['sleep', '30'], stdout=subprocess.DEVNULL)\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE, start_new_session=True
    )
    assert process.stdout is not None
    await process.stdout.readline()

    await _stop(process, 5.0)

    left = await asyncio.to_thread(
        subprocess.run, ["pgrep", "-g", str(process.pid)], capture_output=True, text=True
    )
    assert left.stdout == ""


async def test_check_needs_no_account(example: Any) -> None:
    status = await example().check()

    assert status.usable
    assert status.auth_label == "nothing; no account needed"


async def test_a_whole_task_runs_without_any_agent_account(
    example: Any, tmp_path: Path, git_repo: GitRepo
) -> None:
    conn = await db.connect(tmp_path / "test.db")
    await db.init_schema(conn)
    github = FakeGitHub()
    supervisor = build_supervisor(
        conn,
        Config(data_dir=tmp_path / "data"),
        {"example": example()},
        github,  # type: ignore[arg-type]
    )
    project = await db.upsert_project(conn, str(git_repo.path), github_repo="owner/demo")

    run = await supervisor.runner.start_task(
        project_id=project.id,
        title="Greeting",
        prompt="add a greeting",
        agent_type="example",
        model=None,
    )
    finished = await supervisor.runner.wait(run.id)

    task = await db.get_task(conn, run.task_id)
    assert finished.exit_status == ExitStatus.SUCCESS
    assert task.pr_number is not None
    assert task.branch is not None
    notes = git_repo.git("show", f"{task.branch}:EXAMPLE_AGENT_NOTES.md", cwd=git_repo.origin)
    assert notes == "- turn 1: add a greeting"
    await supervisor.runner.shutdown()
    await conn.close()
