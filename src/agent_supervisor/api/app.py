"""FastAPI application: REST endpoints and one SSE stream under `/api`, plus the frontend's
static files.

Board columns and status labels are always derived by `status.py`. Reads handed to the outside
are all wrapped in `db.consistent_read`, so a response never reflects someone else's
half-finished transaction.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query
from fastapi.responses import StreamingResponse
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
from agent_supervisor.config import absolute_path
from agent_supervisor.supervisor import Supervisor

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


def create_app(
    supervisor: Supervisor,
    feed: ChangeFeed,
    *,
    is_closing: Callable[[], bool] = lambda: False,
    static_dir: Path | None = None,
) -> FastAPI:
    """Create the application.

    `is_closing` tells whether the daemon is shutting down; SSE connections rely on it to end.
    `static_dir` is the directory holding the built frontend files; if given it is mounted at
    `/`. The API is always under `/api`.
    """
    # The API is a separate application mounted at `/api`: when the path exists but the method
    # is wrong it answers 405 itself, instead of falling through to the static files as a 404.
    api = FastAPI(title="agent-supervisor")
    install_error_handlers(api)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app)
    app.add_middleware(LocalRequestsOnly)
    conn, runner = supervisor.conn, supervisor.runner

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
        """Archive the task and remove its worktree."""
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

    @api.get("/agents")
    async def list_agents() -> list[dict[str, Any]]:
        """Agent types this daemon can use, for the new-task form to choose from."""
        return [
            {"agent_type": agent_type, "reports_cost": supervisor.reports_cost(agent_type)}
            for agent_type in supervisor.adapters
        ]

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
    if static_dir is not None:
        # Mount last: `/` catches every other path.
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app
