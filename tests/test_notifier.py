"""notifier.py 的測試。發通知的指令用替身，不真的跳通知。"""

import logging
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_supervisor.notifier import Notifier
from agent_supervisor.process import CommandResult, CommandTimeoutError

CARD = {"id": 7, "title": 'Fix the "quoted" title', "label": "Stopped", "column": "Needs you"}


class FakeRunner:
    """記錄收到的參數；可以設定成失敗。"""

    def __init__(self, outcome: CommandResult | Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.outcome = outcome or CommandResult(0, "", "")

    async def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float = 0,  # noqa: ASYNC109  # 和 run_command 的參數一致
    ) -> CommandResult:
        self.calls.append(list(args))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def found(command: str) -> str | None:
    return f"/usr/bin/{command}"


def missing(command: str) -> str | None:
    return None


async def notify(runner: FakeRunner, platform: str = "darwin", **options: object) -> None:
    notifier = Notifier(platform=platform, run=runner, which=found, **options)  # type: ignore[arg-type]
    notifier.card_needs_you(CARD)
    await notifier.wait()


async def test_notification_carries_the_reason_and_the_task_title() -> None:
    runner = FakeRunner()

    await notify(runner)

    (call,) = runner.calls
    assert call[0] == "osascript"
    # 標題與內文是獨立的參數，不會被當成腳本的一部分。
    assert call[-2:] == ["Needs you · Stopped", 'Fix the "quoted" title']
    assert not any("quoted" in part for part in call[:-2])


async def test_nothing_is_sent_when_notifications_are_turned_off() -> None:
    runner = FakeRunner()

    await notify(runner, enabled=False)

    assert runner.calls == []


async def test_linux_notification_uses_notify_send_with_separate_arguments() -> None:
    runner = FakeRunner()

    await notify(runner, platform="linux")

    (call,) = runner.calls
    assert call[0] == "notify-send"
    # `--` 之後一律當成內容：標題以 `-` 開頭也不會被當成選項。
    assert call[-3:] == ["--", "Needs you · Stopped", 'Fix the "quoted" title']


async def test_nothing_is_sent_on_linux_without_notify_send(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="agent_supervisor.notifier")
    runner = FakeRunner()
    notifier = Notifier(platform="linux", run=runner, which=missing)

    notifier.card_needs_you(CARD)
    await notifier.wait()

    assert runner.calls == []
    assert "notify-send" in caplog.text


async def test_nothing_is_sent_on_a_platform_without_support() -> None:
    runner = FakeRunner()
    notifier = Notifier(platform="win32", run=runner, which=found)

    notifier.card_needs_you(CARD)
    await notifier.wait()

    assert runner.calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(CommandResult(1, "", "execution error"), id="command-fails"),
        pytest.param(FileNotFoundError("osascript"), id="command-missing"),
        pytest.param(CommandTimeoutError("osascript did not finish"), id="command-hangs"),
    ],
)
@pytest.mark.parametrize("platform", ["darwin", "linux"])
async def test_failing_notification_is_logged_and_does_not_raise(
    outcome: CommandResult | Exception, platform: str, caplog: pytest.LogCaptureFixture
) -> None:
    await notify(FakeRunner(outcome), platform=platform)

    assert "notification could not be sent" in caplog.text
