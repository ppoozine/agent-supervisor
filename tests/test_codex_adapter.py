"""Codex adapter tests. A fake `codex` executable emits prerecorded events; no real agent runs."""

import asyncio
import json
import os
import sys
from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest

from agent_supervisor.adapters.base import (
    AgentEvent,
    PermissionPolicy,
    RunLimits,
    SandboxLevel,
    TurnResult,
)
from agent_supervisor.adapters.codex import CodexAdapter
from agent_supervisor.enums import EventType, ExitStatus

FAKE_CODEX = Path(__file__).parent / "fake_codex.py"
FIXTURES = Path(__file__).parent / "fixtures" / "codex"
NO_LIMITS = RunLimits(max_turns=None, max_budget_usd=None)
THREAD = '{"type": "thread.started", "thread_id": "thread-1"}'


def usage_line(input_tokens: int, output_tokens: int) -> str:
    usage = {"input_tokens": input_tokens, "cached_input_tokens": 1, "output_tokens": output_tokens}
    return json.dumps({"type": "turn.completed", "usage": usage})


class Codex:
    """An adapter wired to the fake executable, plus this run's script and record."""

    def __init__(self, tmp_path: Path, **script: Any) -> None:
        self.tmp_path = tmp_path
        self.record_path = tmp_path / "record.json"
        self.pids_path = tmp_path / "pids.json"
        self.scratch_dir = tmp_path / "scratch"
        self.scratch_dir.mkdir()
        script_path = tmp_path / "script.json"
        script_path.write_text(
            json.dumps({"record": str(self.record_path), "pids": str(self.pids_path), **script})
        )
        self.adapter = CodexAdapter(
            executable=[sys.executable, str(FAKE_CODEX), str(script_path)],
            scratch_dir=self.scratch_dir,
            interrupt_grace_seconds=0.5,
        )

    def stream(self, **overrides: Any) -> Any:
        arguments: dict[str, Any] = {
            "prompt": "do it",
            "cwd": self.tmp_path,
            "session_id": None,
            "model": None,
            "limits": NO_LIMITS,
            "policy": PermissionPolicy(),
            "previous_usage": None,
        }
        return self.adapter.run_turn(**{**arguments, **overrides})

    async def run(self, **overrides: Any) -> tuple[list[AgentEvent], TurnResult]:
        async with asyncio.timeout(10.0):
            items = [item async for item in self.stream(**overrides)]
        result = items[-1]
        assert isinstance(result, TurnResult)
        return [item for item in items[:-1] if isinstance(item, AgentEvent)], result

    def recorded(self) -> dict[str, Any]:
        return json.loads(self.record_path.read_text())  # type: ignore[no-any-return]


def fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_capabilities_say_what_codex_cannot_do() -> None:
    capabilities = CodexAdapter.capabilities

    assert capabilities.can_resume_session and capabilities.can_restrict_network
    assert not capabilities.can_limit_turns
    assert not capabilities.can_limit_budget
    assert not capabilities.can_gate_tool_calls
    assert not capabilities.reports_cost


async def test_recorded_command_run_becomes_typed_events(tmp_path: Path) -> None:
    codex = Codex(tmp_path, lines=fixture_lines("commands.jsonl"))

    events, result = await codex.run()

    thread_id = "01a1ef8d-5260-7523-ac7b-614118b9989e"
    assert [event.type for event in events[:2]] == [EventType.SESSION_STARTED] * 2
    assert events[0].payload["session_id"] is None
    assert events[1].payload["session_id"] == thread_id
    commands = [event for event in events if event.type is EventType.COMMAND]
    assert [event.payload["phase"] for event in commands] == ["started", "finished"] * 4
    curl = commands[5].payload
    assert curl["command"] == "/bin/zsh -lc 'curl -sI https://example.com | head -1'"
    assert (curl["exit_code"], curl["is_error"]) == (0, False)
    assert curl["output"].startswith("HTTP/2 200")
    messages = [event.payload for event in events if event.type is EventType.AGENT_MESSAGE]
    assert [message["kind"] for message in messages] == ["message", "message"]
    assert result.exit_status is ExitStatus.SUCCESS
    assert (result.session_id, result.cost_usd) == (thread_id, None)
    assert (result.tokens_in, result.tokens_out) == (114720, 385)
    assert result.usage_raw is not None
    assert result.usage_raw["session_id"] == thread_id
    assert result.usage_raw["usage"]["cached_input_tokens"] == 102656


async def test_recorded_file_change_and_other_items_keep_their_raw_content(tmp_path: Path) -> None:
    codex = Codex(tmp_path, lines=fixture_lines("file_change.jsonl"))

    events, _ = await codex.run()

    tool_calls = [event.payload for event in events if event.type is EventType.TOOL_CALL]
    assert [(call["tool"], call["phase"]) for call in tool_calls] == [
        ("mcp_tool_call", "started"),
        ("mcp_tool_call", "finished"),
        ("file_change", "started"),
        ("file_change", "finished"),
    ]
    finished = tool_calls[3]
    assert finished["is_error"] is False
    assert finished["item"]["changes"] == [
        {"path": "/work/task-1/calc.py", "kind": "update"},
        {"path": "/work/task-1/notes.md", "kind": "add"},
    ]


async def test_new_session_passes_the_policy_and_sends_the_prompt_on_stdin(tmp_path: Path) -> None:
    codex = Codex(tmp_path, lines=[THREAD, usage_line(10, 2)])
    policy = PermissionPolicy(sandbox_level=SandboxLevel.READ_ONLY, allow_network=False)

    await codex.run(prompt="-- starts with a dash", policy=policy, model="some-model")

    recorded = codex.recorded()
    arguments = recorded["arguments"]
    assert arguments[:2] == ["exec", "--json"]
    assert arguments[arguments.index("-s") + 1] == "read-only"
    assert arguments[arguments.index("-C") + 1] == str(tmp_path)
    assert "sandbox_workspace_write.network_access=false" in arguments
    assert arguments[arguments.index("-m") + 1] == "some-model"
    assert arguments[-1] == "-"
    assert recorded["stdin"] == "-- starts with a dash"


async def test_resume_names_the_session_and_repeats_the_policy(tmp_path: Path) -> None:
    codex = Codex(tmp_path, lines=[THREAD, usage_line(10, 2)])

    events, _ = await codex.run(session_id="thread-1")

    arguments = codex.recorded()["arguments"]
    assert arguments[:4] == ["exec", "resume", "thread-1", "--json"]
    assert 'sandbox_mode="workspace-write"' in arguments
    assert "sandbox_workspace_write.network_access=true" in arguments
    assert "-s" not in arguments and "-C" not in arguments
    # The session id does not change on resume, so it is not announced again.
    assert [event.payload["session_id"] for event in events] == ["thread-1"]


async def test_process_marker_is_on_the_command_line_and_its_file_is_cleaned_up(
    tmp_path: Path,
) -> None:
    codex = Codex(tmp_path, lines=[THREAD, usage_line(10, 2)])

    events, _ = await codex.run()

    marker = events[0].payload["process_marker"]
    arguments = codex.recorded()["arguments"]
    output_path = Path(arguments[arguments.index("-o") + 1])
    assert marker in output_path.name
    assert output_path.parent == codex.scratch_dir
    assert list(codex.scratch_dir.iterdir()) == []


async def test_usage_is_converted_from_session_totals_to_this_turn(tmp_path: Path) -> None:
    codex = Codex(tmp_path, lines=[THREAD, usage_line(250, 40)])
    previous = {"session_id": "thread-1", "usage": {"input_tokens": 100, "output_tokens": 15}}

    _, result = await codex.run(session_id="thread-1", previous_usage=previous)

    assert (result.tokens_in, result.tokens_out) == (150, 25)
    assert result.usage_raw == {
        "session_id": "thread-1",
        "usage": {"input_tokens": 250, "cached_input_tokens": 1, "output_tokens": 40},
    }


async def test_usage_of_another_session_is_not_subtracted(tmp_path: Path) -> None:
    codex = Codex(tmp_path, lines=[THREAD, usage_line(250, 40)])
    previous = {"session_id": "another", "usage": {"input_tokens": 100, "output_tokens": 15}}

    _, result = await codex.run(previous_usage=previous)

    assert (result.tokens_in, result.tokens_out) == (250, 40)


async def test_failed_turn_is_an_error_with_the_reported_message(tmp_path: Path) -> None:
    lines = [
        THREAD,
        '{"type": "error", "message": "model is not supported"}',
        '{"type": "turn.failed", "error": {"message": "model is not supported"}}',
    ]
    codex = Codex(tmp_path, lines=lines, exit_code=1)

    _, result = await codex.run()

    assert result.exit_status is ExitStatus.ERROR
    assert result.detail == "model is not supported"
    assert (result.tokens_in, result.tokens_out, result.usage_raw) == (None, None, None)
    assert result.session_id == "thread-1"


async def test_error_item_is_recorded_as_a_failed_tool_call(tmp_path: Path) -> None:
    """Observed in practice: with a nonexistent model, Codex first sends an item of type error."""
    item = {"id": "item_0", "type": "error", "message": "Model metadata not found."}
    lines = [THREAD, json.dumps({"type": "item.completed", "item": item}), usage_line(1, 1)]
    codex = Codex(tmp_path, lines=lines)

    events, _ = await codex.run()

    recorded = next(event.payload for event in events if event.type is EventType.TOOL_CALL)
    assert (recorded["tool"], recorded["is_error"]) == ("error", True)
    assert recorded["item"]["message"] == "Model metadata not found."


async def test_exit_without_any_event_reports_the_error_output(tmp_path: Path) -> None:
    codex = Codex(tmp_path, lines=[], stderr="not logged in\n", exit_code=2)

    events, result = await codex.run()

    assert result.exit_status is ExitStatus.ERROR
    assert result.detail == "codex exited with code 2: not logged in"
    assert result.session_id is None
    assert [event.payload["session_id"] for event in events] == [None]


async def test_lines_that_are_not_events_and_very_long_lines_are_handled(tmp_path: Path) -> None:
    long_output = "x" * 200_000
    item = {"id": "i", "type": "command_execution", "command": "c", "exit_code": 1}
    long_line = json.dumps(
        {"type": "item.completed", "item": {**item, "aggregated_output": long_output}}
    )
    codex = Codex(
        tmp_path, lines=["WARNING: not json", "[1, 2]", THREAD, long_line, usage_line(1, 1)]
    )

    events, result = await codex.run()

    command = next(event.payload for event in events if event.type is EventType.COMMAND)
    assert command["is_error"] is True
    assert len(command["output"]) < 21_000
    assert command["output"].endswith("[truncated]")
    assert result.exit_status is ExitStatus.SUCCESS


async def run_until_hanging(codex: Codex) -> list[int]:
    """Close the stream once the fake Codex starts running a command; return the child's PID."""
    async with asyncio.timeout(10.0):
        async with aclosing(codex.stream()) as stream:
            async for item in stream:
                if isinstance(item, AgentEvent) and item.type is EventType.COMMAND:
                    break
            child_pids: list[int] = json.loads(codex.pids_path.read_text())
            assert all(is_alive(pid) for pid in child_pids)
    return child_pids


async def test_closing_the_stream_interrupts_codex_and_its_children(tmp_path: Path) -> None:
    codex = Codex(tmp_path, lines=[THREAD], hang=True)

    child_pids = await run_until_hanging(codex)

    assert not any(is_alive(pid) for pid in child_pids)
    assert list(codex.scratch_dir.iterdir()) == []


async def test_codex_that_ignores_the_interrupt_is_killed_with_its_children(
    tmp_path: Path,
) -> None:
    codex = Codex(tmp_path, lines=[THREAD], hang=True, ignore_interrupt=True)

    child_pids = await run_until_hanging(codex)

    async with asyncio.timeout(5.0):
        while any(is_alive(pid) for pid in child_pids):  # noqa: ASYNC110  # wait for the OS to reap the processes
            await asyncio.sleep(0.01)


def test_missing_executable_is_reported(tmp_path: Path) -> None:
    adapter = CodexAdapter(executable=[str(tmp_path / "no-such-codex")], scratch_dir=tmp_path)

    async def run() -> None:
        async for _ in adapter.run_turn(
            prompt="x",
            cwd=tmp_path,
            session_id=None,
            model=None,
            limits=NO_LIMITS,
            policy=PermissionPolicy(),
            previous_usage=None,
        ):
            pass

    with pytest.raises(FileNotFoundError):
        asyncio.run(run())
