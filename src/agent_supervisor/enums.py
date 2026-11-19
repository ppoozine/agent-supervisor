"""跨模組共用的列舉值，對應資料表中的字串欄位。"""

from enum import StrEnum


class RunTrigger(StrEnum):
    """一次 run 的觸發原因。"""

    INITIAL = "initial"
    CI_FAILURE = "ci_failure"
    USER_MESSAGE = "user_message"
    REVIEW_FEEDBACK = "review_feedback"
    MERGE_CONFLICT = "merge_conflict"


class ExitStatus(StrEnum):
    """一次 run 的結案結果。"""

    SUCCESS = "success"
    ERROR = "error"
    STOPPED = "stopped"
    LIMIT_REACHED = "limit_reached"
    INTERRUPTED = "interrupted"


class EventType(StrEnum):
    """`events.type` 的合法值。"""

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
    """`tasks.pr_state` 的合法值。"""

    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class CiState(StrEnum):
    """`tasks.ci_state` 的合法值：`head_sha` 上所有 CI 檢查合起來的結果。"""

    NONE = "none"
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class ReviewState(StrEnum):
    """`tasks.review_state` 的合法值。"""

    NONE = "none"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class MergeState(StrEnum):
    """`tasks.merge_state` 的合法值：PR 能不能合併進基底分支。"""

    UNKNOWN = "unknown"
    CLEAN = "clean"
    CONFLICTING = "conflicting"
