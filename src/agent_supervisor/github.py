"""GitHub operations. All `gh` commands live in this module."""

import json
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agent_supervisor.enums import CiState, MergeState, PrState, ReviewState
from agent_supervisor.process import CommandResult, CommandTimeoutError, run_command
from agent_supervisor.tool_status import (
    AuthKind,
    ToolMissingError,
    ToolStatus,
    read_version,
    run_check,
)

PR_FIELDS = "number,state,headRefOid,url"
HOST = "github.com"
INSTALL_COMMAND = (
    "brew install gh"
    if sys.platform == "darwin"
    else "# install gh: https://github.com/cli/cli#installation"
)
UPGRADE_COMMAND = (
    "brew upgrade gh"
    if sys.platform == "darwin"
    else "# update gh: https://github.com/cli/cli#installation"
)
SIGN_IN_COMMAND = "gh auth login"
STATUS_FIELDS = "state,headRefOid,reviewDecision,statusCheckRollup,comments,reviews,mergeable"

# Mapping of check results; for the rationale see the section on what ci_state means in
# docs/design/spec.md.
# Values in neither set (still running, awaiting approval, stale, new values never seen before) are
# treated as not finished.
_PASSED_RESULTS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_FAILED_RESULTS = frozenset({"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "CANCELLED", "ERROR"})
_REVIEW_STATES = {
    "APPROVED": ReviewState.APPROVED,
    "CHANGES_REQUESTED": ReviewState.CHANGES_REQUESTED,
}
# GitHub returns UNKNOWN until it has computed it; unseen values are also treated as unknown, not as
# conflicting or not conflicting.
_MERGE_STATES = {"MERGEABLE": MergeState.CLEAN, "CONFLICTING": MergeState.CONFLICTING}
_ACTIONS_JOB_URL = re.compile(r"/actions/runs/\d+/job/(\d+)")
# Exceptions raised when `gh` output parses but isn't the expected JSON shape (malformed JSON is a
# ValueError).
_UNEXPECTED_OUTPUT_ERRORS = (AttributeError, KeyError, TypeError, ValueError)

CommandRunner = Callable[..., Awaitable[CommandResult]]


class GitHubError(RuntimeError):
    """A gh command failed, or returned unexpected content."""


@dataclass(frozen=True)
class PullRequest:
    """Basic state of a PR. `state` is open, merged, or closed."""

    number: int
    state: str
    head_sha: str
    url: str


@dataclass(frozen=True)
class FailedCheck:
    """A failed check. Only GitHub Actions checks have `job_id`, which is needed to fetch logs."""

    name: str
    job_id: int | None


@dataclass(frozen=True)
class PullRequestStatus:
    """The full set of state the Poller fetches in one query."""

    pr_state: PrState
    head_sha: str
    ci_state: CiState
    review_state: ReviewState
    failed_checks: tuple[FailedCheck, ...]
    feedback_ids: tuple[str, ...] = ()
    """IDs of the PR's regular comments and submitted reviews, to detect new comments to fetch."""
    merge_state: MergeState = MergeState.UNKNOWN


class FeedbackKind(StrEnum):
    """The three forms of PR comments."""

    COMMENT = "comment"
    REVIEW = "review"
    INLINE_COMMENT = "inline_comment"


@dataclass(frozen=True)
class FeedbackItem:
    """One PR comment. `github_id` is GitHub's node id; all three kinds share one ID space.

    `path` and `line` exist only on inline comments; `review_state` only on reviews.
    `author_association` is GitHub's relation of the author to the repo, such as `OWNER` or `NONE`.
    """

    github_id: str
    kind: FeedbackKind
    author: str
    is_bot: bool
    author_association: str
    body: str
    created_at: str
    url: str
    path: str | None = None
    line: int | None = None
    review_state: str | None = None


class GitHub:
    """Query and create PRs, query CI and review state, and fetch CI logs through `gh`."""

    def __init__(self, run: CommandRunner = run_command) -> None:
        self._run = run

    async def find_pull_request(self, repo: str, branch: str) -> PullRequest | None:
        """Find a branch's PR: the open one if there is one, otherwise the most recent one."""
        output = await self._gh(
            ["pr", "list", "--repo", repo, "--head", branch, "--state", "all", "--json", PR_FIELDS]
        )
        try:
            pull_requests = [_to_pull_request(item) for item in json.loads(output)]
        except _UNEXPECTED_OUTPUT_ERRORS as error:
            raise GitHubError(f"gh pr list returned unexpected output: {error!r}") from error
        if not pull_requests:
            return None
        open_ones = [item for item in pull_requests if item.state == PrState.OPEN]
        return max(open_ones or pull_requests, key=lambda item: item.number)

    async def create_pull_request(
        self, repo: str, *, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        """Open a PR and return it."""
        arguments = ["pr", "create", "--repo", repo, "--head", head, "--base", base]
        arguments += ["--title", title, "--body-file", "-"]
        await self._gh(arguments, input_text=body)
        created = await self.find_pull_request(repo, head)
        if created is None:
            raise GitHubError(f"pull request for {head} was created but cannot be found")
        return created

    async def get_pull_request_status(self, repo: str, number: int) -> PullRequestStatus:
        """Fetch the PR state, latest commit, CI result, and review result in one go."""
        output = await self._gh(
            ["pr", "view", str(number), "--repo", repo, "--json", STATUS_FIELDS]
        )
        try:
            return parse_pull_request_status(json.loads(output))
        except _UNEXPECTED_OUTPUT_ERRORS as error:
            raise GitHubError(f"gh pr view returned unexpected output: {error!r}") from error

    async def list_feedback(self, repo: str, number: int) -> list[FeedbackItem]:
        """List all comments on the PR in time order: regular comments, submitted reviews, inline.

        Uses the REST API rather than `gh pr view`: the latter cannot tell whether the author is a
        bot, and has no inline comments.
        Unsubmitted reviews (visible only to their author) and their inline comments don't count.
        """
        comments = await self._list(f"repos/{repo}/issues/{number}/comments")
        reviews = await self._list(f"repos/{repo}/pulls/{number}/reviews")
        inline_comments = await self._list(f"repos/{repo}/pulls/{number}/comments")
        try:
            pending_reviews = {item["id"] for item in reviews if item["state"] == "PENDING"}
            items = [_to_feedback(item, FeedbackKind.COMMENT) for item in comments]
            items += [
                _to_feedback(item, FeedbackKind.REVIEW)
                for item in reviews
                if item["id"] not in pending_reviews
            ]
            items += [
                _to_feedback(item, FeedbackKind.INLINE_COMMENT)
                for item in inline_comments
                if item.get("pull_request_review_id") not in pending_reviews
            ]
        except _UNEXPECTED_OUTPUT_ERRORS as error:
            raise GitHubError(f"gh api returned unexpected feedback: {error!r}") from error
        return sorted(items, key=lambda item: item.created_at)

    async def _list(self, path: str) -> list[dict[str, Any]]:
        """Read a paginated REST list in full. `--slurp` wraps each page in an outer array."""
        output = await self._gh(["api", "--paginate", "--slurp", f"{path}?per_page=100"])
        try:
            return [item for page in json.loads(output) for item in page]
        except _UNEXPECTED_OUTPUT_ERRORS as error:
            raise GitHubError(f"gh api {path} returned unexpected output: {error!r}") from error

    async def fetch_job_log(self, repo: str, job_id: int) -> str:
        """Fetch a GitHub Actions job's log: only the failed steps if possible, else the whole log.

        Cancelled or timed-out jobs have no "failed steps", so fall back to the whole log.
        """
        base = ["run", "view", "--repo", repo, "--job", str(job_id)]
        failed_steps = await self._gh([*base, "--log-failed"])
        if failed_steps.strip():
            return failed_steps
        return await self._gh([*base, "--log"])

    async def check_cli(self) -> ToolStatus:
        """Check that `gh` is installed and signed in to github.com.

        `gh auth status` asks GitHub whether the saved token still works, which is free. Its token
        is never asked for (`--show-token`).
        """
        version, problem = await read_version(self._run, ["gh"], fix=(INSTALL_COMMAND,))
        if problem is not None:
            return problem
        arguments = ["gh", "auth", "status", "--active", "--hostname", HOST, "--json", "hosts"]
        try:
            result = await run_check(self._run, arguments)
            hosts = json.loads(result.stdout)["hosts"]
        except (ToolMissingError, CommandTimeoutError, json.JSONDecodeError, KeyError, TypeError):
            return ToolStatus(
                installed=True,
                version=version,
                detail="could not read `gh auth status`; gh may be too old",
                fix=(UPGRADE_COMMAND,),
            )
        return gh_auth_status(hosts, version)

    async def _gh(self, args: Sequence[str], *, input_text: str | None = None) -> str:
        try:
            result = await self._run(["gh", *args], cwd=None, input_text=input_text)
        except OSError as error:
            # E.g. `gh` is not installed or not on PATH.
            raise GitHubError(f"gh could not be started: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise GitHubError(f"gh {args[0]} {args[1]} failed: {detail}")
        return result.stdout


def gh_auth_status(hosts: Any, version: str | None) -> ToolStatus:
    """Map the `hosts` of `gh auth status --json hosts` to a status, leaving out the account."""
    accounts = hosts.get(HOST) if isinstance(hosts, dict) else None
    active = next((item for item in accounts or [] if item.get("active")), None)
    if active is None:
        return ToolStatus(
            installed=True, version=version, detail="not signed in", fix=(SIGN_IN_COMMAND,)
        )
    if active.get("state") != "success":
        return ToolStatus(
            installed=True,
            version=version,
            detail="GitHub did not accept the saved credentials",
            fix=(SIGN_IN_COMMAND,),
        )
    return ToolStatus(
        installed=True,
        version=version,
        auth=AuthKind.ACCOUNT,
        auth_label=str(active.get("tokenSource") or HOST),
    )


def parse_pull_request_status(data: dict[str, Any]) -> PullRequestStatus:
    """Convert the result of `gh pr view --json` into our state values."""
    checks = data.get("statusCheckRollup") or []
    results = [_check_result(check) for check in checks]
    failed = tuple(
        FailedCheck(name=_check_name(check), job_id=_job_id(check))
        for check, result in zip(checks, results, strict=True)
        if result is CiState.FAILED
    )
    return PullRequestStatus(
        pr_state=PrState(str(data["state"]).lower()),
        head_sha=str(data["headRefOid"]),
        ci_state=_combine(results),
        review_state=_REVIEW_STATES.get(data.get("reviewDecision") or "", ReviewState.NONE),
        failed_checks=failed,
        feedback_ids=_feedback_ids(data),
        merge_state=_MERGE_STATES.get(str(data.get("mergeable") or ""), MergeState.UNKNOWN),
    )


def _feedback_ids(data: dict[str, Any]) -> tuple[str, ...]:
    comments = [str(item["id"]) for item in data.get("comments") or []]
    reviews = [
        str(item["id"]) for item in data.get("reviews") or [] if item.get("state") != "PENDING"
    ]
    return tuple(comments + reviews)


def _to_feedback(item: dict[str, Any], kind: FeedbackKind) -> FeedbackItem:
    user = item.get("user") or {}
    line = item.get("line") or item.get("original_line")
    return FeedbackItem(
        github_id=str(item["node_id"]),
        kind=kind,
        author=str(user.get("login") or "unknown"),
        is_bot=user.get("type") == "Bot",
        author_association=str(item.get("author_association") or "NONE"),
        body=str(item.get("body") or ""),
        created_at=str(item.get("created_at") or item.get("submitted_at") or ""),
        url=str(item.get("html_url") or ""),
        path=item.get("path"),
        line=None if line is None else int(line),
        review_state=item.get("state") if kind is FeedbackKind.REVIEW else None,
    )


def _check_result(check: dict[str, Any]) -> CiState:
    """The result of one check. GitHub Actions checks and external statuses differ in shape."""
    if check.get("__typename") == "StatusContext":
        value = check.get("state")
    elif check.get("status") == "COMPLETED":
        value = check.get("conclusion")
    else:
        return CiState.PENDING
    if value in _PASSED_RESULTS:
        return CiState.PASSED
    if value in _FAILED_RESULTS:
        return CiState.FAILED
    return CiState.PENDING


def _combine(results: Sequence[CiState]) -> CiState:
    """Any unfinished check means pending, even if another failed: handle them all at once."""
    if not results:
        return CiState.NONE
    if CiState.PENDING in results:
        return CiState.PENDING
    return CiState.FAILED if CiState.FAILED in results else CiState.PASSED


def _check_name(check: dict[str, Any]) -> str:
    return str(check.get("name") or check.get("context") or "unknown")


def _job_id(check: dict[str, Any]) -> int | None:
    match = _ACTIONS_JOB_URL.search(str(check.get("detailsUrl") or ""))
    return None if match is None else int(match.group(1))


def _to_pull_request(item: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=int(item["number"]),
        state=str(item["state"]).lower(),
        head_sha=str(item["headRefOid"]),
        url=str(item["url"]),
    )
