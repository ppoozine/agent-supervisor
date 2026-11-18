"""status.py 的測試：自動修復條件與看板欄位的推導。"""

from typing import Any

import pytest

from agent_supervisor import db
from agent_supervisor.enums import ExitStatus, RunTrigger
from agent_supervisor.status import (
    BoardStatus,
    Column,
    Limits,
    board_status,
    can_auto_fix,
)

LIMITS = Limits(cost_usd=5.0, tokens=1000)


def snapshot(
    *,
    exit_status: ExitStatus | None = ExitStatus.SUCCESS,
    trigger: RunTrigger = RunTrigger.INITIAL,
    cost_usd: float = 1.0,
    tokens: int = 100,
    require_review: bool = False,
    **task_fields: Any,
) -> db.TaskSnapshot:
    """建立一個快照。預設是：PR 開著、CI 通過、最後一次 run 成功。

    `exit_status` 為 None 表示 run 還在執行。
    """
    fields: dict[str, Any] = {
        "pr_number": 7,
        "pr_state": "open",
        "head_sha": "sha-2",
        "ci_state": "passed",
        "review_state": "none",
        "last_handled_head_sha": None,
        "ci_fix_attempts": 0,
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
    return db.TaskSnapshot(task=task, require_review=require_review, last_run=run, usage=usage)


def failing(**overrides: Any) -> db.TaskSnapshot:
    """一個符合全部自動修復條件的快照，再套用指定的差異。"""
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
    ],
)
def test_each_broken_condition_blocks_auto_fix(overrides: dict[str, Any]) -> None:
    assert not can_auto_fix(failing(**overrides), LIMITS)


def test_two_fix_attempts_still_allow_a_third() -> None:
    assert can_auto_fix(failing(ci_fix_attempts=2), LIMITS)


@pytest.mark.parametrize(
    ("overrides", "column", "label"),
    [
        # 離開看板
        pytest.param({"archived_at": "t"}, Column.ARCHIVE, "Archived", id="archived"),
        pytest.param({"pr_state": "merged"}, Column.ARCHIVE, "Merged", id="merged"),
        pytest.param({"pr_state": "closed"}, Column.ARCHIVE, "Closed", id="closed"),
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
        # 封存優先於一切
        pytest.param(
            {"archived_at": "t", "exit_status": None}, Column.ARCHIVE, "Archived", id="archived-1st"
        ),
        # PR 已合併但 run 還沒停下：仍在 Working，停下後才離開看板
        pytest.param(
            {"pr_state": "merged", "exit_status": None},
            Column.WORKING,
            "Working",
            id="merged-but-run-still-going",
        ),
        # 合併優先於最後一次 run 的結果
        pytest.param(
            {"pr_state": "merged", "exit_status": ExitStatus.STOPPED},
            Column.ARCHIVE,
            "Merged",
            id="merged-beats-stopped",
        ),
        # Working 優先於 Needs you
        pytest.param(
            {"exit_status": None, "review_state": "changes_requested"},
            Column.WORKING,
            "Working",
            id="running-beats-changes-requested",
        ),
        # Needs you 優先於 Ready to merge：run 沒有正常結束，即使 CI 是綠的
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
        # Reviewer 要求修改且 CI 失敗：不自動修復，進 Needs you
        pytest.param(
            {"review_state": "changes_requested", "ci_state": "failed"},
            Column.NEEDS_YOU,
            "Changes requested",
            id="changes-requested-with-failed-ci",
        ),
        # 上限只擋下一次修復，不影響成果能不能合併
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
        # 核准不能蓋過失敗的 CI
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
