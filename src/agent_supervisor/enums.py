"""Enum values shared across modules, matching string columns in the database."""

from enum import StrEnum


class RunTrigger(StrEnum):
    """Why a run was triggered."""

    INITIAL = "initial"
    CI_FAILURE = "ci_failure"
    USER_MESSAGE = "user_message"
    REVIEW_FEEDBACK = "review_feedback"
    MERGE_CONFLICT = "merge_conflict"


class ExitStatus(StrEnum):
    """How a run finished."""

    SUCCESS = "success"
    ERROR = "error"
    STOPPED = "stopped"
    LIMIT_REACHED = "limit_reached"
    INTERRUPTED = "interrupted"


class EventType(StrEnum):
    """Valid values of `events.type`."""

    SESSION_STARTED = "session_started"
    AGENT_MESSAGE = "agent_message"
    TOOL_CALL = "tool_call"
    COMMAND = "command"
    PERMISSION_DENIED = "permission_denied"
    PUSHED = "pushed"
    PR_OPENED = "pr_opened"
    CI_STATUS = "ci_status"
    REVIEW_COMMENT = "review_comment"
    STATE_CHANGE = "state_change"


class PrState(StrEnum):
    """Valid values of `tasks.pr_state`."""

    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class CiState(StrEnum):
    """Valid values of `tasks.ci_state`: the combined result of all CI checks on `head_sha`."""

    NONE = "none"
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class ReviewState(StrEnum):
    """Valid values of `tasks.review_state`."""

    NONE = "none"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class MergeState(StrEnum):
    """Valid values of `tasks.merge_state`: whether the PR can merge into the base branch."""

    UNKNOWN = "unknown"
    CLEAN = "clean"
    CONFLICTING = "conflicting"
