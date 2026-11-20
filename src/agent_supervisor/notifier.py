"""卡片進 Needs you 時發系統通知。macOS 用 `osascript`，Linux 用 `notify-send`。

通知是「順便」的：發不出去只記一行 log，不影響其他功能，也不讓呼叫端等。
"""

import asyncio
import logging
import shutil
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
CommandFinder = Callable[[str], str | None]
CommandBuilder = Callable[[str, str], list[str]]


def _osascript_command(title: str, message: str) -> list[str]:
    arguments = ["osascript"]
    for line in _SCRIPT:
        arguments += ["-e", line]
    return [*arguments, title, message]


def _notify_send_command(title: str, message: str) -> list[str]:
    # `--` 之後的參數不會被當成選項，標題是使用者輸入的，可能以 `-` 開頭。
    return ["notify-send", "--app-name=agent-supervisor", "--", title, message]


def _command_builder(platform: str, which: CommandFinder) -> CommandBuilder | None:
    """依平台挑發通知的指令；不支援或指令不存在時回傳 None。"""
    if platform == "darwin":
        return _osascript_command
    if not platform.startswith("linux"):
        logger.info("notifications are not supported on %s; none will be sent", platform)
        return None
    # notify-send 只有桌面環境才有，伺服器與 WSL 通常沒有，所以先確認再啟用。
    if which("notify-send") is None:
        logger.info("notify-send was not found; no notifications will be sent")
        return None
    return _notify_send_command


class Notifier:
    """發系統通知。關掉、平台不支援或找不到指令時什麼都不做。"""

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
        """一張卡片剛進 Needs you。在背景發通知，不等它完成。"""
        if self._build_command is None:
            return
        logger.info("task %s needs you (%s); sending a notification", card["id"], card["label"])
        sending = asyncio.create_task(self.send(f"Needs you · {card['label']}", str(card["title"])))
        self._sending.add(sending)
        sending.add_done_callback(self._sending.discard)

    async def send(self, title: str, message: str) -> None:
        """發一則通知。失敗只記錄，不拋出例外。"""
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
        """等還在發的通知發完。"""
        if self._sending:
            await asyncio.wait(self._sending)
