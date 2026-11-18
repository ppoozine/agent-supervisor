"""對 GitHub 的操作。所有 `gh` 指令都集中在這個模組。"""

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agent_supervisor.enums import CiState, PrState, ReviewState
from agent_supervisor.process import CommandResult, run_command

PR_FIELDS = "number,state,headRefOid,url"
STATUS_FIELDS = "state,headRefOid,reviewDecision,statusCheckRollup"

# 檢查結果的對應，依據見 docs/02-mvp-spec.md 的「ci_state 的意思」。
# 不在這兩組裡的值（還在跑、等人核准、過期、沒看過的新值）一律當成還沒結束。
_PASSED_RESULTS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_FAILED_RESULTS = frozenset({"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "CANCELLED", "ERROR"})
_REVIEW_STATES = {
    "APPROVED": ReviewState.APPROVED,
    "CHANGES_REQUESTED": ReviewState.CHANGES_REQUESTED,
}
_ACTIONS_JOB_URL = re.compile(r"/actions/runs/\d+/job/(\d+)")
# 解析 `gh` 的輸出時，內容不是預期的 JSON 形狀會遇到的例外（JSON 格式錯誤屬於 ValueError）。
_UNEXPECTED_OUTPUT_ERRORS = (AttributeError, KeyError, TypeError, ValueError)

CommandRunner = Callable[..., Awaitable[CommandResult]]


class GitHubError(RuntimeError):
    """gh 指令失敗，或回傳的內容不如預期。"""


@dataclass(frozen=True)
class PullRequest:
    """一個 PR 的基本狀態。`state` 是 open、merged 或 closed。"""

    number: int
    state: str
    head_sha: str
    url: str


@dataclass(frozen=True)
class FailedCheck:
    """一個失敗的檢查。`job_id` 只有 GitHub Actions 的檢查才有，抓 log 要用它。"""

    name: str
    job_id: int | None


@dataclass(frozen=True)
class PullRequestStatus:
    """Poller 一次查回來的整組狀態。"""

    pr_state: PrState
    head_sha: str
    ci_state: CiState
    review_state: ReviewState
    failed_checks: tuple[FailedCheck, ...]


class GitHub:
    """透過 `gh` 查詢與建立 PR、查詢 CI 與 review 狀態、抓 CI log。"""

    def __init__(self, run: CommandRunner = run_command) -> None:
        self._run = run

    async def find_pull_request(self, repo: str, branch: str) -> PullRequest | None:
        """找出某個 branch 的 PR：有開著的就回傳它，否則回傳最新的一個。"""
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
        """開一個 PR 並回傳它。"""
        arguments = ["pr", "create", "--repo", repo, "--head", head, "--base", base]
        arguments += ["--title", title, "--body-file", "-"]
        await self._gh(arguments, input_text=body)
        created = await self.find_pull_request(repo, head)
        if created is None:
            raise GitHubError(f"pull request for {head} was created but cannot be found")
        return created

    async def get_pull_request_status(self, repo: str, number: int) -> PullRequestStatus:
        """一次查回 PR 狀態、最新 commit、CI 結果與 review 結果。"""
        output = await self._gh(
            ["pr", "view", str(number), "--repo", repo, "--json", STATUS_FIELDS]
        )
        try:
            return parse_pull_request_status(json.loads(output))
        except _UNEXPECTED_OUTPUT_ERRORS as error:
            raise GitHubError(f"gh pr view returned unexpected output: {error!r}") from error

    async def fetch_job_log(self, repo: str, job_id: int) -> str:
        """抓一個 GitHub Actions job 的 log：優先只要失敗的步驟，沒有就抓整份。

        被取消或逾時的 job 沒有「失敗的步驟」，所以要退回整份 log。
        """
        base = ["run", "view", "--repo", repo, "--job", str(job_id)]
        failed_steps = await self._gh([*base, "--log-failed"])
        if failed_steps.strip():
            return failed_steps
        return await self._gh([*base, "--log"])

    async def _gh(self, args: Sequence[str], *, input_text: str | None = None) -> str:
        try:
            result = await self._run(["gh", *args], cwd=None, input_text=input_text)
        except OSError as error:
            # 例如 `gh` 沒有安裝或不在 PATH 裡。
            raise GitHubError(f"gh could not be started: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise GitHubError(f"gh {args[0]} {args[1]} failed: {detail}")
        return result.stdout


def parse_pull_request_status(data: dict[str, Any]) -> PullRequestStatus:
    """把 `gh pr view --json` 的結果換算成我們的狀態值。"""
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
    )


def _check_result(check: dict[str, Any]) -> CiState:
    """單一檢查的結果。GitHub Actions 的檢查與外部服務回報的狀態形狀不同。"""
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
    """有檢查還沒結束就是 pending，即使已經有別的檢查失敗：等全部結束才一次處理。"""
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
