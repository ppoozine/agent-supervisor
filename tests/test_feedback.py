"""feedback.py 的測試：哪些留言要餵回 agent，以及餵回的內容。"""

import pytest

from agent_supervisor import feedback
from agent_supervisor.github import FeedbackItem, FeedbackKind


def item(kind: FeedbackKind = FeedbackKind.COMMENT, **fields: object) -> FeedbackItem:
    values: dict[str, object] = {
        "github_id": "IC_1",
        "kind": kind,
        "author": "alice",
        "is_bot": False,
        "body": "please rename this",
        "created_at": "2026-11-19T10:00:00Z",
        "url": "https://github.com/o/r/pull/7#issuecomment-1",
    }
    values.update(fields)
    return FeedbackItem(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("feedback_item", "reason"),
    [
        pytest.param(item(), None, id="comment"),
        pytest.param(item(FeedbackKind.INLINE_COMMENT, path="a.py", line=3), None, id="inline"),
        pytest.param(item(is_bot=True), "bot", id="bot"),
        pytest.param(
            item(FeedbackKind.REVIEW, review_state="COMMENTED", body=""), "empty", id="shell"
        ),
        pytest.param(
            item(FeedbackKind.REVIEW, review_state="COMMENTED"), None, id="review-with-summary"
        ),
        pytest.param(
            item(FeedbackKind.REVIEW, review_state="CHANGES_REQUESTED", body=""),
            None,
            id="changes-requested-without-summary",
        ),
        pytest.param(
            item(FeedbackKind.REVIEW, review_state="APPROVED", body="LGTM"),
            "not_a_change_request",
            id="approval",
        ),
        pytest.param(
            item(FeedbackKind.REVIEW, review_state="DISMISSED"),
            "not_a_change_request",
            id="dismissed",
        ),
    ],
)
def test_which_comments_are_fed_back(feedback_item: FeedbackItem, reason: str | None) -> None:
    assert feedback.ignored_reason(feedback_item) == reason
    assert feedback.to_payload(feedback_item)["ignored"] == reason


def test_prompt_lists_each_comment_with_its_author_and_location() -> None:
    comments = [
        feedback.to_payload(item()),
        feedback.to_payload(
            item(
                FeedbackKind.INLINE_COMMENT,
                author="bob",
                body="off by one",
                path="calc.py",
                line=21,
            )
        ),
        feedback.to_payload(item(FeedbackKind.REVIEW, review_state="CHANGES_REQUESTED", body="")),
    ]

    prompt = feedback.build_prompt(comments)

    assert "Comment 1 by alice:\nplease rename this" in prompt
    assert "Comment 2 by bob on calc.py:21:\noff by one" in prompt
    assert "Comment 3 by alice (requested changes):" in prompt
    assert "Do not commit, push, or reply on GitHub" in prompt


def test_long_comments_are_truncated_so_the_prompt_stays_bounded() -> None:
    comments = [feedback.to_payload(item(body="x" * 5000)) for _ in range(4)]

    prompt = feedback.build_prompt(comments, max_chars=2000)

    assert prompt.count("[truncated]") == 4
    assert len(prompt) < 3000
