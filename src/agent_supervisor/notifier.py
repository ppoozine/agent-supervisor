"""卡片進 Needs you 時發系統通知。目前只支援 macOS。

通知是「順便」的：發不出去只記一行 log，不影響其他功能，也不讓呼叫端等。
"""

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from agent_supervisor.process import CommandResult, CommandTimeoutError, run_command

TIMEOUT_SECONDS = 10.0
# 標題與內文用參數傳進去，不拼進腳本裡：任務標題是使用者輸入的，可能有引號。
_SCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)
logger = logging.getLogger("agent_supervisor.notifier")

CommandRunner = Callable[..., Awaitable[CommandResult]]


class Notifier:
    """發系統通知。關掉或不在 macOS 上時什麼都不做。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        platform: str = sys.platform,
        run: CommandRunner = run_command,
    ) -> None:
        self._is_active = enabled and platform == "darwin"
        self._run = run
        self._sending: set[asyncio.Task[None]] = set()
        if enabled and not self._is_active:
            logger.info("notifications are only supported on macOS; none will be sent")

    def card_needs_you(self, card: dict[str, Any]) -> None:
        """一張卡片剛進 Needs you。在背景發通知，不等它完成。"""
        if not self._is_active:
            return
        logger.info("task %s needs you (%s); sending a notification", card["id"], card["label"])
        sending = asyncio.create_task(self.send(f"Needs you · {card['label']}", str(card["title"])))
        self._sending.add(sending)
        sending.add_done_callback(self._sending.discard)

    async def send(self, title: str, message: str) -> None:
        """發一則通知。失敗只記錄，不拋出例外。"""
        arguments = ["osascript"]
        for line in _SCRIPT:
            arguments += ["-e", line]
        try:
            result = await self._run([*arguments, title, message], timeout=TIMEOUT_SECONDS)
        except (OSError, CommandTimeoutError) as error:
            logger.warning("notification could not be sent: %s", error)
            return
        if result.returncode != 0:
            logger.warning("notification could not be sent: %s", result.stderr.strip())

    async def wait(self) -> None:
        """等還在發的通知發完。"""
        if self._sending:
            await asyncio.wait(self._sending)
