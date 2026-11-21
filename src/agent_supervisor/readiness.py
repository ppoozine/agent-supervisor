"""Check which agents and whether gh can be used, and keep the latest result.

The daemon checks once at startup and logs the result. After that the API answers from the kept
result, since checking gh asks GitHub over the network; the settings page can ask for a fresh check
after the user fixes something in a terminal.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from agent_supervisor import db
from agent_supervisor.adapters.base import AgentAdapter
from agent_supervisor.tool_status import ToolStatus


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

    def __init__(self, adapters: Mapping[str, AgentAdapter], github: GitHubCli) -> None:
        self._adapters = adapters
        self._github = github
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
            *(self._adapters[agent_type].check() for agent_type in agent_types),
            self._github.check_cli(),
        )
        return Readiness(dict(zip(agent_types, agents, strict=True)), github, db.utc_now())


def describe(status: ToolStatus) -> str:
    """One line about a tool, for the log. Contains no credentials."""
    version = f" {status.version}" if status.version else ""
    if status.usable:
        return f"usable{version}, signed in with {status.auth_label}"
    fix = f"; run: {' / '.join(status.fix)}" if status.fix else ""
    return f"not usable{version}: {status.detail}{fix}"
