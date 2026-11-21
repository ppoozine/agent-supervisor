"""Whether an external tool (an agent, or gh) can be used: the shared result type of the checks.

A check only looks at what is installed and which credentials are configured. It never spends any
usage, never handles a credential itself, and never returns a tool's raw output, which can contain
part of a key or token.
"""

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from agent_supervisor.process import CommandResult, CommandTimeoutError

CHECK_TIMEOUT_SECONDS = 15.0
VERSION = re.compile(r"\d+\.\d+\.\d+\S*")

CommandRunner = Callable[..., Awaitable[CommandResult]]


class AuthKind(StrEnum):
    """How a tool is signed in."""

    API_KEY = "api_key"
    """A key or token billed by usage (including gateways)."""
    SUBSCRIPTION = "subscription"
    """A personal plan's allowance, e.g. claude.ai or ChatGPT."""
    CLOUD_PROVIDER = "cloud_provider"
    """A cloud provider's credentials, e.g. Amazon Bedrock or Google Vertex AI."""
    ACCOUNT = "account"
    """Signed in to an account of a tool that is not an agent, e.g. gh."""
    OTHER = "other"
    """Signed in some way this tool does not recognize."""
    NONE = "none"


@dataclass(frozen=True)
class ToolStatus:
    """The result of checking one tool.

    Signed in means credentials are configured; whether they are valid shows up only when the tool
    is used.
    """

    installed: bool
    version: str | None = None
    auth: AuthKind = AuthKind.NONE
    auth_label: str | None = None
    """How it is signed in, for people, e.g. "claude.ai (max)"; never a credential."""
    detail: str | None = None
    """Why it cannot be used, when it cannot."""
    fix: tuple[str, ...] = ()
    """Commands to run in a terminal to make it usable."""
    notice: str | None = None
    """Something the user should know even though it can be used."""

    @property
    def usable(self) -> bool:
        """Whether it is installed and signed in."""
        return self.installed and self.auth is not AuthKind.NONE


class ToolMissingError(RuntimeError):
    """The tool's executable could not be run."""


async def run_check(run: CommandRunner, args: Sequence[str]) -> CommandResult:
    """Run one check command. Raises `ToolMissingError` or `CommandTimeoutError`."""
    try:
        return await run(args, timeout=CHECK_TIMEOUT_SECONDS)
    except (FileNotFoundError, PermissionError) as error:
        raise ToolMissingError(f"{args[0]} cannot be run") from error


def parse_version(output: str) -> str | None:
    """The first version number in a `--version` output."""
    match = VERSION.search(output)
    return None if match is None else match.group()


async def read_version(
    run: CommandRunner, executable: Sequence[str], *, fix: tuple[str, ...]
) -> tuple[str | None, ToolStatus | None]:
    """Run `--version`; return the version, or the status to report if the tool is not usable."""
    try:
        result = await run_check(run, [*executable, "--version"])
    except ToolMissingError:
        return None, ToolStatus(installed=False, detail="not installed", fix=fix)
    except CommandTimeoutError:
        return None, ToolStatus(installed=False, detail="`--version` did not answer in time")
    if result.returncode != 0:
        return None, ToolStatus(installed=False, detail="`--version` failed", fix=fix)
    return parse_version(result.stdout), None
