"""Notify the system when a card enters Needs you. `osascript` on macOS, `notify-send` on Linux.

Notifications are a "nice to have": a failure to send only logs one line, does not affect anything
else, and never makes the caller wait.
"""

import asyncio
import logging
import shutil
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from agent_supervisor.process import CommandResult, CommandTimeoutError, run_command

TIMEOUT_SECONDS = 10.0
# The title and body are passed as arguments, not spliced into the script: task titles are user
# input and may contain quotes.
_SCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)
logger = logging.getLogger("agent_supervisor.notifier")

CommandRunner = Callable[..., Awaitable[CommandResult]]
CommandFinder = Callable[[str], str | None]
CommandBuilder = Callable[[str, str], list[str]]


def _osascript_command(title: str, message: str) -> list[str]:
    arguments = ["osascript"]
    for line in _SCRIPT:
        arguments += ["-e", line]
    return [*arguments, title, message]


def _notify_send_command(title: str, message: str) -> list[str]:
    # Arguments after `--` are never treated as options; the title is user input and may start with
    # `-`.
    return ["notify-send", "--app-name=agent-supervisor", "--", title, message]


def _command_builder(platform: str, which: CommandFinder) -> CommandBuilder | None:
    """Pick the notification command for the platform; None if unsupported or not installed."""
    if platform == "darwin":
        return _osascript_command
    if not platform.startswith("linux"):
        logger.info("notifications are not supported on %s; none will be sent", platform)
        return None
    # notify-send only exists in desktop environments; servers and WSL usually lack it, so check
    # before enabling.
    if which("notify-send") is None:
        logger.info("notify-send was not found; no notifications will be sent")
        return None
    return _notify_send_command


class Notifier:
    """Send a system notification. Do nothing if disabled, unsupported, or not installed."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        platform: str = sys.platform,
        run: CommandRunner = run_command,
        which: CommandFinder = shutil.which,
    ) -> None:
        self._build_command = _command_builder(platform, which) if enabled else None
        self._run = run
        self._sending: set[asyncio.Task[None]] = set()

    def card_needs_you(self, card: dict[str, Any]) -> None:
        """A card just entered Needs you. Notify in the background without waiting for it."""
        if self._build_command is None:
            return
        logger.info("task %s needs you (%s); sending a notification", card["id"], card["label"])
        sending = asyncio.create_task(self.send(f"Needs you · {card['label']}", str(card["title"])))
        self._sending.add(sending)
        sending.add_done_callback(self._sending.discard)

    async def send(self, title: str, message: str) -> None:
        """Send one notification. Failures are only logged, never raised."""
        if self._build_command is None:
            return
        try:
            result = await self._run(self._build_command(title, message), timeout=TIMEOUT_SECONDS)
        except (OSError, CommandTimeoutError) as error:
            logger.warning("notification could not be sent: %s", error)
            return
        if result.returncode != 0:
            logger.warning("notification could not be sent: %s", result.stderr.strip())

    async def wait(self) -> None:
        """Wait for notifications still being sent to finish."""
        if self._sending:
            await asyncio.wait(self._sending)
