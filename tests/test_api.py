"""API 的測試：REST 端點與事件推送。agent 與 GitHub 是替身，git 與資料庫是真的。"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_supervisor import db
from agent_supervisor.api import feed as feed_module
from agent_supervisor.api.app import create_app
from agent_supervisor.api.feed import ChangeFeed, StreamMessage, stream_text
from agent_supervisor.config import Config
from agent_supervisor.enums import CiState, ExitStatus
from agent_supervisor.supervisor import build_supervisor
from tests.conftest import GitRepo
from tests.fake_adapter import (
    FULL_CAPABILITIES,
    Call,
    FakeAdapter,
    message,
    result,
    session_started,
)
from tests.test_stages import FakeGitHub, pr_status


def write_file(name: str):  # type: ignore[no-untyped-def]
    """劇本步驟：模擬 agent 在工作目錄裡寫一個檔案。"""

    def step(call: Call) -> None:
        (call.cwd / name).write_text("hello\n")

    return step


class Api:
    """一個接上替身的應用程式，以及呼叫它的 client。"""

    def __init__(self, conn: Any, config: Config, repo: GitRepo) -> None:
        self.conn = conn
        self.repo = repo
        self.adapter = FakeAdapter([])
        self.tokens_only = FakeAdapter([], replace(FULL_CAPABILITIES, reports_cost=False))
        self.github = FakeGitHub()
        adapters = {"fake": self.adapter, "tokens-only": self.tokens_only}
        self.supervisor = build_supervisor(conn, config, adapters, self.github)  # type: ignore[arg-type]
        self.feed = ChangeFeed(self.supervisor)
        self.is_closing = False
        self.project_id: int | None = None
        app = create_app(self.supervisor, self.feed, is_closing=lambda: self.is_closing)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://127.0.0.1:8765",
        )

    async def project(self) -> int:
        """加入測試用的 repo（只加一次），回傳專案編號。"""
        if self.project_id is None:
            response = await self.client.post(
                "/api/projects",
                json={"repo_path": str(self.repo.path), "github_repo": "owner/demo"},
            )
            assert response.status_code == 201, response.text
            self.project_id = int(response.json()["id"])
        return self.project_id

    async def task(
        self, script: list[Any], *, agent: str = "fake", **fields: Any
    ) -> dict[str, Any]:
        """開一個任務，回傳建立當下的任務詳情。"""
        adapter = self.adapter if agent == "fake" else self.tokens_only
        adapter.scripts.append(script)
        body = {"project_id": await self.project(), "prompt": "do it", "agent_type": agent}
        response = await self.client.post("/api/tasks", json={**body, **fields})
        assert response.status_code == 201, response.text
        return response.json()  # type: ignore[no-any-return]

    async def agent_started(self, count: int) -> None:
        """等到 agent 被啟動了 `count` 次。兩個任務共用劇本清單，要照順序取用才對得上。"""
        async with asyncio.timeout(10.0):
            while len(self.adapter.calls) < count:  # noqa: ASYNC110  # 沒有事件可以等
                await asyncio.sleep(0.01)

    async def finished(self, task_id: int) -> dict[str, Any]:
        """等任務最新的 run 結束，回傳任務詳情。"""
        runs = await db.list_runs(self.conn, task_id)
        await self.supervisor.runner.wait(runs[-1].id)
        return (await self.client.get(f"/api/tasks/{task_id}")).json()  # type: ignore[no-any-return]


@pytest.fixture
async def api(tmp_path: Path, git_repo: GitRepo) -> AsyncIterator[Api]:
    conn = await db.connect(tmp_path / "data" / "test.db")
    await db.init_schema(conn)
    made = Api(conn, Config(data_dir=tmp_path / "data", task_cost_limit_usd=1.0), git_repo)
    yield made
    await made.supervisor.runner.shutdown()
    await made.client.aclose()
    await conn.close()


def error_of(response: httpx.Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def test_project_can_be_added_and_listed(api: Api) -> None:
    project_id = await api.project()

    listed = (await api.client.get("/api/projects")).json()

    assert [project["id"] for project in listed] == [project_id]
    assert listed[0]["github_repo"] == "owner/demo"
    assert listed[0]["default_branch"] == "main"
    assert listed[0]["require_review"] is False


async def test_adding_a_directory_that_is_not_a_repo_is_rejected(api: Api, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    not_a_repo = await api.client.post("/api/projects", json={"repo_path": str(plain)})
    missing = await api.client.post("/api/projects", json={"repo_path": str(tmp_path / "missing")})

    assert error_of(not_a_repo) == (422, "invalid_request")
    assert error_of(missing) == (422, "invalid_request")
    assert (await api.client.get("/api/projects")).json() == []


async def test_new_task_answers_at_once_and_ends_up_in_review(api: Api) -> None:
    release = asyncio.Event()
    created = await api.task(
        [session_started(), release, write_file("a.txt"), result(cost_usd=0.25)], title="Add a"
    )

    assert (created["column"], created["label"]) == ("Working", "Working")
    assert created["has_open_run"] is True
    assert created["title"] == "Add a"
    assert [run["trigger"] for run in created["runs"]] == ["initial"]

    release.set()
    done = await api.finished(created["id"])

    assert (done["column"], done["label"]) == ("In review", "CI running")
    assert (done["pr_number"], done["pr_state"]) == (41, "open")
    assert done["branch"].startswith("asv/task-1")
    assert (done["cost_usd"], done["tokens_in"], done["tokens_out"]) == (0.25, 100, 10)
    assert done["runs"][0]["exit_status"] == "success"
    cards = (await api.client.get("/api/tasks")).json()
    assert [(card["id"], card["column"]) for card in cards] == [(created["id"], "In review")]


async def test_tasks_can_be_listed_for_one_project(api: Api) -> None:
    created = await api.task([session_started(), result()])
    await api.finished(created["id"])

    mine = (await api.client.get("/api/tasks", params={"project_id": created["project_id"]})).json()
    others = (await api.client.get("/api/tasks", params={"project_id": 999})).json()

    assert [card["id"] for card in mine] == [created["id"]]
    assert others == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param({"project_id": 999, "prompt": "x"}, (404, "not_found"), id="no-project"),
        pytest.param({"prompt": ""}, (422, "invalid_request"), id="empty-prompt"),
        pytest.param({"agent_type": "nope"}, (422, "invalid_request"), id="unknown-agent"),
    ],
)
async def test_task_that_cannot_be_created_is_rejected(
    api: Api, body: dict[str, Any], expected: tuple[int, str]
) -> None:
    request = {"project_id": await api.project(), "prompt": "do it", "agent_type": "fake", **body}

    response = await api.client.post("/api/tasks", json=request)

    assert error_of(response) == expected
    assert (await api.client.get("/api/tasks")).json() == []


async def test_unknown_task_is_not_found(api: Api) -> None:
    for method, path in [
        ("GET", "/api/tasks/99"),
        ("GET", "/api/tasks/99/events"),
        ("POST", "/api/tasks/99/stop"),
        ("POST", "/api/tasks/99/archive"),
    ]:
        assert error_of(await api.client.request(method, path)) == (404, "not_found")
    response = await api.client.post("/api/tasks/99/messages", json={"text": "hi"})
    assert error_of(response) == (404, "not_found")


async def test_message_starts_another_run_in_the_same_session(api: Api) -> None:
    created = await api.task([session_started("s-1"), write_file("a.txt"), result()])
    await api.finished(created["id"])
    api.adapter.scripts.append([write_file("b.txt"), result()])

    response = await api.client.post(f"/api/tasks/{created['id']}/messages", json={"text": "more"})

    assert response.status_code == 201
    done = await api.finished(created["id"])
    assert [run["trigger"] for run in done["runs"]] == ["initial", "user_message"]
    assert api.adapter.calls[1].session_id == "s-1"


async def test_message_is_refused_while_a_run_is_active(api: Api) -> None:
    created = await api.task([session_started(), asyncio.Event(), result()])

    response = await api.client.post(f"/api/tasks/{created['id']}/messages", json={"text": "more"})

    assert error_of(response) == (409, "run_already_active")
    assert len(await db.list_runs(api.conn, created["id"])) == 1


async def test_message_is_refused_once_the_budget_is_used_up(api: Api) -> None:
    created = await api.task([session_started(), result(cost_usd=1.5)])
    await api.finished(created["id"])

    response = await api.client.post(f"/api/tasks/{created['id']}/messages", json={"text": "more"})

    assert error_of(response) == (409, "budget_exhausted")


async def test_stop_ends_only_the_named_task(api: Api) -> None:
    first = await api.task([session_started("s-1"), asyncio.Event(), result()])
    await api.agent_started(1)
    release = asyncio.Event()
    second = await api.task([session_started("s-2"), release, result(session_id="s-2")])

    response = await api.client.post(f"/api/tasks/{first['id']}/stop")

    stopped = response.json()
    assert response.status_code == 200
    assert (stopped["column"], stopped["label"]) == ("Needs you", "Stopped")
    assert stopped["runs"][0]["exit_status"] == "stopped"
    assert (await api.client.get(f"/api/tasks/{second['id']}")).json()["has_open_run"] is True
    release.set()
    assert (await api.finished(second["id"]))["runs"][0]["exit_status"] == "success"


async def test_stop_without_an_active_run_is_refused(api: Api) -> None:
    created = await api.task([session_started(), result()])
    await api.finished(created["id"])

    response = await api.client.post(f"/api/tasks/{created['id']}/stop")

    assert error_of(response) == (409, "no_active_run")


async def test_archive_removes_the_worktree_and_closes_the_task(api: Api) -> None:
    created = await api.task([session_started(), write_file("a.txt"), result()])
    worktree_path = Path((await api.finished(created["id"]))["worktree_path"])

    response = await api.client.post(f"/api/tasks/{created['id']}/archive")

    archived = response.json()
    assert response.status_code == 200
    assert (archived["column"], archived["label"]) == ("Archive", "Archived")
    assert not await asyncio.to_thread(worktree_path.exists)
    again = await api.client.post(f"/api/tasks/{created['id']}/messages", json={"text": "more"})
    assert error_of(again) == (409, "task_not_runnable")


async def test_archive_is_refused_while_a_run_is_active(api: Api) -> None:
    created = await api.task([session_started(), asyncio.Event(), result()])

    response = await api.client.post(f"/api/tasks/{created['id']}/archive")

    assert error_of(response) == (409, "run_already_active")


async def test_events_are_paged_and_can_be_filtered_by_type(api: Api) -> None:
    script = [session_started(), message("one"), message("two"), message("three"), result()]
    created = await api.task(script)
    await api.finished(created["id"])
    path = f"/api/tasks/{created['id']}/events"

    first = (await api.client.get(path, params={"limit": 2})).json()
    rest = (await api.client.get(path, params={"after": first["next_after"]})).json()
    messages = (await api.client.get(path, params={"type": "agent_message"})).json()

    assert len(first["events"]) == 2
    assert first["next_after"] == first["events"][-1]["id"]
    assert rest["next_after"] is None
    assert rest["events"][0]["id"] > first["next_after"]
    assert [event["payload"]["text"] for event in messages["events"]] == ["one", "two", "three"]


async def test_usage_separates_unknown_cost_from_zero_cost(api: Api) -> None:
    paid = await api.task([session_started(), result(cost_usd=0.25, tokens_in=100, tokens_out=10)])
    await api.finished(paid["id"])
    unknown = await api.task(
        [
            session_started("s-2"),
            result(session_id="s-2", cost_usd=None, tokens_in=7, tokens_out=3),
        ],
        agent="tokens-only",
    )
    card = await api.finished(unknown["id"])

    usage = (await api.client.get("/api/usage")).json()

    assert (card["cost_usd"], card["tokens"]) == (None, 10)
    assert (usage["cost_usd"], usage["tokens_in"], usage["tokens_out"]) == (0.25, 107, 13)
    assert usage["has_unknown_cost"] is True
    assert [row["cost_usd"] for row in usage["tasks"]] == [0.25, None]


async def collect(subscription: feed_module.Subscription) -> list[StreamMessage]:
    messages = []
    while not subscription.queue.empty():
        messages.append(subscription.queue.get_nowait())
    return messages


async def test_feed_publishes_new_events_and_changed_cards(api: Api) -> None:
    await api.feed.start()
    subscription = api.feed.subscribe()
    created = await api.task([session_started(), message("hello"), write_file("a.txt"), result()])
    await api.finished(created["id"])

    await api.feed.publish_changes()

    messages = await collect(subscription)
    events = [item for item in messages if item.kind == "event"]
    cards = [item for item in messages if item.kind == "task"]
    assert [item.data["type"] for item in events][:2] == ["session_started", "agent_message"]
    assert [item.event_id for item in events] == sorted(item.event_id for item in events)  # type: ignore[type-var]
    assert [(card.data["id"], card.data["column"]) for card in cards] == [
        (created["id"], "In review")
    ]
    assert cards[0].event_id is None

    await api.feed.publish_changes()
    assert await collect(subscription) == []


async def test_feed_notices_changes_that_write_no_event(api: Api) -> None:
    created = await api.task([session_started(), write_file("a.txt"), result()])
    await api.finished(created["id"])
    await api.feed.start()
    subscription = api.feed.subscribe()

    await db.update_task(api.conn, created["id"], ci_state=CiState.PASSED, merge_state="clean")
    await api.feed.publish_changes()

    (card,) = await collect(subscription)
    assert (card.kind, card.data["column"]) == ("task", "Ready to merge")


async def test_feed_wakes_up_on_every_commit(api: Api) -> None:
    await api.feed.start()
    subscription = api.feed.subscribe()
    running = asyncio.create_task(api.feed.run())
    try:
        created = await api.task([session_started(), result(ExitStatus.ERROR)])
        async with asyncio.timeout(5.0):
            while True:
                item = await subscription.queue.get()
                if item.kind == "task" and item.data["label"] == "Agent error":
                    break
        assert item.data["id"] == created["id"]
    finally:
        running.cancel()


async def test_stream_replays_missed_events_then_continues_live(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feed_module, "IDLE_CHECK_SECONDS", 0.01)
    await api.feed.start()
    script = [session_started(), message("one"), message("two"), write_file("a.txt"), result()]
    created = await api.task(script)
    await api.finished(created["id"])
    await api.feed.publish_changes()
    events = await db.list_events(api.conn, created["id"])
    seen_up_to = events[1].id

    chunks: list[str] = []
    async with asyncio.timeout(5.0):
        async for chunk in stream_text(api.feed, seen_up_to, lambda: api.is_closing):
            chunks.append(chunk)
            if len(chunks) == 1 + len(events) - 2:
                # 補送完了：這時再發生的事也要收得到。
                await db.update_task(
                    api.conn, created["id"], ci_state=CiState.PASSED, merge_state="clean"
                )
                await api.feed.publish_changes()
            if chunk.startswith("event: task"):
                api.is_closing = True

    assert chunks[0] == ": connected\n\n"
    replayed = [chunk for chunk in chunks if chunk.startswith("id: ")]
    assert [int(chunk.split("\n")[0].removeprefix("id: ")) for chunk in replayed] == [
        event.id for event in events[2:]
    ]
    assert replayed[0].split("\n")[1] == "event: event"
    assert json.loads(replayed[0].split("\n")[2].removeprefix("data: "))["payload"]["text"] == "two"
    assert (
        json.loads(chunks[-1].split("\n")[1].removeprefix("data: "))["column"] == "Ready to merge"
    )


async def test_stream_without_a_last_event_id_replays_nothing(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feed_module, "IDLE_CHECK_SECONDS", 0.01)
    await api.feed.start()
    created = await api.task([session_started(), result()])
    await api.finished(created["id"])
    await api.feed.publish_changes()
    api.is_closing = True

    chunks = [chunk async for chunk in stream_text(api.feed, None, lambda: api.is_closing)]

    assert chunks == [": connected\n\n"]


async def test_connection_that_reads_too_slowly_is_ended(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feed_module, "MAX_PENDING_MESSAGES", 2)
    monkeypatch.setattr(feed_module, "IDLE_CHECK_SECONDS", 0.01)
    await api.feed.start()
    stream = stream_text(api.feed, None, lambda: False)
    assert await anext(stream) == ": connected\n\n"
    created = await api.task([session_started(), message("a"), message("b"), result()])
    await api.finished(created["id"])

    await api.feed.publish_changes()  # 這條連線沒有在讀，超過上限

    async with asyncio.timeout(5.0):
        rest = [chunk async for chunk in stream]
    assert rest == []


async def test_event_that_arrives_during_a_replay_is_sent_only_once(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feed_module, "IDLE_CHECK_SECONDS", 0.01)
    await api.feed.start()
    created = await api.task([session_started(), message("one"), result()])
    await api.finished(created["id"])
    await api.feed.publish_changes()
    first_id = (await db.list_events(api.conn, created["id"]))[0].id
    stream = stream_text(api.feed, first_id, lambda: False)
    assert await anext(stream) == ": connected\n\n"

    # 已經訂閱、還沒開始補送的這一刻發生的事件：補送與即時兩邊都會有它。
    await db.append_event(api.conn, created["id"], None, "agent_message", {"text": "during"})
    await api.feed.publish_changes()
    seen: list[int] = []
    async with asyncio.timeout(5.0):
        async for chunk in stream:
            if not chunk.startswith("id: "):
                continue
            seen.append(int(chunk.split("\n")[0].removeprefix("id: ")))
            if len(seen) == 1:
                await db.append_event(
                    api.conn, created["id"], None, "agent_message", {"text": "end"}
                )
                await api.feed.publish_changes()
            if '"end"' in chunk:
                break

    expected = [event.id for event in await db.list_events(api.conn, created["id"])][1:]
    assert seen == expected


async def test_unexpected_failure_still_answers_in_the_agreed_shape(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await api.task([session_started(), result()])
    await api.finished(created["id"])

    async def broken_archive(task_id: int) -> None:
        raise RuntimeError("git worktree remove failed")

    monkeypatch.setattr(api.supervisor.runner, "archive", broken_archive)

    response = await api.client.post(f"/api/tasks/{created['id']}/archive")

    assert error_of(response) == (500, "internal_error")
    assert "git worktree remove failed" in response.json()["error"]["message"]


async def test_body_that_is_not_declared_as_json_is_rejected(api: Api) -> None:
    """網頁不經 preflight 就能送出的請求（純文字的 body）不能開任務。"""
    body = json.dumps({"project_id": await api.project(), "prompt": "x", "agent_type": "fake"})

    response = await api.client.post(
        "/api/tasks", content=body, headers={"Content-Type": "text/plain"}
    )

    assert error_of(response) == (422, "invalid_request")
    assert (await api.client.get("/api/tasks")).json() == []


def test_message_is_encoded_as_server_sent_event() -> None:
    with_id = StreamMessage("event", {"text": "中文"}, 7).encode()
    without_id = StreamMessage("task", {"id": 1}).encode()

    assert with_id == 'id: 7\nevent: event\ndata: {"text": "中文"}\n\n'
    assert without_id == 'event: task\ndata: {"id": 1}\n\n'


async def test_stream_endpoint_answers_as_an_event_stream(api: Api) -> None:
    api.is_closing = True

    response = await api.client.get("/api/stream")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == ": connected\n\n"


async def test_poll_result_from_github_moves_the_card(api: Api) -> None:
    created = await api.task([session_started(), write_file("a.txt"), result()])
    done = await api.finished(created["id"])
    api.github.statuses[41] = pr_status(done["head_sha"], CiState.PASSED)

    await api.supervisor.poll_and_dispatch(asyncio.Event())

    card = (await api.client.get(f"/api/tasks/{created['id']}")).json()
    assert (card["column"], card["label"]) == ("Ready to merge", "Ready")


async def test_adding_a_project_twice_is_refused_and_keeps_its_settings(api: Api) -> None:
    body = {"repo_path": str(api.repo.path), "default_branch": "develop", "require_review": True}
    assert (await api.client.post("/api/projects", json=body)).status_code == 201

    again = await api.client.post("/api/projects", json={"repo_path": str(api.repo.path)})

    assert error_of(again) == (409, "project_exists")
    [project] = (await api.client.get("/api/projects")).json()
    assert (project["default_branch"], project["require_review"]) == ("develop", True)


async def test_subdirectory_of_a_repo_is_not_accepted_as_a_project(api: Api) -> None:
    inside = api.repo.path / "src"
    inside.mkdir()

    response = await api.client.post("/api/projects", json={"repo_path": str(inside)})

    assert error_of(response) == (422, "invalid_request")


async def test_archiving_an_archived_task_is_refused(api: Api) -> None:
    task = await api.task([session_started("s1"), write_file("a.txt"), result()])
    await api.finished(task["id"])
    assert (await api.client.post(f"/api/tasks/{task['id']}/archive")).status_code == 200

    again = await api.client.post(f"/api/tasks/{task['id']}/archive")

    assert error_of(again) == (409, "task_not_runnable")


async def test_unknown_path_and_wrong_method_answer_in_the_agreed_shape(api: Api) -> None:
    assert error_of(await api.client.get("/api/nowhere")) == (404, "not_found")
    assert error_of(await api.client.delete("/api/tasks")) == (405, "method_not_allowed")


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Host": "evil.example"},
        {"Host": "evil.example:8765", "Origin": "http://evil.example:8765"},
    ],
)
async def test_request_from_another_site_is_refused(api: Api, headers: dict[str, str]) -> None:
    task = await api.task([session_started("s1"), write_file("a.txt"), result()])
    await api.finished(task["id"])

    response = await api.client.post(f"/api/tasks/{task['id']}/archive", headers=headers)

    assert error_of(response) == (403, "forbidden")
    assert (await api.client.get(f"/api/tasks/{task['id']}")).json()["archived_at"] is None


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:8765"])
async def test_request_from_a_local_page_is_accepted(api: Api, origin: str) -> None:
    response = await api.client.get("/api/projects", headers={"Origin": origin})

    assert response.status_code == 200


async def test_failed_publish_sends_the_same_events_on_the_next_try(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    await api.feed.start()
    subscription = api.feed.subscribe()
    task = await api.task([session_started("s1"), message("hi"), result()])
    await api.finished(task["id"])
    read_fingerprints = db.list_task_fingerprints

    async def fail(conn: Any) -> Any:
        raise RuntimeError("database is busy")

    monkeypatch.setattr(db, "list_task_fingerprints", fail)
    with pytest.raises(RuntimeError):
        await api.feed.publish_changes()
    assert subscription.queue.empty()

    monkeypatch.setattr(db, "list_task_fingerprints", read_fingerprints)
    await api.feed.publish_changes()

    kinds = {subscription.queue.get_nowait().kind for _ in range(subscription.queue.qsize())}
    assert kinds == {"event", "task"}


async def test_agents_are_listed_with_whether_they_report_cost(api: Api) -> None:
    listed = (await api.client.get("/api/agents")).json()

    assert listed == [
        {"agent_type": "fake", "reports_cost": True},
        {"agent_type": "tokens-only", "reports_cost": False},
    ]


async def test_web_files_are_served_next_to_the_api(api: Api, tmp_path: Path) -> None:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<title>board</title>")
    app = create_app(api.supervisor, api.feed, static_dir=web)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        assert "board" in (await client.get("/")).text
        assert (await client.get("/api/projects")).json() == []
        # 靜態檔案接走了其他所有路徑，錯誤還是要用約定的形狀回應。
        assert error_of(await client.get("/api/nowhere")) == (404, "not_found")
        assert error_of(await client.delete("/api/tasks")) == (405, "method_not_allowed")
        assert (await client.head("/api/agents")).status_code == 405
