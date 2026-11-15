"""跨模組共用的列舉值，對應資料表中的字串欄位。"""

from enum import StrEnum


class RunTrigger(StrEnum):
    """一次 run 的觸發原因。"""

    INITIAL = "initial"
    CI_FAILURE = "ci_failure"
    USER_MESSAGE = "user_message"


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
    STATE_CHANGE = "state_change"
