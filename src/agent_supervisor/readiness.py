"""Check which agents and whether gh can be used, and keep the latest result.

The daemon checks once at startup and logs the result. After that the API answers from the kept
result, since checking gh asks GitHub over the network; the settings page can ask for a fresh check
after the user fixes something in a terminal.
"""

import asyncio
import logging
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Protocol

from agent_supervisor import db
from agent_supervisor.adapters.base import AgentAdapter
from agent_supervisor.tool_status import ToolStatus

# A check may run a few commands, each with its own timeout; this bounds the whole check, so one
# that hangs cannot hold up the daemon's startup.
CHECK_TIMEOUT_SECONDS = 30.0
logger = logging.getLogger("agent_supervisor.readiness")


class GitHubCli(Protocol):
    """The part of `GitHub` the check needs."""

    async def check_cli(self) -> ToolStatus:
        """Check that gh is installed and signed in."""
        ...


@dataclass(frozen=True)
class Readiness:
    """The result of checking every agent and gh at one moment."""

    agents: Mapping[str, ToolStatus]
    github: ToolStatus
    checked_at: str

    @property
    def usable_agents(self) -> list[str]:
        """Agent types that can be used."""
        return [agent_type for agent_type, status in self.agents.items() if status.usable]


class ReadinessChecker:
    """Run the checks and keep the latest result."""

    def __init__(
        self,
        adapters: Mapping[str, AgentAdapter],
        github: GitHubCli,
        *,
        timeout_seconds: float = CHECK_TIMEOUT_SECONDS,
    ) -> None:
        self._adapters = adapters
        self._github = github
        self._timeout_seconds = timeout_seconds
        self._latest: Readiness | None = None
        self._lock = asyncio.Lock()

    async def latest(self) -> Readiness:
        """The kept result; check first if there is none yet."""
        if self._latest is not None:
            return self._latest
        async with self._lock:
            if self._latest is None:
                self._latest = await self._check()
            return self._latest

    async def check(self) -> Readiness:
        """Check everything again and keep the result."""
        async with self._lock:
            self._latest = await self._check()
            return self._latest

    async def _check(self) -> Readiness:
        agent_types = list(self._adapters)
        *agents, github = await asyncio.gather(
            *(
                self._guarded(agent_type, self._adapters[agent_type].check())
                for agent_type in agent_types
            ),
            self._guarded("gh", self._github.check_cli()),
        )
        return Readiness(dict(zip(agent_types, agents, strict=True)), github, db.utc_now())

    async def _guarded(self, name: str, check: Awaitable[ToolStatus]) -> ToolStatus:
        """Run one check; one that fails or hangs makes that tool unusable, not the daemon.

        Adapters can come from other packages, so their checks are not trusted to behave.
        """
        try:
            return await asyncio.wait_for(check, self._timeout_seconds)
        except TimeoutError:
            logger.error("checking %s did not finish in %s seconds", name, self._timeout_seconds)
            return ToolStatus(
                installed=False,
                detail=f"the check did not finish in {self._timeout_seconds:g} seconds",
            )
        except Exception as error:
            # Any error: the check is someone else's code. The traceback goes to the daemon's log
            # for whoever wrote it; the status only names the error's type, since its message can
            # contain a tool's raw output.
            logger.exception("checking %s failed", name)
            return ToolStatus(
                installed=False,
                detail=f"the check failed ({type(error).__name__}); see the daemon's log",
            )


def describe(status: ToolStatus) -> str:
    """One line about a tool, for the log. Contains no credentials."""
    version = f" {status.version}" if status.version else ""
    if status.usable:
        return f"usable{version}, signed in with {status.auth_label}"
    fix = f"; run: {' / '.join(status.fix)}" if status.fix else ""
    return f"not usable{version}: {status.detail}{fix}"
