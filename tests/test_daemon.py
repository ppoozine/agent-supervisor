"""Daemon tests: a real HTTP server (localhost only) is started; the agent and GitHub are fakes."""

import asyncio
import os
import signal
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_supervisor import daemon, db
from agent_supervisor.config import Config
from agent_supervisor.enums import CiState, ExitStatus
from agent_supervisor.github import FailedCheck
from agent_supervisor.recovery import InstanceLock
from tests.conftest import GitRepo
from tests.fake_adapter import FakeAdapter, result, session_started
from tests.test_api import write_file
from tests.test_recovery import FakeProcessTable
from tests.test_stages import FakeGitHub, pr_status


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Daemon:
    """A daemon running inside the test, plus a client connected to it."""

    def __init__(self, tmp_path: Path, repo: GitRepo) -> None:
        self.config = Config(
            data_dir=tmp_path / "data", poll_interval_seconds=0.05, api_port=free_port()
        )
        self.repo = repo
        self.adapter = FakeAdapter([])
        self.github = FakeGitHub()
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.config.api_port}", trust_env=False, timeout=10.0
        )
        self.serving: asyncio.Task[int] | None = None

    def serve(self) -> asyncio.Task[int]:
        return asyncio.create_task(
            daemon.serve(
                self.config,
                {"fake": self.adapter},
                FakeProcessTable({}),
                self.github,  # type: ignore[arg-type]
            )
        )

    async def start(self) -> None:
        self.serving = self.serve()
        async with asyncio.timeout(10.0):
            while True:
                try:
                    await self.client.get("/api/projects")
                    return
                except httpx.TransportError:
                    await asyncio.sleep(0.02)

    async def task(self, script: list[Any]) -> dict[str, Any]:
        self.adapter.scripts.append(script)
        project = await self.client.post(
            "/api/projects", json={"repo_path": str(self.repo.path), "github_repo": "owner/demo"}
        )
        response = await self.client.post(
            "/api/tasks",
            json={"project_id": project.json()["id"], "prompt": "do it", "agent_type": "fake"},
        )
        assert response.status_code == 201, response.text
        return response.json()  # type: ignore[no-any-return]

    async def card(self, task_id: int) -> dict[str, Any]:
        return (await self.client.get(f"/api/tasks/{task_id}")).json()  # type: ignore[no-any-return]

    async def until(self, task_id: int, **expected: Any) -> dict[str, Any]:
        """Wait until some of the card's fields have the given values."""
        async with asyncio.timeout(10.0):
            while True:
                card = await self.card(task_id)
                if all(card[key] == value for key, value in expected.items()):
                    return card
                await asyncio.sleep(0.02)

    async def quit(self, signal_number: signal.Signals = signal.SIGINT) -> int:
        assert self.serving is not None
        os.kill(os.getpid(), signal_number)
        async with asyncio.timeout(10.0):
            return await self.serving


@pytest.fixture
async def asv(tmp_path: Path, git_repo: GitRepo) -> AsyncIterator[Daemon]:
    made = Daemon(tmp_path, git_repo)
    yield made
    if made.serving is not None and not made.serving.done():
        made.serving.cancel()
        await asyncio.wait({made.serving})
    await made.client.aclose()


async def test_task_opened_over_http_is_fixed_in_the_background_until_ci_passes(
    asv: Daemon,
) -> None:
    await asv.start()
    created = await asv.task([session_started(), write_file("a.txt"), result()])
    first = await asv.until(created["id"], label="CI running")
    asv.github.job_logs[11] = "pytest\tRun tests\t2026-01-01T00:00:00.0Z assert broken\n"
    asv.adapter.scripts.append([write_file("b.txt"), result()])
    asv.github.statuses[41] = pr_status(
        first["head_sha"], CiState.FAILED, failed_checks=(FailedCheck("pytest", 11),)
    )
    fixed = await asv.until(created["id"], ci_fix_attempts=1, has_open_run=False)
    asv.github.statuses[41] = pr_status(fixed["head_sha"], CiState.PASSED)

    ready = await asv.until(created["id"], column="Ready to merge")

    assert [run["trigger"] for run in ready["runs"]] == ["initial", "ci_failure"]
    assert fixed["head_sha"] != first["head_sha"]
    assert await asv.quit() == 0


async def test_interrupt_ends_the_daemon_even_with_a_stream_open(asv: Daemon) -> None:
    await asv.start()
    created = await asv.task([session_started(), asyncio.Event(), result()])
    lines: list[str] = []

    async with asyncio.timeout(15.0):
        async with asv.client.stream("GET", "/api/stream", timeout=None) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            reading = response.aiter_lines()
            async for line in reading:
                lines.append(line)
                if line.startswith("event: event"):
                    break
            # Ctrl-C arrives while a connection is open and the agent is still running.
            code = await asv.quit()

    assert code == 0
    assert lines[0] == ": connected"
    conn = await db.connect(asv.config.db_path)
    runs = await db.list_runs(conn, created["id"])
    await conn.close()
    assert [run.exit_status for run in runs] == [ExitStatus.INTERRUPTED]


async def test_terminate_signal_ends_the_daemon_like_an_interrupt(asv: Daemon) -> None:
    # Service managers (e.g. systemd) ask to exit with SIGTERM; the outcome must match Ctrl-C.
    await asv.start()
    created = await asv.task([session_started(), asyncio.Event(), result()])
    await asv.until(created["id"], has_open_run=True)

    code = await asv.quit(signal.SIGTERM)

    assert code == 0
    conn = await db.connect(asv.config.db_path)
    runs = await db.list_runs(conn, created["id"])
    await conn.close()
    assert [run.exit_status for run in runs] == [ExitStatus.INTERRUPTED]


async def test_stop_signal_stops_the_runs_but_the_daemon_keeps_serving(asv: Daemon) -> None:
    await asv.start()
    created = await asv.task([session_started(), asyncio.Event(), result()])
    await asv.until(created["id"], has_open_run=True)

    os.kill(os.getpid(), signal.SIGUSR1)
    stopped = await asv.until(created["id"], has_open_run=False)

    assert stopped["label"] == "Stopped"
    assert (await asv.client.get("/api/projects")).status_code == 200
    assert await asv.quit() == 0


async def test_second_daemon_is_refused(asv: Daemon, capsys: pytest.CaptureFixture[str]) -> None:
    lock = InstanceLock.acquire(asv.config.lock_path)

    async with asyncio.timeout(10.0):
        code = await asv.serve()

    lock.release()
    assert code == 2
    assert "already running" in capsys.readouterr().err


async def test_failing_polling_round_does_not_stop_later_rounds(asv: Daemon) -> None:
    await asv.start()
    created = await asv.task([session_started(), write_file("a.txt"), result()])
    card = await asv.until(created["id"], label="CI running")
    failures = 0

    async def fail_twice() -> None:
        nonlocal failures
        if failures < 2:
            failures += 1
            raise RuntimeError("unexpected failure inside a polling round")

    asv.github.on_status_query = fail_twice
    asv.github.statuses[41] = pr_status(card["head_sha"], CiState.PASSED)

    ready = await asv.until(created["id"], column="Ready to merge")

    assert failures == 2
    assert ready["ci_state"] == "passed"
    assert await asv.quit() == 0
