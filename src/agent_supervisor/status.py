"""Derive board columns, status labels, and auto-fix conditions from task state. All pure.

Board columns are not stored in the database: they are computed each time from the run, PR, CI, and
review state.
The "auto-fix conditions" live here and are shared with the Dispatcher, so the two never disagree.
"""

from dataclasses import dataclass
from enum import StrEnum

from agent_supervisor.db import TaskSnapshot
from agent_supervisor.enums import (
    CiState,
    ExitStatus,
    MergeState,
    PrState,
    ReviewState,
    RunTrigger,
)

MAX_CI_FIX_ATTEMPTS = 3
MAX_FEEDBACK_ATTEMPTS = 5


class Column(StrEnum):
    """The four board columns, plus the Archive area for tasks that have left the board."""

    WORKING = "Working"
    NEEDS_YOU = "Needs you"
    IN_REVIEW = "In review"
    READY_TO_MERGE = "Ready to merge"
    ARCHIVE = "Archive"


@dataclass(frozen=True)
class Limits:
    """Per-task cumulative limits."""

    cost_usd: float
    tokens: int


@dataclass(frozen=True)
class BoardStatus:
    """The column a card is in, and the status label explaining why."""

    column: Column
    label: str


_EXIT_LABELS = {
    ExitStatus.ERROR: "Agent error",
    ExitStatus.STOPPED: "Stopped",
    ExitStatus.LIMIT_REACHED: "Limit reached",
    ExitStatus.INTERRUPTED: "Interrupted",
}
_FINISHED_PR_LABELS = {PrState.MERGED: "Merged", PrState.CLOSED: "Closed"}
_WORKING_LABELS = {
    RunTrigger.CI_FAILURE: "Fixing CI",
    RunTrigger.REVIEW_FEEDBACK: "Addressing review",
    RunTrigger.MERGE_CONFLICT: "Resolving conflict",
}


def has_remaining_budget(snapshot: TaskSnapshot, limits: Limits) -> bool:
    """Whether the cost and tokens since the latest human intervention are below the limits."""
    return snapshot.budget.cost_usd < limits.cost_usd and snapshot.budget.tokens < limits.tokens


def can_auto_fix(snapshot: TaskSnapshot, limits: Limits) -> bool:
    """Whether the task meets the auto-fix conditions; all nine must hold."""
    task = snapshot.task
    last_run = snapshot.last_run
    if task.ci_state != CiState.FAILED:
        return False
    if task.merge_state == MergeState.CONFLICTING:
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
    return not has_unaddressed_review(snapshot)


def can_resolve_conflict(snapshot: TaskSnapshot, limits: Limits) -> bool:
    """Whether the task meets the "ask the agent to resolve conflicts" conditions; all must hold."""
    task = snapshot.task
    last_run = snapshot.last_run
    if task.merge_state != MergeState.CONFLICTING:
        return False
    if task.head_sha is None or task.head_sha == task.last_handled_conflict_head_sha:
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
    return not has_unaddressed_review(snapshot)


def has_unaddressed_review(snapshot: TaskSnapshot) -> bool:
    """Whether the PR still has unhandled review feedback.

    It does if there are PR comments not yet fed back to the agent. It also does when a reviewer
    requested changes without leaving any PR comment that can be fed back:
    GitHub keeps reporting "changes requested" until the reviewer reviews again, so once the PR
    comments have been handled it no longer counts;
    otherwise the card would get stuck in Needs you and CI would no longer be auto-fixed.
    """
    task = snapshot.task
    if snapshot.pending_feedback > 0:
        return True
    was_never_addressed = task.last_handled_feedback_event_id is None
    return task.review_state == ReviewState.CHANGES_REQUESTED and was_never_addressed


def can_address_feedback(snapshot: TaskSnapshot, limits: Limits) -> bool:
    """Whether the task meets the "feed PR comments back to the agent" conditions; all must hold."""
    task = snapshot.task
    last_run = snapshot.last_run
    if snapshot.pending_feedback == 0:
        return False
    if task.feedback_attempts >= MAX_FEEDBACK_ATTEMPTS:
        return False
    if not has_remaining_budget(snapshot, limits):
        return False
    if snapshot.has_open_run:
        return False
    if task.pr_state != PrState.OPEN or task.archived_at is not None:
        return False
    return last_run is not None and last_run.exit_status is ExitStatus.SUCCESS


def board_status(snapshot: TaskSnapshot, limits: Limits) -> BoardStatus:
    """Derive the card's column and status label. Checked top to bottom; the first match wins."""
    task = snapshot.task
    last_run = snapshot.last_run
    # The PR outcome is more useful than "was archived", so it is shown first even for archived
    # tasks.
    if task.pr_state in _FINISHED_PR_LABELS and not snapshot.has_open_run:
        return BoardStatus(Column.ARCHIVE, _FINISHED_PR_LABELS[PrState(task.pr_state)])
    if task.archived_at is not None:
        return BoardStatus(Column.ARCHIVE, "Archived")

    if snapshot.has_open_run:
        return BoardStatus(Column.WORKING, _working_label(snapshot))
    if can_address_feedback(snapshot, limits):
        return BoardStatus(Column.WORKING, "Addressing review")
    if can_resolve_conflict(snapshot, limits):
        return BoardStatus(Column.WORKING, "Resolving conflict")
    if can_auto_fix(snapshot, limits):
        return BoardStatus(Column.WORKING, "Fixing CI")

    if last_run is not None and last_run.exit_status in _EXIT_LABELS:
        return BoardStatus(Column.NEEDS_YOU, _EXIT_LABELS[last_run.exit_status])
    if task.pr_number is None:
        return BoardStatus(Column.NEEDS_YOU, "No changes")
    if has_unaddressed_review(snapshot):
        return BoardStatus(Column.NEEDS_YOU, "Changes requested")
    if task.merge_state == MergeState.CONFLICTING:
        return BoardStatus(Column.NEEDS_YOU, "Merge conflict")
    if task.ci_state == CiState.FAILED:
        label = "CI failed" if has_remaining_budget(snapshot, limits) else "Over budget"
        return BoardStatus(Column.NEEDS_YOU, label)

    if task.ci_state == CiState.PASSED:
        if task.merge_state != MergeState.CLEAN:
            # GitHub hasn't computed mergeability yet: if we don't know, we can't say it's
            # mergeable.
            return BoardStatus(Column.IN_REVIEW, "Checking mergeability")
        if not snapshot.require_review or task.review_state == ReviewState.APPROVED:
            return BoardStatus(Column.READY_TO_MERGE, "Ready")
        return BoardStatus(Column.IN_REVIEW, "Review pending")
    if task.ci_state == CiState.NONE:
        return BoardStatus(Column.IN_REVIEW, "No checks")
    return BoardStatus(Column.IN_REVIEW, "CI running")


def _working_label(snapshot: TaskSnapshot) -> str:
    last_run = snapshot.last_run
    if last_run is None:
        return "Working"
    return _WORKING_LABELS.get(last_run.trigger, "Working")
