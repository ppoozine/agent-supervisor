"""對 GitHub 的操作。所有 `gh` 指令都集中在這個模組。"""

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agent_supervisor.process import CommandResult, run_command

PR_FIELDS = "number,state,headRefOid,url"
OPEN = "open"

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


class GitHub:
    """透過 `gh` 查詢與建立 PR。"""

    def __init__(self, run: CommandRunner = run_command) -> None:
        self._run = run

    async def find_pull_request(self, repo: str, branch: str) -> PullRequest | None:
        """找出某個 branch 的 PR：有開著的就回傳它，否則回傳最新的一個。"""
        output = await self._gh(
            ["pr", "list", "--repo", repo, "--head", branch, "--state", "all", "--json", PR_FIELDS]
        )
        pull_requests = [_to_pull_request(item) for item in json.loads(output)]
        if not pull_requests:
            return None
        open_ones = [item for item in pull_requests if item.state == OPEN]
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

    async def _gh(self, args: Sequence[str], *, input_text: str | None = None) -> str:
        result = await self._run(["gh", *args], cwd=None, input_text=input_text)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise GitHubError(f"gh {args[0]} {args[1]} failed: {detail}")
        return result.stdout


def _to_pull_request(item: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=int(item["number"]),
        state=str(item["state"]).lower(),
        head_sha=str(item["headRefOid"]),
        url=str(item["url"]),
    )
