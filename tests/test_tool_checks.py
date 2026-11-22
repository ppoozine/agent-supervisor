"""Checks of whether each agent and gh can be used. Commands are faked; nothing is spent."""

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_supervisor import daemon, db
from agent_supervisor.adapters.claude_code import (
    SUBSCRIPTION_NOTICE,
    ClaudeCodeAdapter,
    find_sdk_cli,
)
from agent_supervisor.adapters.codex import CodexAdapter
from agent_supervisor.config import Config
from agent_supervisor.github import GitHub
from agent_supervisor.process import CommandResult, CommandTimeoutError
from agent_supervisor.readiness import ReadinessChecker
from agent_supervisor.supervisor import build_supervisor
from agent_supervisor.tool_status import AuthKind, ToolStatus
from tests.fake_adapter import FakeAdapter
from tests.test_stages import FakeGitHub

Answer = CommandResult | type[Exception]


class FakeCommands:
    """Answer commands by their arguments after the executable; record what was run."""

    def __init__(self, answers: dict[tuple[str, ...], Answer]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, args: Sequence[str], **options: Any) -> CommandResult:
        key = tuple(args[1:])
        self.calls.append(key)
        answer = self.answers.get(key, FileNotFoundError)
        if isinstance(answer, type):
            raise answer(f"{args[0]}: faked failure")
        return answer


def ok(stdout: str = "", stderr: str = "", code: int = 0) -> CommandResult:
    return CommandResult(code, stdout, stderr)


def claude(status: dict[str, Any], *, code: int = 0, **environ: str) -> ClaudeCodeAdapter:
    commands = FakeCommands(
        {
            ("--version",): ok("2.1.325 (Claude Code)\n"),
            ("auth", "status", "--json"): ok(json.dumps(status), code=code),
        }
    )
    return ClaudeCodeAdapter(command_runner=commands, cli_path=Path("/sdk/claude"), environ=environ)


SIGNED_IN_WITH_PLAN = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "subscriptionType": "max",
    "email": "someone@example.com",
    "orgId": "org-secret-id",
    "orgName": "someone@example.com's Organization",
}


async def test_claude_signed_in_with_a_subscription_is_usable_with_the_terms_notice() -> None:
    status = await claude(SIGNED_IN_WITH_PLAN).check()

    assert status.usable
    assert (status.version, status.auth, status.auth_label) == (
        "2.1.325",
        AuthKind.SUBSCRIPTION,
        "claude.ai (max)",
    )
    assert status.notice == SUBSCRIPTION_NOTICE
    assert "someone@example.com" not in repr(status)
    assert "org-secret-id" not in repr(status)


@pytest.mark.parametrize(
    ("data", "environ", "expected"),
    [
        (
            {"loggedIn": True, "authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"},
            {},
            (AuthKind.API_KEY, "ANTHROPIC_API_KEY"),
        ),
        (
            {"loggedIn": True, "authMethod": "oauth_token"},
            {"ANTHROPIC_AUTH_TOKEN": "x"},
            (AuthKind.API_KEY, "ANTHROPIC_AUTH_TOKEN"),
        ),
        (
            {"loggedIn": True, "authMethod": "oauth_token"},
            {"CLAUDE_CODE_OAUTH_TOKEN": "x"},
            (AuthKind.SUBSCRIPTION, "CLAUDE_CODE_OAUTH_TOKEN"),
        ),
        (
            {"loggedIn": True, "authMethod": "third_party", "apiProvider": "bedrock"},
            {},
            (AuthKind.CLOUD_PROVIDER, "bedrock"),
        ),
        (
            {"loggedIn": True, "authMethod": "something_new"},
            {},
            (AuthKind.OTHER, "something_new"),
        ),
    ],
    ids=["api-key", "gateway-token", "setup-token", "cloud-provider", "unknown"],
)
async def test_claude_sign_in_methods(
    data: dict[str, Any], environ: dict[str, str], expected: tuple[AuthKind, str]
) -> None:
    status = await claude(data, **environ).check()

    assert status.usable
    assert (status.auth, status.auth_label) == expected
    assert (status.notice is not None) == (expected[0] is AuthKind.SUBSCRIPTION)


async def test_claude_signed_out_points_to_an_api_key_first() -> None:
    # Signed out, `claude auth status` exits with 1 but still prints its JSON.
    status = await claude({"loggedIn": False, "authMethod": "none"}, code=1).check()

    assert not status.usable
    assert (status.installed, status.auth, status.detail) == (True, AuthKind.NONE, "not signed in")
    assert status.fix[0].startswith("export ANTHROPIC_API_KEY=")
    assert status.notice is None


async def test_claude_cli_that_cannot_run_is_reported_as_not_installed() -> None:
    adapter = ClaudeCodeAdapter(command_runner=FakeCommands({}), cli_path=Path("/missing/claude"))

    status = await adapter.check()

    assert (status.usable, status.installed, status.detail) == (False, False, "not installed")
    assert status.fix


async def test_claude_unreadable_status_is_reported_without_its_output() -> None:
    commands = FakeCommands(
        {
            ("--version",): ok("2.1.325 (Claude Code)"),
            ("auth", "status", "--json"): ok("not json sk-ant-secret"),
        }
    )
    adapter = ClaudeCodeAdapter(command_runner=commands, cli_path=Path("/sdk/claude"))

    status = await adapter.check()

    assert not status.usable
    assert status.detail == "could not read `claude auth status`"
    assert "sk-ant" not in repr(status)


def test_the_cli_checked_is_the_one_bundled_with_the_sdk() -> None:
    cli = find_sdk_cli()

    assert cli is not None
    assert cli.parent.name == "_bundled"


def codex(login: Answer, **environ: str) -> tuple[CodexAdapter, FakeCommands]:
    commands = FakeCommands({("--version",): ok("codex-cli 0.167.0\n"), ("login", "status"): login})
    return CodexAdapter(command_runner=commands, environ=environ), commands


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Logged in using ChatGPT\n", (AuthKind.SUBSCRIPTION, "ChatGPT")),
        ("API key configured - sk-abcd***wxyz\n", (AuthKind.API_KEY, "saved API key")),
    ],
    ids=["chatgpt", "saved-api-key"],
)
async def test_codex_saved_login(output: str, expected: tuple[AuthKind, str]) -> None:
    # `codex login status` writes to stderr.
    adapter, _ = codex(ok(stderr=output))

    status = await adapter.check()

    assert status.usable
    assert (status.version, status.auth, status.auth_label) == ("0.167.0", *expected)
    assert "sk-" not in repr(status)


async def test_codex_api_key_in_the_environment_counts_without_a_saved_login() -> None:
    adapter, commands = codex(ok(stderr="Not logged in\n", code=1), CODEX_API_KEY="sk-secret")

    status = await adapter.check()

    assert (status.usable, status.auth, status.auth_label) == (
        True,
        AuthKind.API_KEY,
        "CODEX_API_KEY",
    )
    assert "sk-secret" not in repr(status)
    assert ("login", "status") not in commands.calls


async def test_codex_signed_out() -> None:
    adapter, _ = codex(ok(stderr="Not logged in\n", code=1))

    status = await adapter.check()

    assert (status.usable, status.detail) == (False, "not signed in")
    assert status.fix[0] == "codex login"


async def test_codex_not_on_path_is_not_installed() -> None:
    adapter = CodexAdapter(command_runner=FakeCommands({}), environ={})

    status = await adapter.check()

    assert (status.usable, status.installed) == (False, False)
    assert status.fix == ("npm install -g @openai/codex",)


async def test_codex_check_that_hangs_is_reported() -> None:
    adapter, _ = codex(CommandTimeoutError)

    status = await adapter.check()

    assert (status.usable, status.detail) == (False, "could not read `codex login status`")


GH_STATUS = ("auth", "status", "--active", "--hostname", "github.com", "--json", "hosts")


def gh(answer: Answer) -> GitHub:
    commands = FakeCommands(
        {("--version",): ok("gh version 2.101.0 (2026-09-15)\n"), GH_STATUS: answer}
    )
    return GitHub(commands)


def hosts(*accounts: dict[str, Any]) -> CommandResult:
    return ok(json.dumps({"hosts": {"github.com": list(accounts)} if accounts else {}}))


async def test_gh_signed_in() -> None:
    account = {"state": "success", "active": True, "login": "someone", "tokenSource": "keyring"}

    status = await gh(hosts(account)).check_cli()

    assert (status.usable, status.version, status.auth, status.auth_label) == (
        True,
        "2.101.0",
        AuthKind.ACCOUNT,
        "keyring",
    )
    assert "someone" not in repr(status)


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (hosts(), "not signed in"),
        (
            hosts({"state": "error", "active": True, "error": "401 Bad credentials"}),
            "GitHub did not accept the saved credentials",
        ),
        (ok("unknown flag: --json", code=1), "could not read `gh auth status`; gh may be too old"),
    ],
    ids=["signed-out", "rejected-token", "old-gh"],
)
async def test_gh_not_usable(answer: CommandResult, detail: str) -> None:
    status = await gh(answer).check_cli()

    assert (status.usable, status.installed, status.detail) == (False, True, detail)
    assert status.fix


async def test_gh_not_installed() -> None:
    status = await GitHub(FakeCommands({})).check_cli()

    assert (status.usable, status.installed) == (False, False)


@pytest.fixture
def log_checks(tmp_path: Path) -> Callable[..., Any]:
    async def run(*statuses: ToolStatus) -> None:
        conn = await db.connect(tmp_path / "test.db")
        await db.init_schema(conn)
        adapters = {f"agent-{index}": FakeAdapter([], status=s) for index, s in enumerate(statuses)}
        supervisor = build_supervisor(conn, Config(data_dir=tmp_path), adapters, FakeGitHub())  # type: ignore[arg-type]
        await daemon._log_readiness(supervisor)
        await conn.close()

    return run


async def test_startup_logs_each_tool_without_credentials(
    log_checks: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agent_supervisor.daemon")
    usable = ToolStatus(
        installed=True, version="1.2.3", auth=AuthKind.API_KEY, auth_label="ANTHROPIC_API_KEY"
    )
    missing = ToolStatus(installed=False, detail="not installed", fix=("npm install -g x",))

    await log_checks(usable, missing)

    lines = [(record.levelno, record.getMessage()) for record in caplog.records]
    assert (logging.INFO, "Fake (agent-0): usable 1.2.3, signed in with ANTHROPIC_API_KEY") in lines
    assert (
        logging.WARNING,
        "Fake (agent-1): not usable: not installed; run: npm install -g x",
    ) in lines
    assert (logging.INFO, "gh: usable 2.0.0, signed in with keyring") in lines
    assert not any("no agent can be used" in message for _, message in lines)


async def test_startup_warns_when_no_agent_can_be_used(
    log_checks: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agent_supervisor.daemon")

    await log_checks(ToolStatus(installed=False, detail="not installed"))

    assert "no agent can be used; see http://127.0.0.1:8765/#/settings" in caplog.text


class RaisingAdapter(FakeAdapter):
    async def check(self) -> ToolStatus:
        raise ValueError("secret-looking output sk-123")


class HangingAdapter(FakeAdapter):
    async def check(self) -> ToolStatus:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_a_check_that_raises_or_hangs_only_makes_that_agent_unusable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="agent_supervisor.readiness")
    adapters = {"fine": FakeAdapter([]), "raises": RaisingAdapter([]), "hangs": HangingAdapter([])}
    checker = ReadinessChecker(adapters, FakeGitHub(), timeout_seconds=0.1)  # type: ignore[arg-type]

    readiness = await checker.check()

    assert readiness.usable_agents == ["fine"]
    raised = readiness.agents["raises"]
    assert raised.detail == "the check failed (ValueError); see the daemon's log"
    assert "sk-123" not in str(raised)
    assert readiness.agents["hangs"].detail == "the check did not finish in 0.1 seconds"
    assert readiness.github.usable
    assert "checking raises failed" in caplog.text
    assert "checking hangs did not finish" in caplog.text


async def test_the_daemon_still_starts_when_an_agent_check_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agent_supervisor.daemon")
    conn = await db.connect(tmp_path / "test.db")
    await db.init_schema(conn)
    adapters = {"fine": FakeAdapter([]), "raises": RaisingAdapter([])}
    supervisor = build_supervisor(conn, Config(data_dir=tmp_path), adapters, FakeGitHub())  # type: ignore[arg-type]

    try:
        await daemon._log_readiness(supervisor)
    finally:
        await conn.close()

    assert "Fake (raises): not usable: the check failed (ValueError)" in caplog.text
