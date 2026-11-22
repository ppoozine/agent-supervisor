"""Turn database data into API response content. All pure functions."""

from dataclasses import asdict
from typing import Any

from agent_supervisor import db, status
from agent_supervisor.readiness import Readiness
from agent_supervisor.tool_status import ToolStatus


def project_view(project: db.Project) -> dict[str, Any]:
    """A project."""
    return asdict(project)


def card_view(
    snapshot: db.TaskSnapshot, board: status.BoardStatus, *, reports_cost: bool
) -> dict[str, Any]:
    """A card on the board. The column and status label are derived, not stored in the database.

    `cost_usd` of `None` means this agent type doesn't report cost, so cost is unknown; the UI
    then shows the token count instead. `usage_is_complete` being false means some run's usage
    was unavailable (e.g. it was stopped), so the numbers are understated.
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
        "head_sha": task.head_sha,
        "ci_state": task.ci_state,
        "review_state": task.review_state,
        "merge_state": task.merge_state,
        "ci_fix_attempts": task.ci_fix_attempts,
        "max_ci_fix_attempts": status.MAX_CI_FIX_ATTEMPTS,
        "feedback_attempts": task.feedback_attempts,
        "max_feedback_attempts": status.MAX_FEEDBACK_ATTEMPTS,
        "cost_usd": snapshot.usage.cost_usd if reports_cost else None,
        "tokens": snapshot.usage.tokens,
        "usage_is_complete": snapshot.usage.is_complete,
        "archived_at": task.archived_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def run_view(run: db.Run) -> dict[str, Any]:
    """A run."""
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
    """An audit log entry."""
    return asdict(event)


MAX_DIFF_CHARS = 400_000


def diff_view(text: str, *, base_branch: str) -> dict[str, Any]:
    """A diff. If too long, cut at the last complete line and mark it as truncated."""
    is_truncated = len(text) > MAX_DIFF_CHARS
    if is_truncated:
        text = text[:MAX_DIFF_CHARS]
        text = text[: text.rfind("\n") + 1]
    return {"diff": text, "is_truncated": is_truncated, "base_branch": base_branch}


def tool_status_view(tool: ToolStatus) -> dict[str, Any]:
    """Whether one tool can be used, what it is signed in with, and how to fix it if not."""
    return {
        "usable": tool.usable,
        "installed": tool.installed,
        "version": tool.version,
        "auth": str(tool.auth),
        "auth_label": tool.auth_label,
        "detail": tool.detail,
        "fix": list(tool.fix),
        "notice": tool.notice,
    }


def readiness_view(readiness: Readiness, display_names: dict[str, str]) -> dict[str, Any]:
    """Every agent's and gh's status, and when they were checked."""
    return {
        "checked_at": readiness.checked_at,
        "agents": [
            {
                "agent_type": agent_type,
                "display_name": display_names[agent_type],
                **tool_status_view(tool),
            }
            for agent_type, tool in readiness.agents.items()
        ],
        "github": tool_status_view(readiness.github),
    }
