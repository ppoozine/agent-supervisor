"""由任務的狀態推導看板欄位、狀態標籤與自動修復條件。全部是純函式。

看板欄位不存資料庫：每次都從 run、PR、CI、review 的狀態算出來。
「自動修復條件」由這裡與 Dispatcher 共用同一份，避免兩邊判斷不一致。
"""

from dataclasses import dataclass
from enum import StrEnum

from agent_supervisor.db import TaskSnapshot
from agent_supervisor.enums import CiState, ExitStatus, PrState, ReviewState

MAX_CI_FIX_ATTEMPTS = 3


class Column(StrEnum):
    """看板的四個欄位，加上離開看板後的 Archive 區。"""

    WORKING = "Working"
    NEEDS_YOU = "Needs you"
    IN_REVIEW = "In review"
    READY_TO_MERGE = "Ready to merge"
    ARCHIVE = "Archive"


@dataclass(frozen=True)
class Limits:
    """單一任務的累計上限。"""

    cost_usd: float
    tokens: int


@dataclass(frozen=True)
class BoardStatus:
    """一張卡片所在的欄位，以及說明原因的狀態標籤。"""

    column: Column
    label: str


_EXIT_LABELS = {
    ExitStatus.ERROR: "Agent error",
    ExitStatus.STOPPED: "Stopped",
    ExitStatus.LIMIT_REACHED: "Limit reached",
    ExitStatus.INTERRUPTED: "Interrupted",
}
_FINISHED_PR_LABELS = {PrState.MERGED: "Merged", PrState.CLOSED: "Closed"}


def has_remaining_budget(snapshot: TaskSnapshot, limits: Limits) -> bool:
    """任務的累計花費與累計 token 數是否都還沒到上限。"""
    return snapshot.usage.cost_usd < limits.cost_usd and snapshot.usage.tokens < limits.tokens


def can_auto_fix(snapshot: TaskSnapshot, limits: Limits) -> bool:
    """任務是否符合自動修復條件；八條全部成立才算。"""
    task = snapshot.task
    last_run = snapshot.last_run
    if task.ci_state != CiState.FAILED:
        return False
    if task.head_sha is None or task.head_sha == task.last_handled_head_sha:
        return False
    if task.ci_fix_attempts >= MAX_CI_FIX_ATTEMPTS:
        return False
    if not has_remaining_budget(snapshot, limits):
        return False
    if snapshot.has_open_run:
        return False
    if task.pr_state != PrState.OPEN or task.archived_at is not None:
        return False
    if last_run is None or last_run.exit_status is not ExitStatus.SUCCESS:
        return False
    return task.review_state != ReviewState.CHANGES_REQUESTED


def board_status(snapshot: TaskSnapshot, limits: Limits) -> BoardStatus:
    """推導卡片所在的欄位與狀態標籤。由上往下判斷，先符合者優先。"""
    task = snapshot.task
    last_run = snapshot.last_run
    # PR 的結果比「封存過」有用，所以已封存的任務也優先顯示它。
    if task.pr_state in _FINISHED_PR_LABELS and not snapshot.has_open_run:
        return BoardStatus(Column.ARCHIVE, _FINISHED_PR_LABELS[PrState(task.pr_state)])
    if task.archived_at is not None:
        return BoardStatus(Column.ARCHIVE, "Archived")

    if snapshot.has_open_run:
        return BoardStatus(Column.WORKING, _working_label(snapshot))
    if can_auto_fix(snapshot, limits):
        return BoardStatus(Column.WORKING, "Fixing CI")

    if last_run is not None and last_run.exit_status in _EXIT_LABELS:
        return BoardStatus(Column.NEEDS_YOU, _EXIT_LABELS[last_run.exit_status])
    if task.pr_number is None:
        return BoardStatus(Column.NEEDS_YOU, "No changes")
    if task.review_state == ReviewState.CHANGES_REQUESTED:
        return BoardStatus(Column.NEEDS_YOU, "Changes requested")
    if task.ci_state == CiState.FAILED:
        label = "CI failed" if has_remaining_budget(snapshot, limits) else "Over budget"
        return BoardStatus(Column.NEEDS_YOU, label)

    if task.ci_state == CiState.PASSED:
        if not snapshot.require_review or task.review_state == ReviewState.APPROVED:
            return BoardStatus(Column.READY_TO_MERGE, "Ready")
        return BoardStatus(Column.IN_REVIEW, "Review pending")
    if task.ci_state == CiState.NONE:
        return BoardStatus(Column.IN_REVIEW, "No checks")
    return BoardStatus(Column.IN_REVIEW, "CI running")


def _working_label(snapshot: TaskSnapshot) -> str:
    last_run = snapshot.last_run
    is_fixing = last_run is not None and last_run.source_head_sha is not None
    return "Fixing CI" if is_fixing else "Working"
