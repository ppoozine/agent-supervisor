"""PR comments: which to feed back to the agent, and what to send. All functions are pure."""

from typing import Any

from agent_supervisor.github import FeedbackItem, FeedbackKind

MAX_PROMPT_CHARS = 20_000
_ACTIONABLE_REVIEW_STATES = frozenset({"COMMENTED", "CHANGES_REQUESTED"})
# Authors whose comments reach the agent: the repo's owner, members of the organization that owns
# it, and invited collaborators. Anyone else (for example anyone on a public repo) could otherwise
# instruct an agent that runs with the user's access.
TRUSTED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def ignored_reason(item: FeedbackItem) -> str | None:
    """Why this PR comment is not fed back to the agent; None if it should be fed back."""
    if item.is_bot:
        return "bot"
    if item.author_association not in TRUSTED_AUTHOR_ASSOCIATIONS:
        return "untrusted_author"
    if item.kind is not FeedbackKind.REVIEW:
        return None
    if item.review_state not in _ACTIONABLE_REVIEW_STATES:
        # An approved or dismissed review is not a change request; its inline comments are separate
        # items and are handled as usual.
        return "not_a_change_request"
    if item.review_state == "COMMENTED" and not item.body.strip():
        # Just an empty shell holding inline comments; it has no content of its own.
        return "empty"
    return None


def to_payload(item: FeedbackItem) -> dict[str, Any]:
    """The content written into a `review_comment` event for one PR comment."""
    return {
        "github_id": item.github_id,
        "kind": str(item.kind),
        "author": item.author,
        "author_association": item.author_association,
        "body": item.body,
        "path": item.path,
        "line": item.line,
        "review_state": item.review_state,
        "url": item.url,
        "created_at": item.created_at,
        "ignored": ignored_reason(item),
    }


def build_prompt(comments: list[dict[str, Any]], *, max_chars: int = MAX_PROMPT_CHARS) -> str:
    """Build the instructions for the agent from the unhandled PR comments.

    Each comment gets the same share of the length; longer ones are truncated.
    """
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
