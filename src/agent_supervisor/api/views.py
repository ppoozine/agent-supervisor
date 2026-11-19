"""把資料庫裡的資料轉成 API 回應的內容。全部是純函式。"""

from dataclasses import asdict
from typing import Any

from agent_supervisor import db, status


def project_view(project: db.Project) -> dict[str, Any]:
    """一個專案。"""
    return asdict(project)


def card_view(
    snapshot: db.TaskSnapshot, board: status.BoardStatus, *, reports_cost: bool
) -> dict[str, Any]:
    """看板上的一張卡片。欄位與狀態標籤是推導出來的，不存資料庫。

    `cost_usd` 為 `None` 表示這種 agent 不回報花費，花費未知；這時畫面改顯示 token 數。
    `usage_is_complete` 為否表示有 run 的用量拿不到（例如被中止），數字偏低。
    """
    task = snapshot.task
    return {
        "id": task.id,
        "project_id": task.project_id,
        "title": task.title,
        "agent_type": task.agent_type,
        "branch": task.branch,
        "column": str(board.column),
        "label": board.label,
        "has_open_run": snapshot.has_open_run,
        "pr_number": task.pr_number,
        "pr_state": task.pr_state,
        "ci_state": task.ci_state,
        "review_state": task.review_state,
        "merge_state": task.merge_state,
        "ci_fix_attempts": task.ci_fix_attempts,
        "max_ci_fix_attempts": status.MAX_CI_FIX_ATTEMPTS,
        "cost_usd": snapshot.usage.cost_usd if reports_cost else None,
        "tokens": snapshot.usage.tokens,
        "usage_is_complete": snapshot.usage.is_complete,
        "archived_at": task.archived_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def run_view(run: db.Run) -> dict[str, Any]:
    """一次 run。"""
    return {
        "id": run.id,
        "task_id": run.task_id,
        "trigger": str(run.trigger),
        "input_text": run.input_text,
        "source_head_sha": run.source_head_sha,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "exit_status": None if run.exit_status is None else str(run.exit_status),
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "cost_usd": run.cost_usd,
    }


def event_view(event: db.Event) -> dict[str, Any]:
    """一筆稽核紀錄。"""
    return asdict(event)
