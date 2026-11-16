"""github.py 的測試。`gh` 的呼叫用替身，不打 GitHub。"""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_supervisor.github import GitHub, GitHubError
from agent_supervisor.process import CommandResult


class FakeGh:
    """記錄收到的參數，依序回傳預先準備的結果。"""

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


def _option(call: list[str], name: str) -> str:
    return call[call.index(name) + 1]
