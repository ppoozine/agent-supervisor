"""cli.py 的測試：開任務、送訊息、Stop、查看。使用假 adapter。"""

import asyncio
import os
import signal
from pathlib import Path

import aiosqlite
import pytest

from agent_supervisor import cli, db
from agent_supervisor.config import Config
from agent_supervisor.enums import ExitStatus
from agent_supervisor.recovery import InstanceLock
from tests.fake_adapter import FakeAdapter, message, result, session_started
from tests.test_recovery import FakeProcessTable, open_run


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "data")


async def run_cli(
    config: Config, adapter: FakeAdapter, argv: list[str], processes: FakeProcessTable | None = None
) -> int:
    return await cli.run(
        argv, config=config, adapters={"fake": adapter}, processes=processes or FakeProcessTable({})
    )


async def stored_runs(config: Config, task_id: int = 1) -> list[db.Run]:
    conn = await db.connect(config.db_path)
    try:
        return await db.list_runs(conn, task_id)
    finally:
        await conn.close()


async def test_start_runs_a_task_to_completion_and_prints_events(
    config: Config, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    adapter = FakeAdapter([[session_started(), message("hello from agent"), result()]])

    code = await run_cli(
        config, adapter, ["start", "--agent", "fake", "--cwd", str(tmp_path), "say hello"]
    )

    output = capsys.readouterr().out
    runs = await stored_runs(config)
    assert code == 0
    assert runs[0].exit_status == ExitStatus.SUCCESS
    assert adapter.calls[0].prompt == "say hello"
    assert adapter.calls[0].cwd == tmp_path
    assert "task 1" in output
    assert "hello from agent" in output
    assert "success" in output


async def test_start_returns_nonzero_when_the_run_does_not_succeed(
    config: Config, tmp_path: Path
) -> None:
    adapter = FakeAdapter([[session_started(), result(ExitStatus.ERROR)]])

    code = await run_cli(config, adapter, ["start", "--agent", "fake", "--cwd", str(tmp_path), "x"])

    assert code == 1


async def test_message_continues_the_same_task(config: Config, tmp_path: Path) -> None:
    adapter = FakeAdapter(
        [[session_started("s-1"), result(session_id="s-1")], [session_started("s-1"), result()]]
    )
    await run_cli(config, adapter, ["start", "--agent", "fake", "--cwd", str(tmp_path), "first"])

    code = await run_cli(config, adapter, ["message", "1", "second"])

    runs = await stored_runs(config)
    assert code == 0
    assert [run.input_text for run in runs] == ["first", "second"]
    assert adapter.calls[1].session_id == "s-1"


async def test_second_instance_is_refused(
    config: Config, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = InstanceLock.acquire(config.lock_path)
    adapter = FakeAdapter([[session_started(), result()]])

    code = await run_cli(config, adapter, ["start", "--agent", "fake", "--cwd", str(tmp_path), "x"])

    lock.release()
    assert code == 2
    assert adapter.calls == []
    assert "already running" in capsys.readouterr().err


async def test_leftover_run_is_recovered_before_new_work(
    config: Config, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config.data_dir.mkdir(parents=True)
    conn = await db.connect(config.db_path)
    await db.init_schema(conn)
    leftover = await open_run(conn, "left-over")
    await conn.close()
    processes = FakeProcessTable({"left-over": [42]})
    adapter = FakeAdapter([[session_started("s-2"), result(session_id="s-2")]])

    code = await run_cli(
        config, adapter, ["start", "--agent", "fake", "--cwd", str(tmp_path), "second"], processes
    )

    assert code == 0
    assert processes.terminated == [(42, False)]
    assert (await stored_runs(config, leftover.task_id))[0].exit_status == ExitStatus.INTERRUPTED
    assert "interrupted" in capsys.readouterr().out


async def test_interrupt_signal_stops_the_running_task(config: Config, tmp_path: Path) -> None:
    adapter = FakeAdapter([[session_started("s-1"), asyncio.Event(), result()]])
    running = asyncio.create_task(
        run_cli(config, adapter, ["start", "--agent", "fake", "--cwd", str(tmp_path), "x"])
    )
    await _until_session(config, "s-1")

    os.kill(os.getpid(), signal.SIGINT)
    code = await running

    assert code == 1
    assert (await stored_runs(config))[0].exit_status == ExitStatus.STOPPED


async def test_stop_signals_the_running_instance(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    lock = InstanceLock.acquire(config.lock_path)

    code = await run_cli(config, FakeAdapter([]), ["stop"])

    lock.release()
    assert code == 0
    assert sent == [(os.getpid(), signal.SIGINT)]


async def test_stop_without_a_running_instance_reports_an_error(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await run_cli(config, FakeAdapter([]), ["stop"])

    assert code == 1
    assert "nothing is running" in capsys.readouterr().err


async def test_show_prints_runs_and_events(
    config: Config, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    adapter = FakeAdapter([[session_started(), message("visible text"), result(cost_usd=0.25)]])
    await run_cli(config, adapter, ["start", "--agent", "fake", "--cwd", str(tmp_path), "x"])
    capsys.readouterr()

    code = await run_cli(config, adapter, ["show", "1"])

    output = capsys.readouterr().out
    assert code == 0
    assert "initial" in output
    assert "success" in output
    assert "0.25" in output
    assert "visible text" in output


async def test_unknown_task_reports_an_error(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await run_cli(config, FakeAdapter([]), ["message", "99", "hello"])

    assert code == 1
    assert "task 99 not found" in capsys.readouterr().err


async def _until_session(config: Config, session_id: str) -> None:
    async with asyncio.timeout(2.0):
        while True:
            if config.db_path.exists():
                conn = await db.connect(config.db_path)
                try:
                    cursor = await conn.execute(
                        "SELECT 1 FROM tasks WHERE session_id = ?", (session_id,)
                    )
                    if await cursor.fetchone():
                        return
                except aiosqlite.OperationalError:  # 資料表可能還沒建立
                    pass
                finally:
                    await conn.close()
            await asyncio.sleep(0.01)


async def test_config_file_given_on_the_command_line_is_used(tmp_path: Path) -> None:
    data_dir = tmp_path / "custom-data"
    config_path = tmp_path / "asv.toml"
    config_path.write_text(f'data_dir = "{data_dir}"\n')
    adapter = FakeAdapter([[session_started(), result()]])

    code = await cli.run(
        ["--config", str(config_path), "start", "--agent", "fake", "--cwd", str(tmp_path), "x"],
        config=None,
        adapters={"fake": adapter},
        processes=FakeProcessTable({}),
    )

    assert code == 0
    assert (data_dir / "agent-supervisor.db").exists()
