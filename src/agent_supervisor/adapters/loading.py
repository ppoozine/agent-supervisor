"""Find the installed agent adapters.

Adapters register under the `agent_supervisor.adapters` entry point group; the entry point's name
is the agent type, and its value is something that, called with no arguments, returns an adapter
(usually the adapter class). The built-in adapters register the same way, in this project's
`pyproject.toml`, so installing a package that declares one is all it takes to add an agent.

A broken adapter is logged and skipped, so one bad package cannot keep the daemon from starting.
"""

import logging
import re
from collections.abc import Iterable
from importlib.metadata import EntryPoint, entry_points

from agent_supervisor.adapters.base import AdapterCapabilities, AgentAdapter

GROUP = "agent_supervisor.adapters"
BUILT_IN_DISTRIBUTION = "agent-supervisor"
# The agent type is stored with each task and used in the API and in CSS class names.
AGENT_TYPE = re.compile(r"[a-z][a-z0-9_]*")

logger = logging.getLogger("agent_supervisor.adapters")


class InvalidAdapterError(TypeError):
    """An entry point did not produce a usable adapter."""


def load_adapters(found: Iterable[EntryPoint] | None = None) -> dict[str, AgentAdapter]:
    """Load every adapter registered in the entry point group, built-in ones first.

    When two adapters share a name the built-in one, or else the first in name order, wins.
    """
    candidates = list(entry_points(group=GROUP) if found is None else found)
    candidates.sort(key=lambda entry: (not _is_built_in(entry), entry.name, entry.value))
    adapters: dict[str, AgentAdapter] = {}
    for entry in candidates:
        if entry.name in adapters:
            logger.error(
                "adapter %r (%s) skipped: another adapter already uses that name",
                entry.name,
                entry.value,
            )
            continue
        try:
            adapters[entry.name] = _load(entry)
        # Third-party code can fail in any way while being imported or built; whatever happens,
        # skip it and keep the daemon running.
        except Exception as error:
            logger.error("adapter %r (%s) could not be loaded: %s", entry.name, entry.value, error)
    return adapters


def _load(entry: EntryPoint) -> AgentAdapter:
    if AGENT_TYPE.fullmatch(entry.name) is None:
        raise InvalidAdapterError(
            "the entry point name must be lowercase letters, digits and underscores"
        )
    factory = entry.load()
    adapter = factory()
    missing = [name for name in ("run_turn", "check") if not callable(getattr(adapter, name, None))]
    if not isinstance(getattr(adapter, "display_name", None), str):
        missing.append("display_name")
    if not isinstance(getattr(adapter, "capabilities", None), AdapterCapabilities):
        missing.append("capabilities")
    if missing:
        raise InvalidAdapterError(f"the adapter lacks {', '.join(missing)}")
    return adapter  # type: ignore[no-any-return]


def _is_built_in(entry: EntryPoint) -> bool:
    distribution = entry.dist
    return distribution is not None and distribution.metadata["Name"] == BUILT_IN_DISTRIBUTION
