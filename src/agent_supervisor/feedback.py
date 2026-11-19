"""PR 上的留言：哪些要餵回 agent，以及餵回時的內容。全部是純函式。"""

from typing import Any

from agent_supervisor.github import FeedbackItem, FeedbackKind

MAX_PROMPT_CHARS = 20_000
_ACTIONABLE_REVIEW_STATES = frozenset({"COMMENTED", "CHANGES_REQUESTED"})


def ignored_reason(item: FeedbackItem) -> str | None:
    """這則留言不餵回 agent 的原因；要餵回時回傳 None。"""
    if item.is_bot:
        return "bot"
    if item.kind is not FeedbackKind.REVIEW:
        return None
    if item.review_state not in _ACTIONABLE_REVIEW_STATES:
        # 核准或已撤銷的 review 不是要求修改；它底下的逐行留言是另外的項目，照常處理。
        return "not_a_change_request"
    if item.review_state == "COMMENTED" and not item.body.strip():
        # 只是用來裝逐行留言的空殼，本身沒有內容。
        return "empty"
    return None


def to_payload(item: FeedbackItem) -> dict[str, Any]:
    """一則留言寫進 `review_comment` 事件的內容。"""
    return {
        "github_id": item.github_id,
        "kind": str(item.kind),
        "author": item.author,
        "body": item.body,
        "path": item.path,
        "line": item.line,
        "review_state": item.review_state,
        "url": item.url,
        "created_at": item.created_at,
        "ignored": ignored_reason(item),
    }


def build_prompt(comments: list[dict[str, Any]], *, max_chars: int = MAX_PROMPT_CHARS) -> str:
    """把還沒處理的留言組成送給 agent 的指示。每則留言分到的長度相同，太長就截斷。"""
    per_comment = max(max_chars // max(len(comments), 1), 200)
    sections = [_section(index, comment, per_comment) for index, comment in enumerate(comments, 1)]
    return (
        "Reviewers left the following comments on the pull request for this task."
        " Address each of them by changing the code in this working directory."
        " If a comment asks a question or you disagree with it, explain your answer"
        " in your final message instead of changing the code."
        " Do not commit, push, or reply on GitHub; that is handled for you.\n\n"
        + "\n\n".join(sections)
    )


def _section(index: int, comment: dict[str, Any], max_chars: int) -> str:
    where = ""
    if comment.get("path"):
        line = f":{comment['line']}" if comment.get("line") else ""
        where = f" on {comment['path']}{line}"
    elif comment.get("review_state") == "CHANGES_REQUESTED":
        where = " (requested changes)"
    body = str(comment.get("body") or "").strip() or "(no text; see the other comments)"
    if len(body) > max_chars:
        body = body[:max_chars] + "\n[truncated]"
    return f"Comment {index} by {comment.get('author') or 'unknown'}{where}:\n{body}"
