"""FastAPI application: REST endpoints and one SSE stream under `/api`, plus the frontend's
static files.

Board columns and status labels are always derived by `status.py`. Reads handed to the outside
are all wrapped in `db.consistent_read`, so a response never reflects someone else's
half-finished transaction.
"""

import asyncio
import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent_supervisor import db, status, worktree
from agent_supervisor.api import views
from agent_supervisor.api.errors import (
    InvalidRequestError,
    ProjectExistsError,
    install_error_handlers,
)
from agent_supervisor.api.feed import ChangeFeed, stream_text
from agent_supervisor.api.guard import LocalRequestsOnly
from agent_supervisor.config import DEFAULT_CONFIG_PATH, absolute_path
from agent_supervisor.shell import Closed, Lagged, ShellClosedError, ShellManager
from agent_supervisor.supervisor import Supervisor

TRY_AGAIN_LATER = 1013  # WebSocket close code

DEFAULT_AGENT = "claude_code"
TITLE_CHARS = 60
MAX_PAGE_SIZE = 500


class ProjectRequest(BaseModel):
    """Add a local repo. Settings not given are detected from the repo."""

    repo_path: str = Field(min_length=1)
    github_repo: str | None = None
    default_branch: str | None = None
    require_review: bool = False


class TaskRequest(BaseModel):
    """Open a task."""

    project_id: int
    prompt: str = Field(min_length=1)
    title: str | None = None
    agent_type: str = DEFAULT_AGENT
    model: str | None = None


class MessageRequest(BaseModel):
    """Send a follow-up instruction to the task's same session."""

    text: str = Field(min_length=1)


# Shown at `/` when this installation carries no built frontend (e.g. installed from a git URL).
MISSING_BOARD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>agent-supervisor</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 48px auto; line-height: 1.5">
<h1>The board is not included in this installation</h1>
<p>The daemon is running and its API is at <code>/api</code>, but this installation has no
built frontend. Installs from a git URL never include it.</p>
<p>To get the board, install a release package instead (see the README), or, when working
from a clone of the repository, run <code>npm install</code> and <code>npm run build</code> in
<code>web/</code> and restart <code>agsv serve</code>.</p>
</body></html>
"""


def create_app(
    supervisor: Supervisor,
    feed: ChangeFeed,
    *,
    is_closing: Callable[[], bool] = lambda: False,
    static_dir: Path | None = None,
    config_path: Path = DEFAULT_CONFIG_PATH,
    shells: ShellManager | None = None,
) -> FastAPI:
    """Create the application.

    `is_closing` tells whether the daemon is shutting down; SSE connections rely on it to end.
    `static_dir` is the directory holding the built frontend files; if given it is mounted at
    `/`. The API is always under `/api`. `config_path` is the file the settings were read from,
    shown on the settings page. `shells` holds the task shells; the daemon passes its own so it can
    close them on shutdown.
    """
    # The API is a separate application mounted at `/api`: when the path exists but the method
    # is wrong it answers 405 itself, instead of falling through to the static files as a 404.
    api = FastAPI(title="agent-supervisor")
    install_error_handlers(api)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app)
    app.add_middleware(LocalRequestsOnly)
    conn, runner = supervisor.conn, supervisor.runner
    shell_manager = shells if shells is not None else ShellManager()

    async def task_detail(task_id: int) -> dict[str, Any]:
        async with db.consistent_read(conn):
            card = await feed.card(task_id)
            task = await db.get_task(conn, task_id)
            runs = await db.list_runs(conn, task_id)
            tokens_in, tokens_out = await db.get_task_token_totals(conn, task_id)
        return {
            **card,
            "prompt": task.prompt,
            "model": task.model,
            "session_id": task.session_id,
            "worktree_path": task.worktree_path,
            "head_sha": task.head_sha,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "runs": [views.run_view(run) for run in runs],
        }

    @api.post("/projects", status_code=201)
    async def add_project(request: ProjectRequest) -> dict[str, Any]:
        """Add a local repo. Reject it if already added, leaving its existing settings untouched."""
        repo_path = await asyncio.to_thread(absolute_path, request.repo_path)
        try:
            await worktree.ensure_repository(repo_path)
        except worktree.GitError as error:
            raise InvalidRequestError(str(error)) from error
        existing = await db.get_project_by_path(conn, str(repo_path))
        if existing is not None:
            raise ProjectExistsError(f"{repo_path} is already project {existing.id}")
        github_repo = request.github_repo or await worktree.detect_github_repo(repo_path)
        default_branch = request.default_branch or await worktree.detect_default_branch(repo_path)
        project = await db.upsert_project(
            conn,
            str(repo_path),
            github_repo=github_repo,
            default_branch=default_branch,
            require_review=request.require_review,
        )
        return views.project_view(project)

    @api.get("/projects")
    async def list_projects() -> list[dict[str, Any]]:
        """List projects."""
        async with db.consistent_read(conn):
            return [views.project_view(project) for project in await db.list_projects(conn)]

    @api.post("/tasks", status_code=201)
    async def create_task(request: TaskRequest) -> dict[str, Any]:
        """Open a task.

        Responds once the task and its initial run exist; the worktree and agent continue in
        the background.
        """
        await db.get_project(conn, request.project_id)
        # Wrapped in shield: if the request is cancelled, "create the run" and "start running"
        # still complete together, so no run is left without an owner. The two run-creating
        # endpoints below do the same.
        run = await asyncio.shield(
            runner.start_task(
                project_id=request.project_id,
                title=request.title or request.prompt[:TITLE_CHARS],
                prompt=request.prompt,
                agent_type=request.agent_type,
                model=request.model,
            )
        )
        return await task_detail(run.task_id)

    @api.get("/tasks")
    async def list_tasks(project_id: int | None = None) -> list[dict[str, Any]]:
        """Board data: one card per task, with the derived column and status label."""
        async with db.consistent_read(conn):
            return [
                views.card_view(
                    snapshot,
                    status.board_status(snapshot, supervisor.limits),
                    reports_cost=supervisor.reports_cost(snapshot.task.agent_type),
                )
                for snapshot in await db.list_snapshots(conn, project_id)
            ]

    @api.get("/tasks/{task_id}")
    async def get_task(task_id: int) -> dict[str, Any]:
        """Task detail, including all of the task's runs."""
        return await task_detail(task_id)

    @api.post("/tasks/{task_id}/messages", status_code=201)
    async def send_message(task_id: int, request: MessageRequest) -> dict[str, Any]:
        """Send a follow-up instruction to the same session, creating a new run."""
        await asyncio.shield(supervisor.send_message(task_id, request.text))
        return await task_detail(task_id)

    @api.post("/tasks/{task_id}/stop")
    async def stop_task(task_id: int) -> dict[str, Any]:
        """Stop the task's active run, responding only once it has fully stopped."""
        await db.get_task(conn, task_id)
        await asyncio.shield(runner.stop(task_id))
        return await task_detail(task_id)

    @api.post("/tasks/{task_id}/archive")
    async def archive_task(task_id: int) -> dict[str, Any]:
        """Archive the task and remove its worktree, ending its shell first."""
        task = await db.get_task(conn, task_id)  # 404 before touching the shell
        # Leave the shell alone when the runner is going to refuse (already archived, run active).
        if task.archived_at is None and await db.get_open_run(conn, task_id) is None:
            await asyncio.shield(shell_manager.close(task_id, "archived"))
        await asyncio.shield(runner.archive(task_id))
        return await task_detail(task_id)

    @api.get("/tasks/{task_id}/diff")
    async def get_diff(task_id: int) -> dict[str, Any]:
        """Diff of the task's branch against the base branch. Empty if there is no branch yet."""
        async with db.consistent_read(conn):
            task = await db.get_task(conn, task_id)
            project = await db.get_project(conn, task.project_id)
        text = ""
        if task.branch is not None:
            text = await worktree.diff_against_base(
                Path(project.repo_path), project.default_branch, task.branch
            )
        return views.diff_view(text, base_branch=project.default_branch)

    @api.get("/tasks/{task_id}/events")
    async def list_events(
        task_id: int,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 100,
        event_type: Annotated[str | None, Query(alias="type")] = None,
    ) -> dict[str, Any]:
        """Audit log, paged in write order. A non-null `next_after` is the next page's `after`."""
        async with db.consistent_read(conn):
            await db.get_task(conn, task_id)
            events = await db.list_events_after(
                conn, after, limit=limit, task_id=task_id, event_type=event_type
            )
        return {
            "events": [views.event_view(event) for event in events],
            "next_after": events[-1].id if len(events) == limit else None,
        }

    display_names = {
        agent_type: adapter.display_name for agent_type, adapter in supervisor.adapters.items()
    }

    @api.get("/agents")
    async def list_agents() -> list[dict[str, Any]]:
        """Agent types this daemon has, for the new-task form; `usable` is as of the last check."""
        readiness = await supervisor.readiness.latest()
        return [
            {
                "agent_type": agent_type,
                "display_name": display_names[agent_type],
                "usable": readiness.agents[agent_type].usable,
                "reports_cost": supervisor.reports_cost(agent_type),
            }
            for agent_type in supervisor.adapters
        ]

    @api.get("/setup")
    async def get_setup() -> dict[str, Any]:
        """Whether each agent and gh can be used, as of the last check."""
        return views.readiness_view(await supervisor.readiness.latest(), display_names)

    @api.post("/setup/check")
    async def check_setup() -> dict[str, Any]:
        """Check every agent and gh again, e.g. after signing in from a terminal."""
        readiness = await asyncio.shield(supervisor.readiness.check())
        return views.readiness_view(readiness, display_names)

    async def shell_directory(task_id: int) -> Path | None:
        """The worktree a shell for this task runs in, or None if it cannot have one."""
        task = await db.get_task(conn, task_id)
        if task.archived_at is not None or task.worktree_path is None:
            return None
        path = Path(task.worktree_path)
        return path if await asyncio.to_thread(path.is_dir) else None

    @api.websocket("/tasks/{task_id}/shell")
    async def shell_socket(
        websocket: WebSocket, task_id: int, rows: int = 24, cols: int = 80
    ) -> None:
        """A terminal in the task's worktree: binary frames carry terminal data both ways."""
        await websocket.accept()
        try:
            cwd = await shell_directory(task_id)
        except db.NotFoundError:
            cwd = None
        if cwd is None:
            unavailable = {"type": "closed", "reason": "unavailable", "exit_code": None}
            await websocket.send_json(unavailable)
            await websocket.close()
            return
        shell = await shell_manager.open(task_id, cwd, (rows, cols))
        shell.resize(rows, cols)
        # Read the scrollback and subscribe with no await in between: nothing is lost or repeated.
        earlier = shell.scrollback
        listener = shell.subscribe()

        async def send_output() -> None:
            await websocket.send_json({"type": "opened", "shell": shell.name, "cwd": str(cwd)})
            if earlier:
                await websocket.send_bytes(earlier)
            while True:
                item = await listener.get()
                if isinstance(item, Lagged):
                    # The page fell too far behind; it reconnects and gets the scrollback instead.
                    await websocket.close(code=TRY_AGAIN_LATER)
                    return
                if isinstance(item, Closed):
                    await websocket.send_json(
                        {"type": "closed", "reason": item.reason, "exit_code": item.exit_code}
                    )
                    await websocket.close()
                    return
                await websocket.send_bytes(item)

        async def receive_input() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    with contextlib.suppress(ShellClosedError):
                        shell.write(message["bytes"])
                elif message.get("text") is not None:
                    control = json.loads(message["text"])
                    if control.get("type") == "resize":
                        shell.resize(int(control["rows"]), int(control["cols"]))

        workers = {asyncio.create_task(send_output()), asyncio.create_task(receive_input())}
        try:
            await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        finally:
            shell.unsubscribe(listener)
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    @api.post("/tasks/{task_id}/shell/restart")
    async def restart_shell(task_id: int) -> dict[str, Any]:
        """End the task's shell; the page reconnects and gets a new one."""
        await db.get_task(conn, task_id)
        return {"restarted": await asyncio.shield(shell_manager.close(task_id, "restarted"))}

    @api.get("/config")
    async def get_config() -> dict[str, Any]:
        """The limits and permission policy in effect. Read-only; edit the file and restart."""
        return views.config_view(supervisor.config, config_path)

    @api.get("/usage")
    async def get_usage(project_id: int | None = None) -> dict[str, Any]:
        """Token and cost totals. Tasks not reporting cost get null and are left out of the sum."""
        rows = []
        async with db.consistent_read(conn):
            for snapshot in await db.list_snapshots(conn, project_id):
                task = snapshot.task
                tokens_in, tokens_out = await db.get_task_token_totals(conn, task.id)
                reports_cost = supervisor.reports_cost(task.agent_type)
                rows.append(
                    {
                        "task_id": task.id,
                        "title": task.title,
                        "agent_type": task.agent_type,
                        "cost_usd": snapshot.usage.cost_usd if reports_cost else None,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "usage_is_complete": snapshot.usage.is_complete,
                    }
                )
        return {
            "cost_usd": sum(row["cost_usd"] or 0.0 for row in rows),
            "tokens_in": sum(row["tokens_in"] for row in rows),
            "tokens_out": sum(row["tokens_out"] for row in rows),
            "has_unknown_cost": any(row["cost_usd"] is None for row in rows),
            "usage_is_complete": all(row["usage_is_complete"] for row in rows),
            "tasks": rows,
        }

    @api.get("/stream")
    async def stream(
        last_event_id: Annotated[int | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        """SSE: `event` is a new audit log entry, `task` is the latest content of a card."""
        return StreamingResponse(
            stream_text(feed, last_event_id, is_closing),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.mount("/api", api)
    if static_dir is None:

        @app.get("/", response_class=HTMLResponse)
        async def missing_board() -> str:
            """Explain why there is no board instead of answering 404."""
            return MISSING_BOARD_PAGE

        return app
    # Mount last: `/` catches every other path.
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app
