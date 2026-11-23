"""The shell over WebSocket, through a real uvicorn server, the guard and `/bin/sh`."""

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus

from agent_supervisor import db
from agent_supervisor.api.app import create_app
from agent_supervisor.api.guard import websocket_problem
from agent_supervisor.config import Config
from agent_supervisor.shell import ShellManager
from tests.conftest import GitRepo
from tests.fake_adapter import result, session_started
from tests.test_api import Api, write_file
from tests.test_shell import resolved, start_sh, wait_until_gone

pytestmark = pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs /bin/sh")


class Server:
    """The app served by uvicorn on a free port, with shells that run `/bin/sh`."""

    def __init__(self, under_test: Api) -> None:
        self.shells = ShellManager(start=start_sh)
        app = create_app(under_test.supervisor, under_test.feed, shells=self.shells)
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
        )
        self._serving: asyncio.Task[None] | None = None
        self.port = 0

    async def __aenter__(self) -> "Server":
        self._serving = asyncio.create_task(self._server.serve())
        async with asyncio.timeout(10):
            while not self._server.started:  # noqa: ASYNC110  # uvicorn offers no event
                await asyncio.sleep(0.01)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.shells.close_all()
        self._server.should_exit = True
        assert self._serving is not None
        await self._serving

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def shell(self, task_id: int, origin: str | None = "same", **query: int) -> Any:
        """Connect to the task's shell; by default from the daemon's own page."""
        suffix = "".join(f"&{name}={value}" for name, value in query.items()).replace("&", "?", 1)
        url = f"ws://127.0.0.1:{self.port}/api/tasks/{task_id}/shell{suffix}"
        return connect(url, origin=self.origin if origin == "same" else origin)  # type: ignore[arg-type]

    async def post(self, path: str) -> httpx.Response:
        async with httpx.AsyncClient(base_url=self.origin) as client:
            return await client.post(path, headers={"Origin": self.origin})


@pytest.fixture
async def api(tmp_path: Path, git_repo: GitRepo) -> AsyncIterator[Api]:
    """Same as `tests.test_api.api`: the app wired to fakes, with a real git repo."""
    conn = await db.connect(tmp_path / "data" / "test.db")
    await db.init_schema(conn)
    made = Api(conn, Config(data_dir=tmp_path / "data"), git_repo)
    yield made
    await made.supervisor.runner.shutdown()
    await made.client.aclose()
    await conn.close()


@pytest.fixture
async def server(api: Api) -> AsyncIterator[Server]:
    async with Server(api) as made:
        yield made


async def finished_task(api: Api) -> int:
    task = await api.task([session_started("s1"), write_file("a.txt"), result()])
    await api.finished(task["id"])
    return int(task["id"])


async def read_until(socket: ClientConnection, pattern: str, timeout: float = 10.0) -> str:  # noqa: ASYNC109
    """Terminal output until `pattern` matches; fails on a control message."""
    seen = ""
    async with asyncio.timeout(timeout):
        while not re.search(pattern, seen):
            message = await socket.recv()
            if isinstance(message, str) and json.loads(message)["type"] == "opened":
                continue
            if isinstance(message, str):
                raise AssertionError(f"got {message} before {pattern!r}; output so far {seen!r}")
            seen += message.decode(errors="replace")
    return seen


async def next_control(socket: ClientConnection, timeout: float = 10.0) -> dict[str, Any]:  # noqa: ASYNC109
    """Skip terminal output and the `opened` message until the next JSON control message."""
    async with asyncio.timeout(timeout):
        while True:
            message = await socket.recv()
            if isinstance(message, str) and json.loads(message)["type"] != "opened":
                return json.loads(message)  # type: ignore[no-any-return]


async def test_typing_runs_commands_in_the_task_worktree(api: Api, server: Server) -> None:
    task_id = await finished_task(api)
    worktree = Path((await api.client.get(f"/api/tasks/{task_id}")).json()["worktree_path"])

    async with server.shell(task_id) as socket:
        opened = json.loads(await socket.recv())
        assert opened == {"type": "opened", "shell": "sh", "cwd": str(worktree)}
        await socket.send(b"echo ws-$((4+4)); pwd\n")
        seen = await read_until(socket, r"ws-8[\s\S]*" + re.escape(resolved(worktree).name))

    assert str(resolved(worktree)) in seen.replace("\r", "")


async def test_the_page_size_and_later_resizes_reach_the_terminal(api: Api, server: Server) -> None:
    task_id = await finished_task(api)

    async with server.shell(task_id, rows=30, cols=90) as socket:
        await socket.send(b"stty size\n")
        await read_until(socket, r"30 90")
        await socket.send(json.dumps({"type": "resize", "rows": 33, "cols": 77}))
        await socket.send(b"stty size\n")
        await read_until(socket, r"33 77")


async def test_reconnecting_keeps_the_shell_and_shows_recent_output(
    api: Api, server: Server
) -> None:
    task_id = await finished_task(api)
    async with server.shell(task_id) as socket:
        await socket.send(b"echo PID:$$\n")
        pid = re.findall(r"PID:(\d+)", await read_until(socket, r"PID:\d+"))[-1]

    async with server.shell(task_id) as socket:
        earlier = await read_until(socket, r"PID:\d+")
        await socket.send(b"echo PID:$$\n")
        again = re.findall(r"PID:(\d+)", await read_until(socket, r"PID:\d+"))

    assert f"PID:{pid}" in earlier
    assert again[-1] == pid


@pytest.mark.parametrize(
    "origin",
    [
        None,  # no Origin: not a page in a browser
        "https://evil.example",
        "http://127.0.0.1:5173",  # a local page, but not the daemon's own
        "http://localhost:{port}",  # the same port under another name is another origin
    ],
)
async def test_connections_from_anywhere_but_the_board_are_refused(
    api: Api,
    server: Server,
    origin: str | None,
) -> None:
    task_id = await finished_task(api)
    if origin is not None:
        origin = origin.format(port=server.port)

    with pytest.raises(InvalidStatus) as refused:
        async with server.shell(task_id, origin=origin):
            pass

    assert refused.value.response.status_code == 403
    assert server.shells.get(task_id) is None


def test_websocket_rule_also_checks_the_host() -> None:
    assert websocket_problem("evil.example:8765", "http://evil.example:8765") is not None
    assert websocket_problem(None, "http://127.0.0.1:8765") is not None
    assert websocket_problem("127.0.0.1:8765", "http://127.0.0.1:8765") is None
    assert websocket_problem("localhost:8765", "http://LOCALHOST:8765") is None


async def test_archiving_ends_the_shell_and_what_runs_in_it(api: Api, server: Server) -> None:
    task_id = await finished_task(api)
    async with server.shell(task_id) as socket:
        await socket.send(b"echo PID:$$; sh -c 'echo CHILD:$$; exec sleep 300'\n")
        seen = await read_until(socket, r"CHILD:\d+")
        shell_pid = int(re.findall(r"PID:(\d+)", seen)[-1])
        child_pid = int(re.findall(r"CHILD:(\d+)", seen)[-1])

        response = await server.post(f"/api/tasks/{task_id}/archive")

        assert response.status_code == 200, response.text
        closed = await next_control(socket)
        assert closed == {"type": "closed", "reason": "archived", "exit_code": None}
    await wait_until_gone(child_pid)
    await wait_until_gone(shell_pid)

    async with server.shell(task_id) as socket:
        assert await next_control(socket) == {
            "type": "closed",
            "reason": "unavailable",
            "exit_code": None,
        }


async def test_restart_ends_the_shell_and_the_next_connection_gets_a_new_one(
    api: Api,
    server: Server,
) -> None:
    task_id = await finished_task(api)
    async with server.shell(task_id) as socket:
        await socket.send(b"echo PID:$$\n")
        first = int(re.findall(r"PID:(\d+)", await read_until(socket, r"PID:\d+"))[-1])

        response = await server.post(f"/api/tasks/{task_id}/shell/restart")

        assert response.json() == {"restarted": True}
        assert (await next_control(socket))["reason"] == "restarted"
    await wait_until_gone(first)

    async with server.shell(task_id) as socket:
        await socket.send(b"echo PID:$$\n")
        second = int(re.findall(r"PID:(\d+)", await read_until(socket, r"PID:\d+"))[-1])
    assert second != first
    assert os.getpid() not in {first, second}


async def test_a_task_without_a_worktree_has_no_shell(api: Api, server: Server) -> None:
    async with server.shell(999) as socket:
        assert (await next_control(socket))["reason"] == "unavailable"


async def test_an_archive_that_is_refused_leaves_the_shell_running(
    api: Api, server: Server
) -> None:
    release = asyncio.Event()
    task = await api.task([session_started("s1"), release, result()])
    task_id = int(task["id"])
    await api.agent_started(1)
    try:
        async with server.shell(task_id) as socket:
            await read_until(socket, r".")  # the shell is up
            response = await server.post(f"/api/tasks/{task_id}/archive")

            assert response.status_code == 409, response.text
            await socket.send(b"echo still-here-$((7+7))\n")
            await read_until(socket, r"still-here-14")
    finally:
        release.set()
        await api.finished(task_id)
