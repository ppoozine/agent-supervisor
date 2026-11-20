"""Tests for github.py. `gh` calls go to a fake; GitHub is never contacted."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_supervisor.enums import CiState, PrState, ReviewState
from agent_supervisor.github import (
    FailedCheck,
    FeedbackKind,
    GitHub,
    GitHubError,
    parse_pull_request_status,
)
from agent_supervisor.process import CommandResult


class FakeGh:
    """Record the received arguments and return the prepared results in order."""

    def __init__(self, results: list[CommandResult]) -> None:
        self.results = results
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []

    async def __call__(
        self, args: Sequence[str], *, cwd: Path | None = None, input_text: str | None = None
    ) -> CommandResult:
        self.calls.append(list(args))
        self.inputs.append(input_text)
        return self.results.pop(0)


def ok(stdout: str) -> CommandResult:
    return CommandResult(returncode=0, stdout=stdout, stderr="")


def pr_json(*prs: dict[str, object]) -> CommandResult:
    return ok(json.dumps(list(prs)))


async def test_find_returns_none_when_branch_has_no_pull_request() -> None:
    gh = FakeGh([pr_json()])

    found = await GitHub(gh).find_pull_request("owner/repo", "asv/task-1")

    assert found is None
    assert gh.calls[0][:3] == ["gh", "pr", "list"]
    assert _option(gh.calls[0], "--repo") == "owner/repo"
    assert _option(gh.calls[0], "--head") == "asv/task-1"
    assert _option(gh.calls[0], "--state") == "all"


async def test_find_returns_number_state_and_head_sha() -> None:
    gh = FakeGh([pr_json({"number": 12, "state": "OPEN", "headRefOid": "abc123", "url": "u"})])

    found = await GitHub(gh).find_pull_request("owner/repo", "asv/task-1")

    assert found is not None
    assert (found.number, found.state, found.head_sha, found.url) == (12, "open", "abc123", "u")


async def test_find_prefers_an_open_pull_request_over_closed_ones() -> None:
    gh = FakeGh(
        [
            pr_json(
                {"number": 3, "state": "CLOSED", "headRefOid": "a", "url": "u3"},
                {"number": 9, "state": "OPEN", "headRefOid": "b", "url": "u9"},
                {"number": 5, "state": "MERGED", "headRefOid": "c", "url": "u5"},
            )
        ]
    )

    found = await GitHub(gh).find_pull_request("owner/repo", "asv/task-1")

    assert found is not None
    assert found.number == 9


async def test_find_falls_back_to_the_newest_finished_pull_request() -> None:
    gh = FakeGh(
        [
            pr_json(
                {"number": 3, "state": "CLOSED", "headRefOid": "a", "url": "u3"},
                {"number": 5, "state": "MERGED", "headRefOid": "c", "url": "u5"},
            )
        ]
    )

    found = await GitHub(gh).find_pull_request("owner/repo", "asv/task-1")

    assert found is not None
    assert (found.number, found.state) == (5, "merged")


async def test_create_opens_a_pull_request_and_returns_it() -> None:
    gh = FakeGh(
        [
            ok("https://github.com/owner/repo/pull/21\n"),
            pr_json({"number": 21, "state": "OPEN", "headRefOid": "sha", "url": "u21"}),
        ]
    )

    created = await GitHub(gh).create_pull_request(
        "owner/repo", head="asv/task-1", base="main", title="Fix it", body="the body"
    )

    create_call = gh.calls[0]
    assert create_call[:3] == ["gh", "pr", "create"]
    assert _option(create_call, "--repo") == "owner/repo"
    assert _option(create_call, "--head") == "asv/task-1"
    assert _option(create_call, "--base") == "main"
    assert _option(create_call, "--title") == "Fix it"
    assert _option(create_call, "--body-file") == "-"
    assert gh.inputs[0] == "the body"
    assert (created.number, created.state) == (21, "open")


async def test_failed_command_raises_with_the_error_output() -> None:
    gh = FakeGh([CommandResult(returncode=1, stdout="", stderr="HTTP 401: Bad credentials")])

    with pytest.raises(GitHubError, match="Bad credentials"):
        await GitHub(gh).find_pull_request("owner/repo", "asv/task-1")


async def test_create_raises_when_the_new_pull_request_cannot_be_found() -> None:
    gh = FakeGh([ok("https://github.com/owner/repo/pull/21\n"), pr_json()])

    with pytest.raises(GitHubError, match="asv/task-1"):
        await GitHub(gh).create_pull_request(
            "owner/repo", head="asv/task-1", base="main", title="t", body="b"
        )


@pytest.mark.parametrize(
    "stdout",
    [
        pytest.param("<html>502 Bad Gateway</html>", id="not-json"),
        pytest.param("[]", id="not-an-object"),
        pytest.param(json.dumps({"state": "OPEN"}), id="missing-field"),
        pytest.param(json.dumps({"state": "DRAFT", "headRefOid": "abc"}), id="unknown-pr-state"),
    ],
)
async def test_unexpected_status_output_is_reported_as_a_github_error(stdout: str) -> None:
    gh = FakeGh([ok(stdout)])

    with pytest.raises(GitHubError, match="gh pr view returned unexpected output"):
        await GitHub(gh).get_pull_request_status("owner/repo", 7)


async def test_unexpected_pull_request_list_output_is_reported_as_a_github_error() -> None:
    gh = FakeGh([ok(json.dumps([{"number": 12}]))])

    with pytest.raises(GitHubError, match="gh pr list returned unexpected output"):
        await GitHub(gh).find_pull_request("owner/repo", "asv/task-1")


async def test_gh_that_cannot_be_started_is_reported_as_a_github_error() -> None:
    async def missing_gh(
        args: Sequence[str], *, cwd: Path | None = None, input_text: str | None = None
    ) -> CommandResult:
        raise FileNotFoundError(2, "No such file or directory", "gh")

    with pytest.raises(GitHubError, match="gh could not be started"):
        await GitHub(missing_gh).get_pull_request_status("owner/repo", 7)


def _option(call: list[str], name: str) -> str:
    return call[call.index(name) + 1]


def check_run(conclusion: str | None, *, status: str = "COMPLETED", name: str = "job") -> dict:  # type: ignore[type-arg]
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion or "",
        "detailsUrl": "https://github.com/o/r/actions/runs/900/job/123",
    }


def status_context(state: str, *, context: str = "external/ci") -> dict:  # type: ignore[type-arg]
    return {"__typename": "StatusContext", "context": context, "state": state, "targetUrl": ""}


def pr_view(*checks: dict, state: str = "OPEN", review: str = "") -> dict:  # type: ignore[type-arg]
    return {
        "state": state,
        "headRefOid": "abc123",
        "reviewDecision": review,
        "statusCheckRollup": list(checks),
    }


PASS, FAIL = check_run("SUCCESS"), check_run("FAILURE")


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        pytest.param([], CiState.NONE, id="no-checks"),
        pytest.param([PASS], CiState.PASSED, id="success"),
        pytest.param([check_run("NEUTRAL")], CiState.PASSED, id="neutral"),
        pytest.param([check_run("SKIPPED")], CiState.PASSED, id="skipped"),
        pytest.param([FAIL], CiState.FAILED, id="failure"),
        pytest.param([check_run("CANCELLED")], CiState.FAILED, id="cancelled"),
        pytest.param([check_run("TIMED_OUT")], CiState.FAILED, id="timed-out"),
        pytest.param([check_run("STARTUP_FAILURE")], CiState.FAILED, id="startup-failure"),
        pytest.param([check_run("ACTION_REQUIRED")], CiState.PENDING, id="action-required"),
        pytest.param([check_run("STALE")], CiState.PENDING, id="stale"),
        pytest.param([check_run("SOMETHING_NEW")], CiState.PENDING, id="unknown-conclusion"),
        pytest.param([check_run(None, status="IN_PROGRESS")], CiState.PENDING, id="in-progress"),
        pytest.param([check_run(None, status="QUEUED")], CiState.PENDING, id="queued"),
        pytest.param([status_context("SUCCESS")], CiState.PASSED, id="status-success"),
        pytest.param([status_context("PENDING")], CiState.PENDING, id="status-pending"),
        pytest.param([status_context("EXPECTED")], CiState.PENDING, id="status-expected"),
        pytest.param([status_context("FAILURE")], CiState.FAILED, id="status-failure"),
        pytest.param([status_context("ERROR")], CiState.FAILED, id="status-error"),
        pytest.param([PASS, check_run("SKIPPED")], CiState.PASSED, id="pass-and-skip"),
        pytest.param([PASS, FAIL], CiState.FAILED, id="pass-and-fail"),
        pytest.param(
            [FAIL, check_run(None, status="IN_PROGRESS")],
            CiState.PENDING,
            id="failed-but-others-still-running",
        ),
        pytest.param(
            [FAIL, status_context("PENDING")], CiState.PENDING, id="failed-status-pending"
        ),
    ],
)
def test_checks_map_to_ci_state(checks: list[dict], expected: CiState) -> None:  # type: ignore[type-arg]
    assert parse_pull_request_status(pr_view(*checks)).ci_state == expected


@pytest.mark.parametrize(
    ("state", "review", "pr_state", "review_state"),
    [
        ("OPEN", "", PrState.OPEN, ReviewState.NONE),
        ("OPEN", "REVIEW_REQUIRED", PrState.OPEN, ReviewState.NONE),
        ("OPEN", "APPROVED", PrState.OPEN, ReviewState.APPROVED),
        ("OPEN", "CHANGES_REQUESTED", PrState.OPEN, ReviewState.CHANGES_REQUESTED),
        ("MERGED", "APPROVED", PrState.MERGED, ReviewState.APPROVED),
        ("CLOSED", "", PrState.CLOSED, ReviewState.NONE),
    ],
)
def test_pull_request_and_review_states(
    state: str, review: str, pr_state: PrState, review_state: ReviewState
) -> None:
    parsed = parse_pull_request_status(pr_view(state=state, review=review))

    assert (parsed.pr_state, parsed.review_state, parsed.head_sha) == (
        pr_state,
        review_state,
        "abc123",
    )


def test_failed_checks_carry_the_job_id_when_they_come_from_actions() -> None:
    parsed = parse_pull_request_status(
        pr_view(
            PASS,
            check_run("FAILURE", name="pytest"),
            check_run("CANCELLED", name="slow"),
            status_context("FAILURE", context="external/lint"),
        )
    )

    assert parsed.failed_checks == (
        FailedCheck("pytest", 123),
        FailedCheck("slow", 123),
        FailedCheck("external/lint", None),
    )


async def test_status_is_fetched_with_one_gh_call() -> None:
    gh = FakeGh([ok(json.dumps(pr_view(FAIL, review="APPROVED")))])

    status = await GitHub(gh).get_pull_request_status("owner/repo", 7)

    assert status.ci_state == CiState.FAILED
    assert gh.calls == [
        [
            "gh",
            "pr",
            "view",
            "7",
            "--repo",
            "owner/repo",
            "--json",
            "state,headRefOid,reviewDecision,statusCheckRollup,comments,reviews,mergeable",
        ]
    ]


async def test_job_log_prefers_the_failed_steps() -> None:
    gh = FakeGh([ok("job\tstep\tboom\n")])

    log = await GitHub(gh).fetch_job_log("owner/repo", 123)

    assert log == "job\tstep\tboom\n"
    assert gh.calls[0][-3:] == ["--job", "123", "--log-failed"]


async def test_job_log_falls_back_to_the_full_log_for_a_cancelled_job() -> None:
    gh = FakeGh([ok(""), ok("job\tstep\tThe operation was canceled.\n")])

    log = await GitHub(gh).fetch_job_log("owner/repo", 123)

    assert "canceled" in log
    assert gh.calls[1][-3:] == ["--job", "123", "--log"]


def test_status_carries_the_ids_of_comments_and_submitted_reviews() -> None:
    data = pr_view(PASS)
    data["comments"] = [{"id": "IC_1"}, {"id": "IC_2"}]
    data["reviews"] = [{"id": "PRR_1", "state": "COMMENTED"}, {"id": "PRR_2", "state": "PENDING"}]

    assert parse_pull_request_status(data).feedback_ids == ("IC_1", "IC_2", "PRR_1")


def rest_user(login: str = "alice", kind: str = "User") -> dict[str, str]:
    return {"login": login, "type": kind}


async def test_feedback_lists_comments_reviews_and_inline_comments_in_time_order() -> None:
    comments = [
        {
            "node_id": "IC_1",
            "user": rest_user(),
            "body": "general",
            "created_at": "2026-11-19T10:00:03Z",
            "html_url": "u1",
        },
        {
            "node_id": "IC_2",
            "user": rest_user("ci[bot]", "Bot"),
            "body": "coverage",
            "created_at": "2026-11-19T10:00:04Z",
            "html_url": "u2",
        },
    ]
    reviews = [
        {
            "id": 11,
            "node_id": "PRR_1",
            "user": rest_user(),
            "body": "",
            "state": "CHANGES_REQUESTED",
            "submitted_at": "2026-11-19T10:00:02Z",
            "html_url": "u3",
        },
        {"id": 12, "node_id": "PRR_2", "user": rest_user(), "body": "draft", "state": "PENDING"},
    ]
    inline = [
        {
            "node_id": "PRRC_1",
            "pull_request_review_id": 11,
            "user": rest_user(),
            "body": "off by one",
            "path": "calc.py",
            "line": None,
            "original_line": 21,
            "created_at": "2026-11-19T10:00:01Z",
            "html_url": "u4",
        },
        {
            "node_id": "PRRC_2",
            "pull_request_review_id": 12,
            "user": rest_user(),
            "body": "not sent yet",
            "path": "calc.py",
            "line": 3,
            "created_at": "2026-11-19T10:00:05Z",
        },
    ]
    # `--slurp` wraps each page as one element of an outer array.
    gh = FakeGh([ok(json.dumps([comments])), ok(json.dumps([reviews])), ok(json.dumps([inline]))])

    items = await GitHub(gh).list_feedback("owner/repo", 7)

    assert [(item.github_id, item.kind) for item in items] == [
        ("PRRC_1", FeedbackKind.INLINE_COMMENT),
        ("PRR_1", FeedbackKind.REVIEW),
        ("IC_1", FeedbackKind.COMMENT),
        ("IC_2", FeedbackKind.COMMENT),
    ]
    assert (items[0].path, items[0].line, items[0].review_state) == ("calc.py", 21, None)
    assert items[1].review_state == "CHANGES_REQUESTED"
    assert [item.is_bot for item in items] == [False, False, False, True]
    assert [call[1:4] for call in gh.calls] == [["api", "--paginate", "--slurp"]] * 3
    assert [call[4] for call in gh.calls] == [
        "repos/owner/repo/issues/7/comments?per_page=100",
        "repos/owner/repo/pulls/7/reviews?per_page=100",
        "repos/owner/repo/pulls/7/comments?per_page=100",
    ]


async def test_feedback_reads_every_page() -> None:
    page = [{"node_id": "IC_1", "user": rest_user(), "body": "a", "created_at": "t1"}]
    more = [{"node_id": "IC_2", "user": rest_user(), "body": "b", "created_at": "t2"}]
    gh = FakeGh([ok(json.dumps([page, more])), ok("[[]]"), ok("[[]]")])

    items = await GitHub(gh).list_feedback("owner/repo", 7)

    assert [item.github_id for item in items] == ["IC_1", "IC_2"]


async def test_unexpected_feedback_output_is_a_github_error() -> None:
    gh = FakeGh([ok("not json")])

    with pytest.raises(GitHubError):
        await GitHub(gh).list_feedback("owner/repo", 7)


@pytest.mark.parametrize(
    ("mergeable", "expected"),
    [
        ("MERGEABLE", "clean"),
        ("CONFLICTING", "conflicting"),
        ("UNKNOWN", "unknown"),
        ("SOMETHING_NEW", "unknown"),
        (None, "unknown"),
    ],
)
def test_mergeable_maps_to_a_merge_state(mergeable: str | None, expected: str) -> None:
    data = pr_view(PASS)
    data["mergeable"] = mergeable

    assert parse_pull_request_status(data).merge_state == expected
