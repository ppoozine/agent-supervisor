"""FastAPI 應用程式：`/api` 底下的 REST 端點與一條 SSE，以及前端的靜態檔案。

看板欄位與狀態標籤一律由 `status.py` 推導。要交給外部的讀取都包在
`db.consistent_read` 裡，才不會回應別人做到一半的交易。
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
    """加入一個本機 repo。沒給的設定從 repo 偵測。"""

    repo_path: str = Field(min_length=1)
    github_repo: str | None = None
    default_branch: str | None = None
    require_review: bool = False


class TaskRequest(BaseModel):
    """開一個任務。"""

    project_id: int
    prompt: str = Field(min_length=1)
    title: str | None = None
    agent_type: str = DEFAULT_AGENT
    model: str | None = None


class MessageRequest(BaseModel):
    """對任務的同一個 session 追加指令。"""

    text: str = Field(min_length=1)


def create_app(
    supervisor: Supervisor,
    feed: ChangeFeed,
    *,
    is_closing: Callable[[], bool] = lambda: False,
    static_dir: Path | None = None,
) -> FastAPI:
    """建立應用程式。

    `is_closing` 回答 daemon 是否正要關閉，SSE 連線靠它結束。`static_dir` 是前端
    build 出來的檔案所在的目錄，有給就掛在 `/`；API 一律在 `/api` 底下。
    """
    # API 是掛在 `/api` 的獨立應用程式：路徑存在但方法不對時由它自己回 405，
    # 不會落到後面的靜態檔案變成 404。
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
        """加入本機 repo。已經加入過就拒絕，不動它原本的設定。"""
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
        """專案列表。"""
        async with db.consistent_read(conn):
            return [views.project_view(project) for project in await db.list_projects(conn)]

    @api.post("/tasks", status_code=201)
    async def create_task(request: TaskRequest) -> dict[str, Any]:
        """開任務：建立任務與 initial run 後就回應，worktree 與 agent 在背景進行。"""
        await db.get_project(conn, request.project_id)
        # 用 shield 包住：請求被取消時，「建立 run」與「開始執行」仍然會一起做完，
        # 不會留下一個沒有人負責的 run。下面兩個建立 run 的端點也一樣。
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
        """看板資料：每個任務一張卡片，含推導出的欄位與狀態標籤。"""
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
        """任務詳情，含這個任務所有的 run。"""
        return await task_detail(task_id)

    @api.post("/tasks/{task_id}/messages", status_code=201)
    async def send_message(task_id: int, request: MessageRequest) -> dict[str, Any]:
        """對同一個 session 追加指令，建立一個新的 run。"""
        await asyncio.shield(runner.send_message(task_id, request.text))
        return await task_detail(task_id)

    @api.post("/tasks/{task_id}/stop")
    async def stop_task(task_id: int) -> dict[str, Any]:
        """中止這個任務正在跑的 run，等它完全停下才回應。"""
        await db.get_task(conn, task_id)
        await asyncio.shield(runner.stop(task_id))
        return await task_detail(task_id)

    @api.post("/tasks/{task_id}/archive")
    async def archive_task(task_id: int) -> dict[str, Any]:
        """封存任務並清掉它的 worktree。"""
        await asyncio.shield(runner.archive(task_id))
        return await task_detail(task_id)

    @api.get("/tasks/{task_id}/events")
    async def list_events(
        task_id: int,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 100,
        event_type: Annotated[str | None, Query(alias="type")] = None,
    ) -> dict[str, Any]:
        """稽核紀錄，依寫入順序分頁。`next_after` 不是 null 時用它當下一頁的 `after`。"""
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
        """這個 daemon 能用的 agent 種類，給開任務的表單選。"""
        return [
            {"agent_type": agent_type, "reports_cost": supervisor.reports_cost(agent_type)}
            for agent_type in supervisor.adapters
        ]

    @api.get("/usage")
    async def get_usage(project_id: int | None = None) -> dict[str, Any]:
        """token 與花費彙總。不回報花費的任務，花費是 null，也不計入總花費。"""
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
        """SSE：`event` 是新的稽核紀錄，`task` 是某張卡片的最新內容。"""
        return StreamingResponse(
            stream_text(feed, last_event_id, is_closing),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.mount("/api", api)
    if static_dir is not None:
        # 放在最後：`/` 會接走所有其他路徑。
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app
