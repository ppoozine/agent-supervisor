"""Tests for status.py: auto-fix conditions and deriving the board column."""

from typing import Any

import pytest

from agent_supervisor import db
from agent_supervisor.enums import ExitStatus, RunTrigger
from agent_supervisor.status import (
    BoardStatus,
    Column,
    Limits,
    board_status,
    can_address_feedback,
    can_auto_fix,
    can_resolve_conflict,
)

LIMITS = Limits(cost_usd=5.0, tokens=1000)


def snapshot(
    *,
    exit_status: ExitStatus | None = ExitStatus.SUCCESS,
    trigger: RunTrigger = RunTrigger.INITIAL,
    cost_usd: float = 1.0,
    tokens: int = 100,
    require_review: bool = False,
    pending_feedback: int = 0,
    **task_fields: Any,
) -> db.TaskSnapshot:
    """Build a snapshot. Defaults: PR open, CI passed, last run succeeded.

    An `exit_status` of None means the run is still in progress.
    """
    fields: dict[str, Any] = {
        "pr_number": 7,
        "pr_state": "open",
        "head_sha": "sha-2",
        "ci_state": "passed",
        "review_state": "none",
        "last_handled_head_sha": None,
        "ci_fix_attempts": 0,
        "last_handled_feedback_event_id": None,
        "feedback_attempts": 0,
        "merge_state": "clean",
        "last_handled_conflict_head_sha": None,
        "archived_at": None,
    }
    fields.update(task_fields)
    task = db.Task(
        id=1,
        project_id=1,
        title="demo",
        prompt="do it",
        agent_type="fake",
        model=None,
        session_id="s-1",
        branch="asv/task-1",
        worktree_path="/work",
        created_at="t",
        updated_at="t",
        **fields,
    )
    run = db.Run(
        id=1,
        task_id=1,
        trigger=trigger,
        input_text="x",
        source_head_sha="sha-1" if trigger is RunTrigger.CI_FAILURE else None,
        started_at="t",
        ended_at=None if exit_status is None else "t",
        exit_status=exit_status,
        tokens_in=None,
        tokens_out=None,
        cost_usd=None,
    )
    usage = db.TaskUsage(cost_usd=cost_usd, tokens=tokens, is_complete=True)
    return db.TaskSnapshot(
        task=task,
        require_review=require_review,
        last_run=run,
        usage=usage,
        pending_feedback=pending_feedback,
    )


def failing(**overrides: Any) -> db.TaskSnapshot:
    """A snapshot meeting every auto-fix condition, with the given overrides applied."""
    return snapshot(**{"ci_state": "failed", **overrides})


def test_task_with_a_failed_ci_on_a_new_commit_can_be_fixed() -> None:
    assert can_auto_fix(failing(), LIMITS)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"ci_state": "pending"}, id="1-checks-still-running"),
        pytest.param({"ci_state": "passed"}, id="1-ci-passed"),
        pytest.param({"ci_state": "none"}, id="1-no-checks"),
        pytest.param({"last_handled_head_sha": "sha-2"}, id="2-commit-already-handled"),
        pytest.param({"head_sha": None}, id="2-no-commit-known"),
        pytest.param({"ci_fix_attempts": 3}, id="3-fix-attempts-used-up"),
        pytest.param({"cost_usd": 5.0}, id="4-cost-limit-reached"),
        pytest.param({"tokens": 1000}, id="4-token-limit-reached"),
        pytest.param({"exit_status": None}, id="5-run-in-progress"),
        pytest.param({"pr_state": "merged"}, id="6-pr-merged"),
        pytest.param({"pr_state": "closed"}, id="6-pr-closed"),
        pytest.param({"archived_at": "t"}, id="6-archived"),
        pytest.param({"exit_status": ExitStatus.STOPPED}, id="7-last-run-stopped"),
        pytest.param({"exit_status": ExitStatus.ERROR}, id="7-last-run-error"),
        pytest.param({"exit_status": ExitStatus.LIMIT_REACHED}, id="7-last-run-hit-limit"),
        pytest.param({"exit_status": ExitStatus.INTERRUPTED}, id="7-last-run-interrupted"),
        pytest.param({"review_state": "changes_requested"}, id="8-changes-requested"),
        pytest.param({"pending_feedback": 1}, id="8-comments-waiting"),
        pytest.param({"merge_state": "conflicting"}, id="9-merge-conflict"),
    ],
)
def test_each_broken_condition_blocks_auto_fix(overrides: dict[str, Any]) -> None:
    assert not can_auto_fix(failing(**overrides), LIMITS)


def test_two_fix_attempts_still_allow_a_third() -> None:
    assert can_auto_fix(failing(ci_fix_attempts=2), LIMITS)


@pytest.mark.parametrize(
    ("overrides", "column", "label"),
    [
        # Leaves the board
        pytest.param({"archived_at": "t"}, Column.ARCHIVE, "Archived", id="archived"),
        pytest.param({"pr_state": "merged"}, Column.ARCHIVE, "Merged", id="merged"),
        pytest.param({"pr_state": "closed"}, Column.ARCHIVE, "Closed", id="closed"),
        # Archived tasks show the PR outcome first; they are Archived only while the PR is open
        pytest.param(
            {"archived_at": "t", "pr_state": "merged"},
            Column.ARCHIVE,
            "Merged",
            id="archived-merged",
        ),
        pytest.param(
            {"archived_at": "t", "pr_state": "closed"},
            Column.ARCHIVE,
            "Closed",
            id="archived-closed",
        ),
        pytest.param(
            {"archived_at": "t", "pr_state": "open"}, Column.ARCHIVE, "Archived", id="archived-open"
        ),
        # Working
        pytest.param({"exit_status": None}, Column.WORKING, "Working", id="run-in-progress"),
        pytest.param(
            {"exit_status": None, "trigger": RunTrigger.CI_FAILURE},
            Column.WORKING,
            "Fixing CI",
            id="fix-run-in-progress",
        ),
        pytest.param({"ci_state": "failed"}, Column.WORKING, "Fixing CI", id="waiting-for-fix"),
        # Needs you
        pytest.param(
            {"exit_status": ExitStatus.ERROR}, Column.NEEDS_YOU, "Agent error", id="run-error"
        ),
        pytest.param(
            {"exit_status": ExitStatus.STOPPED}, Column.NEEDS_YOU, "Stopped", id="run-stopped"
        ),
        pytest.param(
            {"exit_status": ExitStatus.LIMIT_REACHED},
            Column.NEEDS_YOU,
            "Limit reached",
            id="run-hit-limit",
        ),
        pytest.param(
            {"exit_status": ExitStatus.INTERRUPTED},
            Column.NEEDS_YOU,
            "Interrupted",
            id="run-interrupted",
        ),
        pytest.param(
            {"pr_number": None, "pr_state": None, "ci_state": "none", "head_sha": None},
            Column.NEEDS_YOU,
            "No changes",
            id="success-without-pr",
        ),
        pytest.param(
            {"ci_state": "failed", "ci_fix_attempts": 3},
            Column.NEEDS_YOU,
            "CI failed",
            id="fix-attempts-used-up",
        ),
        pytest.param(
            {"ci_state": "failed", "last_handled_head_sha": "sha-2"},
            Column.NEEDS_YOU,
            "CI failed",
            id="fix-made-no-new-commit",
        ),
        pytest.param(
            {"ci_state": "failed", "cost_usd": 9.0}, Column.NEEDS_YOU, "Over budget", id="over-cost"
        ),
        pytest.param(
            {"ci_state": "failed", "tokens": 5000},
            Column.NEEDS_YOU,
            "Over budget",
            id="over-tokens",
        ),
        pytest.param(
            {"review_state": "changes_requested"},
            Column.NEEDS_YOU,
            "Changes requested",
            id="changes-requested",
        ),
        # Ready to merge
        pytest.param({}, Column.READY_TO_MERGE, "Ready", id="ci-passed-no-review-needed"),
        pytest.param(
            {"require_review": True, "review_state": "approved"},
            Column.READY_TO_MERGE,
            "Ready",
            id="ci-passed-and-approved",
        ),
        # In review
        pytest.param({"ci_state": "pending"}, Column.IN_REVIEW, "CI running", id="ci-running"),
        pytest.param({"ci_state": "none"}, Column.IN_REVIEW, "No checks", id="no-checks"),
        pytest.param(
            {"require_review": True}, Column.IN_REVIEW, "Review pending", id="waiting-for-review"
        ),
    ],
)
def test_board_status_for_each_row_of_the_spec(
    overrides: dict[str, Any], column: Column, label: str
) -> None:
    assert board_status(snapshot(**overrides), LIMITS) == BoardStatus(column, label)


@pytest.mark.parametrize(
    ("overrides", "column", "label"),
    [
        # Archiving takes precedence over everything
        pytest.param(
            {"archived_at": "t", "exit_status": None}, Column.ARCHIVE, "Archived", id="archived-1st"
        ),
        # PR merged but the run has not stopped: still Working; leaves the board once it stops
        pytest.param(
            {"pr_state": "merged", "exit_status": None},
            Column.WORKING,
            "Working",
            id="merged-but-run-still-going",
        ),
        # Merging takes precedence over the last run's outcome
        pytest.param(
            {"pr_state": "merged", "exit_status": ExitStatus.STOPPED},
            Column.ARCHIVE,
            "Merged",
            id="merged-beats-stopped",
        ),
        # Working takes precedence over Needs you
        pytest.param(
            {"exit_status": None, "review_state": "changes_requested"},
            Column.WORKING,
            "Working",
            id="running-beats-changes-requested",
        ),
        # Needs you takes precedence over Ready to merge: the run did not end normally, even if CI
        # is green
        pytest.param(
            {"exit_status": ExitStatus.STOPPED},
            Column.NEEDS_YOU,
            "Stopped",
            id="stopped-beats-green-ci",
        ),
        pytest.param(
            {"review_state": "changes_requested", "ci_state": "pending"},
            Column.NEEDS_YOU,
            "Changes requested",
            id="changes-requested-beats-ci-running",
        ),
        # Reviewer requested changes and CI failed: no auto-fix; goes to Needs you
        pytest.param(
            {"review_state": "changes_requested", "ci_state": "failed"},
            Column.NEEDS_YOU,
            "Changes requested",
            id="changes-requested-with-failed-ci",
        ),
        # The limit only blocks the next fix; it does not affect whether the work can be merged
        pytest.param(
            {"ci_fix_attempts": 3, "trigger": RunTrigger.CI_FAILURE},
            Column.READY_TO_MERGE,
            "Ready",
            id="third-fix-turned-ci-green",
        ),
        pytest.param({"cost_usd": 9.0}, Column.READY_TO_MERGE, "Ready", id="over-budget-but-green"),
        pytest.param(
            {"cost_usd": 9.0, "ci_state": "pending"},
            Column.IN_REVIEW,
            "CI running",
            id="over-budget-ci-running",
        ),
        # Approval cannot override a failed CI
        pytest.param(
            {"review_state": "approved", "ci_state": "failed", "ci_fix_attempts": 3},
            Column.NEEDS_YOU,
            "CI failed",
            id="approved-but-ci-failed",
        ),
    ],
)
def test_board_status_priorities_and_combinations(
    overrides: dict[str, Any], column: Column, label: str
) -> None:
    assert board_status(snapshot(**overrides), LIMITS) == BoardStatus(column, label)


def test_task_with_waiting_comments_can_have_them_addressed() -> None:
    assert can_address_feedback(snapshot(pending_feedback=2), LIMITS)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"pending_feedback": 0}, id="no-comments-waiting"),
        pytest.param({"feedback_attempts": 5}, id="attempts-used-up"),
        pytest.param({"cost_usd": 5.0}, id="cost-limit-reached"),
        pytest.param({"tokens": 1000}, id="token-limit-reached"),
        pytest.param({"exit_status": None}, id="run-in-progress"),
        pytest.param({"pr_state": "merged"}, id="pr-merged"),
        pytest.param({"archived_at": "t"}, id="archived"),
        pytest.param({"exit_status": ExitStatus.STOPPED}, id="last-run-stopped"),
        pytest.param({"exit_status": ExitStatus.ERROR}, id="last-run-error"),
    ],
)
def test_each_broken_condition_blocks_addressing_feedback(overrides: dict[str, Any]) -> None:
    assert not can_address_feedback(snapshot(**{"pending_feedback": 1, **overrides}), LIMITS)


@pytest.mark.parametrize(
    ("overrides", "column", "label"),
    [
        pytest.param({"pending_feedback": 1}, Column.WORKING, "Addressing review", id="waiting"),
        pytest.param(
            {"exit_status": None, "trigger": RunTrigger.REVIEW_FEEDBACK},
            Column.WORKING,
            "Addressing review",
            id="run-in-progress",
        ),
        # PR comments take precedence over CI failure: handling them produces a new commit
        pytest.param(
            {"pending_feedback": 1, "ci_state": "failed"},
            Column.WORKING,
            "Addressing review",
            id="comments-beat-failed-ci",
        ),
        pytest.param(
            {"pending_feedback": 1, "feedback_attempts": 5},
            Column.NEEDS_YOU,
            "Changes requested",
            id="attempts-used-up",
        ),
        pytest.param(
            {"pending_feedback": 1, "cost_usd": 5.0},
            Column.NEEDS_YOU,
            "Changes requested",
            id="over-budget",
        ),
        pytest.param(
            {"pending_feedback": 1, "exit_status": ExitStatus.STOPPED},
            Column.NEEDS_YOU,
            "Stopped",
            id="stopped-beats-comments",
        ),
        # GitHub keeps reporting "changes requested" until re-review; once the comments are handled
        # it no longer blocks the flow
        pytest.param(
            {"review_state": "changes_requested", "last_handled_feedback_event_id": 9},
            Column.READY_TO_MERGE,
            "Ready",
            id="changes-requested-already-addressed",
        ),
        pytest.param(
            {"review_state": "changes_requested"},
            Column.NEEDS_YOU,
            "Changes requested",
            id="changes-requested-without-any-comment",
        ),
    ],
)
def test_board_status_with_review_feedback(
    overrides: dict[str, Any], column: Column, label: str
) -> None:
    assert board_status(snapshot(**overrides), LIMITS) == BoardStatus(column, label)


def test_failed_ci_is_fixed_again_once_the_requested_changes_were_addressed() -> None:
    addressed = failing(review_state="changes_requested", last_handled_feedback_event_id=9)

    assert can_auto_fix(addressed, LIMITS)


def conflicting(**overrides: Any) -> db.TaskSnapshot:
    """A snapshot meeting every conflict-resolution condition, with the given overrides applied."""
    return snapshot(**{"merge_state": "conflicting", **overrides})


def test_task_with_a_conflict_on_a_new_commit_can_have_it_resolved() -> None:
    assert can_resolve_conflict(conflicting(), LIMITS)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"merge_state": "clean"}, id="no-conflict"),
        pytest.param({"merge_state": "unknown"}, id="not-computed-yet"),
        pytest.param({"last_handled_conflict_head_sha": "sha-2"}, id="commit-already-handled"),
        pytest.param({"head_sha": None}, id="no-commit-known"),
        pytest.param({"ci_fix_attempts": 3}, id="fix-attempts-used-up"),
        pytest.param({"cost_usd": 5.0}, id="cost-limit-reached"),
        pytest.param({"exit_status": None}, id="run-in-progress"),
        pytest.param({"pr_state": "closed"}, id="pr-closed"),
        pytest.param({"archived_at": "t"}, id="archived"),
        pytest.param({"exit_status": ExitStatus.STOPPED}, id="last-run-stopped"),
        pytest.param({"pending_feedback": 1}, id="comments-waiting"),
    ],
)
def test_each_broken_condition_blocks_resolving_a_conflict(overrides: dict[str, Any]) -> None:
    assert not can_resolve_conflict(conflicting(**overrides), LIMITS)


def test_ci_failure_handled_on_a_commit_does_not_hide_a_conflict_on_it() -> None:
    assert can_resolve_conflict(conflicting(last_handled_head_sha="sha-2"), LIMITS)


@pytest.mark.parametrize(
    ("overrides", "column", "label"),
    [
        pytest.param(
            {"merge_state": "conflicting"}, Column.WORKING, "Resolving conflict", id="waiting"
        ),
        pytest.param(
            {"exit_status": None, "trigger": RunTrigger.MERGE_CONFLICT},
            Column.WORKING,
            "Resolving conflict",
            id="run-in-progress",
        ),
        # PR comments > conflicts > CI failure
        pytest.param(
            {"merge_state": "conflicting", "pending_feedback": 1},
            Column.WORKING,
            "Addressing review",
            id="comments-beat-conflict",
        ),
        pytest.param(
            {"merge_state": "conflicting", "ci_state": "failed"},
            Column.WORKING,
            "Resolving conflict",
            id="conflict-beats-failed-ci",
        ),
        pytest.param(
            {"merge_state": "conflicting", "last_handled_conflict_head_sha": "sha-2"},
            Column.NEEDS_YOU,
            "Merge conflict",
            id="still-conflicting-after-a-run",
        ),
        pytest.param(
            {"merge_state": "conflicting", "ci_fix_attempts": 3},
            Column.NEEDS_YOU,
            "Merge conflict",
            id="attempts-used-up",
        ),
        # CI passed but there are conflicts, or conflicts are unknown yet: not ready to merge
        pytest.param(
            {"merge_state": "unknown"}, Column.IN_REVIEW, "Checking mergeability", id="unknown"
        ),
        pytest.param(
            {"merge_state": "unknown", "ci_state": "pending"},
            Column.IN_REVIEW,
            "CI running",
            id="unknown-while-ci-runs",
        ),
    ],
)
def test_board_status_with_merge_conflicts(
    overrides: dict[str, Any], column: Column, label: str
) -> None:
    assert board_status(snapshot(**overrides), LIMITS) == BoardStatus(column, label)
