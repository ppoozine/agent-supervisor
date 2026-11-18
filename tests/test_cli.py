"""cli.py 的測試。agent 與 GitHub 是替身，git 是真的。"""

import asyncio
import os
import signal
import sqlite3
from pathlib import Path

import pytest

from agent_supervisor import cli, db
from agent_supervisor.config import Config
from agent_supervisor.enums import CiState, ExitStatus, RunTrigger
from agent_supervisor.github import FailedCheck
from agent_supervisor.recovery import InstanceLock
from tests.conftest import GitRepo
from tests.fake_adapter import Call, FakeAdapter, message, result, session_started
from tests.test_recovery import FakeProcessTable, open_run
from tests.test_stages import FakeGitHub, pr_status


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "data", poll_interval_seconds=0.02)


class Cli:
    """包住 `cli.run`，固定使用同一組替身。"""

    def __init__(self, config: Config, repo: GitRepo) -> None:
        self.config = config
        self.repo = repo
        self.github = FakeGitHub()
        self.processes = FakeProcessTable({})

    async def __call__(self, adapter: FakeAdapter, *argv: str) -> int:
        return await cli.run(
            list(argv),
            config=self.config,
            adapters={"fake": adapter},
            processes=self.processes,
            github=self.github,  # type: ignore[arg-type]
        )

    async def register(self) -> None:
        code = await self(
            FakeAdapter([]), "project", "add", str(self.repo.path), "--github-repo", "owner/demo"
        )
        assert code == 0

    async def start(self, adapter: FakeAdapter, prompt: str = "do it") -> int:
        return await self(
            adapter, "start", "--agent", "fake", "--project", str(self.repo.path), prompt
        )

    async def task(self, task_id: int = 1) -> db.Task:
        conn = await db.connect(self.config.db_path)
        try:
            return await db.get_task(conn, task_id)
        finally:
            await conn.close()

    async def runs(self, task_id: int = 1) -> list[db.Run]:
        conn = await db.connect(self.config.db_path)
        try:
            return await db.list_runs(conn, task_id)
        finally:
            await conn.close()


@pytest.fixture
def asv(config: Config, git_repo: GitRepo) -> Cli:
    return Cli(config, git_repo)


def write_file(name: str, content: str = "hello\n"):  # type: ignore[no-untyped-def]
    """劇本步驟：模擬 agent 在工作目錄裡寫一個檔案。"""

    def step(call: Call) -> None:
        (call.cwd / name).write_text(content)

    return step


async def test_start_runs_the_agent_in_a_worktree_and_opens_a_pull_request(
    asv: Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    await asv.register()
    adapter = FakeAdapter(
        [[session_started(), write_file("hello.txt"), message("wrote the file"), result()]]
    )

    code = await asv.start(adapter, "create hello.txt")

    task = await asv.task()
    output = capsys.readouterr().out
    assert code == 0
    assert task.worktree_path is not None
    assert adapter.calls[0].cwd == Path(task.worktree_path)
    assert adapter.calls[0].cwd != asv.repo.path
    assert (task.pr_number, task.pr_state) == (41, "open")
    assert asv.github.created[0]["head"] == task.branch
    assert asv.repo.git("status", "--porcelain") == ""
    assert not (asv.repo.path / "hello.txt").exists()
    assert "wrote the file" in output
    assert "pr_opened" in output
    assert "success" in output


async def test_start_returns_nonzero_when_the_run_does_not_succeed(asv: Cli) -> None:
    await asv.register()

    code = await asv.start(FakeAdapter([[session_started(), result(ExitStatus.ERROR)]]))

    assert code == 1
    assert asv.github.created == []


async def test_start_registers_an_unknown_repo_automatically(asv: Cli) -> None:
    code = await asv.start(FakeAdapter([[session_started(), result()]]))

    conn = await db.connect(asv.config.db_path)
    project = await db.get_project_by_path(conn, str(asv.repo.path))
    await conn.close()
    assert code == 0
    assert project is not None
    assert project.default_branch == "main"


async def test_start_reports_an_error_for_a_directory_that_is_not_a_git_repo(
    asv: Cli, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "plain-dir"
    plain.mkdir()

    code = await asv(
        FakeAdapter([[session_started(), result()]]),
        "start", "--agent", "fake", "--project", str(plain), "x",
    )  # fmt: skip

    assert code == 1
    assert "run_ended error" in capsys.readouterr().out


async def test_message_continues_in_the_same_worktree_and_pull_request(asv: Cli) -> None:
    await asv.register()
    adapter = FakeAdapter(
        [
            [session_started("s-1"), write_file("a.txt"), result(session_id="s-1")],
            [session_started("s-1"), write_file("b.txt"), result(session_id="s-1")],
        ]
    )
    await asv.start(adapter, "first")

    code = await asv(adapter, "message", "1", "second")

    task = await asv.task()
    assert code == 0
    assert [run.input_text for run in await asv.runs()] == ["first", "second"]
    assert adapter.calls[1].session_id == "s-1"
    assert adapter.calls[1].cwd == adapter.calls[0].cwd
    assert len(asv.github.created) == 1
    assert task.branch is not None
    assert asv.repo.git("show", f"{task.branch}:b.txt", cwd=asv.repo.origin) == "hello"


async def test_project_add_stores_the_given_settings(
    asv: Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await asv(
        FakeAdapter([]),
        "project", "add", str(asv.repo.path),
        "--github-repo", "owner/demo", "--default-branch", "main", "--require-review",
    )  # fmt: skip

    conn = await db.connect(asv.config.db_path)
    project = await db.get_project_by_path(conn, str(asv.repo.path))
    await conn.close()
    assert code == 0
    assert project is not None
    assert (project.github_repo, project.default_branch, project.require_review) == (
        "owner/demo",
        "main",
        True,
    )
    assert "owner/demo" in capsys.readouterr().out


async def test_archive_removes_the_worktree_and_marks_the_task(
    asv: Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    await asv.register()
    await asv.start(FakeAdapter([[session_started(), write_file("a.txt"), result()]]))
    worktree_path = Path((await asv.task()).worktree_path or "")
    (worktree_path / "leftover.txt").write_text("not committed\n")

    code = await asv(FakeAdapter([]), "archive", "1")

    task = await asv.task()
    assert code == 0
    assert task.archived_at is not None
    assert not await asyncio.to_thread(worktree_path.exists)
    assert task.branch is not None
    assert asv.repo.git("show", f"{task.branch}:leftover.txt") == "not committed"
    assert "archived" in capsys.readouterr().out


async def test_second_instance_is_refused(asv: Cli, capsys: pytest.CaptureFixture[str]) -> None:
    lock = InstanceLock.acquire(asv.config.lock_path)
    adapter = FakeAdapter([[session_started(), result()]])

    code = await asv.start(adapter)

    lock.release()
    assert code == 2
    assert adapter.calls == []
    assert "already running" in capsys.readouterr().err


async def test_leftover_run_is_recovered_before_new_work(
    asv: Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    asv.config.data_dir.mkdir(parents=True)
    conn = await db.connect(asv.config.db_path)
    await db.init_schema(conn)
    leftover = await open_run(conn, "left-over")
    await conn.close()
    asv.processes = FakeProcessTable({"left-over": [42]})

    code = await asv.start(FakeAdapter([[session_started("s-2"), result(session_id="s-2")]]))

    assert code == 0
    assert asv.processes.terminated == [(42, False)]
    assert (await asv.runs(leftover.task_id))[0].exit_status == ExitStatus.INTERRUPTED
    assert "interrupted" in capsys.readouterr().out


async def test_interrupt_signal_stops_the_running_task(asv: Cli) -> None:
    adapter = FakeAdapter([[session_started("s-1"), asyncio.Event(), result()]])
    running = asyncio.create_task(asv.start(adapter))
    await _until_session(asv.config, "s-1")

    os.kill(os.getpid(), signal.SIGINT)
    async with asyncio.timeout(5.0):
        code = await running

    assert code == 1
    assert (await asv.runs())[0].exit_status == ExitStatus.STOPPED


async def test_stop_signals_the_running_instance(asv: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    lock = InstanceLock.acquire(asv.config.lock_path)

    code = await asv(FakeAdapter([]), "stop")

    lock.release()
    assert code == 0
    assert sent == [(os.getpid(), signal.SIGUSR1)]


async def test_stop_without_a_running_instance_reports_an_error(
    asv: Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await asv(FakeAdapter([]), "stop")

    assert code == 1
    assert "nothing is running" in capsys.readouterr().err


async def test_show_prints_task_runs_and_events(
    asv: Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    await asv.register()
    adapter = FakeAdapter(
        [[session_started(), write_file("a.txt"), message("visible text"), result(cost_usd=0.25)]]
    )
    await asv.start(adapter)
    capsys.readouterr()

    code = await asv(adapter, "show", "1")

    output = capsys.readouterr().out
    assert code == 0
    assert "asv/task-1" in output
    assert "PR #41 open" in output
    assert "initial" in output
    assert "0.25" in output
    assert "visible text" in output


async def test_unknown_task_reports_an_error(asv: Cli, capsys: pytest.CaptureFixture[str]) -> None:
    code = await asv(FakeAdapter([]), "message", "99", "hello")

    assert code == 1
    assert "task 99 not found" in capsys.readouterr().err


async def test_config_file_given_on_the_command_line_is_used(
    tmp_path: Path, git_repo: GitRepo
) -> None:
    data_dir = tmp_path / "custom-data"
    config_path = tmp_path / "asv.toml"
    config_path.write_text(f'data_dir = "{data_dir}"\n')

    code = await cli.run(
        ["--config", str(config_path), "project", "add", str(git_repo.path)],
        config=None,
        adapters={"fake": FakeAdapter([])},
        processes=FakeProcessTable({}),
        github=FakeGitHub(),  # type: ignore[arg-type]
    )

    assert code == 0
    assert (data_dir / "agent-supervisor.db").exists()


async def _until_session(config: Config, session_id: str) -> None:
    """等到某個 session id 被存進資料庫。

    用唯讀的連線查詢，避免和正在初始化資料庫的 CLI 搶鎖。
    """
    async with asyncio.timeout(5.0):
        # 被觀察的是資料庫裡的值，沒有事件可以等，只能輪詢。
        while not await asyncio.to_thread(_has_session, config.db_path, session_id):  # noqa: ASYNC110
            await asyncio.sleep(0.01)


def _has_session(db_path: Path, session_id: str) -> bool:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:  # 資料庫檔案還不存在
        return False
    try:
        return (
            conn.execute("SELECT 1 FROM tasks WHERE session_id = ?", (session_id,)).fetchone()
            is not None
        )
    except sqlite3.OperationalError:  # 資料表還沒建立，或正在寫入
        return False
    finally:
        conn.close()


async def test_start_requires_an_explicit_project(asv: Cli) -> None:
    with pytest.raises(SystemExit):
        await asv(FakeAdapter([[session_started(), result()]]), "start", "--agent", "fake", "x")

    assert not asv.config.db_path.exists()


async def test_start_fills_in_a_missing_github_repo_for_an_already_registered_project(
    asv: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = await db.connect(asv.config.db_path)
    await db.init_schema(conn)
    await db.upsert_project(conn, str(asv.repo.path))
    await conn.close()

    async def detect(repo_path: Path) -> str | None:
        return "someone/detected"

    monkeypatch.setattr(cli.worktree, "detect_github_repo", detect)

    await asv.start(FakeAdapter([[session_started(), result()]]))

    conn = await db.connect(asv.config.db_path)
    project = await db.get_project_by_path(conn, str(asv.repo.path))
    await conn.close()
    assert project is not None
    assert project.github_repo == "someone/detected"


async def _until(config: Config, query: str, *params: object) -> tuple[object, ...]:
    """等到查詢有結果並回傳第一列。用唯讀連線輪詢，避免和 CLI 搶鎖。"""
    async with asyncio.timeout(10.0):
        while True:
            row = await asyncio.to_thread(_query_one, config.db_path, query, params)
            if row is not None:
                return row
            await asyncio.sleep(0.01)


def _query_one(db_path: Path, query: str, params: tuple[object, ...]) -> tuple[object, ...] | None:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:  # 資料庫檔案還不存在
        return None
    try:
        return conn.execute(query, params).fetchone()  # type: ignore[no-any-return]
    except sqlite3.OperationalError:  # 資料表還沒建立，或正在寫入
        return None
    finally:
        conn.close()


async def test_watch_feeds_a_ci_failure_back_and_ends_up_ready_to_merge(
    asv: Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    await asv.register()
    adapter = FakeAdapter(
        [
            [session_started(), write_file("calc.py", "broken\n"), result()],
            [write_file("calc.py", "fixed\n"), result()],
        ]
    )
    watching = asyncio.create_task(
        asv(adapter, "start", "--agent", "fake", "--watch", "--project", str(asv.repo.path), "go")
    )

    # 第一個 commit 的 CI 失敗
    (first_sha,) = await _until(
        asv.config, "SELECT head_sha FROM tasks WHERE pr_number IS NOT NULL"
    )
    asv.github.job_logs[11] = "pytest\tRun tests\t2026-01-01T00:00:00.0Z assert broken == fixed\n"
    asv.github.statuses[41] = pr_status(
        str(first_sha), CiState.FAILED, failed_checks=(FailedCheck("pytest", 11),)
    )
    # 自動修復 push 了第二個 commit，這次 CI 通過
    # 這段期間替身的 GitHub 仍回報舊的 commit，Poller 不能把它寫回去。
    (second_sha,) = await _until(
        asv.config, "SELECT head_sha FROM tasks WHERE head_sha != ?", first_sha
    )
    await asyncio.sleep(0.2)
    assert (await asv.task()).head_sha == second_sha
    asv.github.statuses[41] = pr_status(str(second_sha), CiState.PASSED)
    await _until(asv.config, "SELECT 1 FROM tasks WHERE ci_state = 'passed'")

    os.kill(os.getpid(), signal.SIGINT)
    assert await watching == 0

    runs = await asv.runs()
    assert [(run.trigger, run.exit_status) for run in runs] == [
        (RunTrigger.INITIAL, ExitStatus.SUCCESS),
        (RunTrigger.CI_FAILURE, ExitStatus.SUCCESS),
    ]
    assert "assert broken == fixed" in adapter.calls[1].prompt
    assert adapter.calls[1].session_id == "session-1"
    task = await asv.task()
    assert (task.ci_fix_attempts, task.last_handled_head_sha) == (1, first_sha)

    capsys.readouterr()
    assert await asv(FakeAdapter([]), "list") == 0
    assert "[Ready to merge / Ready]" in capsys.readouterr().out


async def test_stop_signal_stops_the_run_but_keeps_watching(asv: Cli) -> None:
    await asv.register()
    adapter = FakeAdapter([[session_started("s-1"), asyncio.Event(), result()]])
    watching = asyncio.create_task(
        asv(adapter, "start", "--agent", "fake", "--watch", "--project", str(asv.repo.path), "go")
    )
    await _until_session(asv.config, "s-1")

    os.kill(os.getpid(), signal.SIGUSR1)
    await _until(asv.config, "SELECT 1 FROM runs WHERE exit_status = 'stopped'")
    await asyncio.sleep(0.1)

    assert not watching.done()
    os.kill(os.getpid(), signal.SIGINT)
    assert await watching == 0


async def test_watch_alone_resumes_monitoring_existing_tasks(asv: Cli) -> None:
    await asv.register()
    await asv.start(FakeAdapter([[session_started(), write_file("a.txt"), result()]]))
    head_sha = (await asv.task()).head_sha
    assert head_sha is not None
    asv.github.statuses[41] = pr_status(head_sha, CiState.PASSED)

    watching = asyncio.create_task(asv(FakeAdapter([]), "watch"))
    await _until(asv.config, "SELECT 1 FROM tasks WHERE ci_state = 'passed'")
    os.kill(os.getpid(), signal.SIGINT)

    assert await watching == 0


async def test_list_shows_the_derived_column_for_each_task(
    asv: Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    await asv.register()
    await asv.start(FakeAdapter([[session_started(), write_file("a.txt"), result()]]))
    await asv.start(FakeAdapter([[session_started("s-2"), result(session_id="s-2")]]))
    capsys.readouterr()

    code = await asv(FakeAdapter([]), "list")

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert "task 1  [In review / CI running]  PR #41  ci=pending  fixes=0/3" in lines[0]
    assert "task 2  [Needs you / No changes]  no PR" in lines[1]


async def _deliver(signal_number: int) -> None:
    """對自己送一個訊號，並等事件迴圈把它交給處理函式。"""
    os.kill(os.getpid(), signal_number)
    await asyncio.sleep(0.02)


def _signal_during_recovery(monkeypatch: pytest.MonkeyPatch, signal_number: int) -> None:
    """讓訊號在啟動時的復原階段送到，那時還沒有任何 run。"""

    async def recover(conn: object, processes: object) -> None:
        await _deliver(signal_number)

    monkeypatch.setattr(cli, "_recover", recover)


async def test_interrupt_during_startup_creates_no_run(
    asv: Cli, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    await asv.register()
    _signal_during_recovery(monkeypatch, signal.SIGINT)
    adapter = FakeAdapter([[session_started(), asyncio.Event(), result()]])

    async with asyncio.timeout(5.0):
        code = await asv.start(adapter)

    assert code == 1
    assert adapter.calls == []
    assert _query_one(asv.config.db_path, "SELECT 1 FROM tasks", ()) is None
    assert "interrupted before the run was created" in capsys.readouterr().err


async def test_interrupt_while_the_run_is_being_created_still_stops_it(
    asv: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    await asv.register()
    create_task_with_initial_run = db.create_task_with_initial_run

    async def interrupted_while_creating(*args: object, **kwargs: object) -> object:
        await _deliver(signal.SIGINT)
        return await create_task_with_initial_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db, "create_task_with_initial_run", interrupted_while_creating)
    adapter = FakeAdapter([[session_started(), asyncio.Event(), result()]])

    async with asyncio.timeout(5.0):
        code = await asv.start(adapter)

    assert code == 1
    assert (await asv.runs())[0].exit_status == ExitStatus.STOPPED


async def test_stop_request_during_startup_is_kept_until_the_run_exists(
    asv: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    await asv.register()
    _signal_during_recovery(monkeypatch, signal.SIGUSR1)
    adapter = FakeAdapter([[session_started(), asyncio.Event(), result()]])

    async with asyncio.timeout(5.0):
        code = await asv.start(adapter)

    assert code == 1
    assert (await asv.runs())[0].exit_status == ExitStatus.STOPPED


async def test_interrupt_does_not_wait_for_a_github_query_that_hangs(asv: Cli) -> None:
    await asv.register()
    await asv.start(FakeAdapter([[session_started(), write_file("a.txt"), result()]]))
    asv.github.statuses[41] = pr_status("any", CiState.PASSED)
    querying = asyncio.Event()

    async def hang() -> None:
        querying.set()
        await asyncio.Event().wait()

    asv.github.on_status_query = hang
    watching = asyncio.create_task(asv(FakeAdapter([]), "watch"))

    async with asyncio.timeout(5.0):
        await querying.wait()
        os.kill(os.getpid(), signal.SIGINT)
        assert await watching == 0


async def test_interrupt_during_dispatch_starts_no_further_fix_runs(
    asv: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    await asv.register()
    for number, name in ((41, "a.txt"), (42, "b.txt")):
        await asv.start(FakeAdapter([[session_started(), write_file(name), result()]]))
        head_sha = (await asv.task(number - 40)).head_sha
        asv.github.job_logs[number] = "pytest\tRun tests\t2026-01-01T00:00:00.0Z boom\n"
        asv.github.statuses[number] = pr_status(
            str(head_sha), CiState.FAILED, failed_checks=(FailedCheck("pytest", number),)
        )
    start_ci_fix = cli.Runner.start_ci_fix

    async def interrupted_after_starting(runner: cli.Runner, task_id: int) -> db.Run | None:
        run = await start_ci_fix(runner, task_id)
        await _deliver(signal.SIGINT)
        return run

    monkeypatch.setattr(cli.Runner, "start_ci_fix", interrupted_after_starting)

    async with asyncio.timeout(5.0):
        code = await asv(FakeAdapter([[asyncio.Event(), result()]]), "watch")

    assert code == 0
    assert [run.trigger for run in await asv.runs(1)][-1] == RunTrigger.CI_FAILURE
    assert [run.trigger for run in await asv.runs(2)] == [RunTrigger.INITIAL]
    assert (await asv.task(2)).ci_fix_attempts == 0
