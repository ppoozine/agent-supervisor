"""SQLite schema 與查詢。不使用 ORM。

不變量由資料庫本身保證：
- 每個任務同時最多一個未結束的 run（部分唯一索引）
- `events` 只能新增，不能修改或刪除（trigger）

所有模組共用一條連線，所以每一筆寫入都要先拿到該連線的寫入鎖：
否則一方的 commit 會把另一方做到一半的交易切開。
"""

import asyncio
import json
import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from agent_supervisor.enums import EventType, ExitStatus, MergeState, PrState, RunTrigger

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    repo_path TEXT NOT NULL UNIQUE,
    github_repo TEXT,
    default_branch TEXT NOT NULL DEFAULT 'main',
    require_review INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    model TEXT,
    session_id TEXT,
    branch TEXT,
    worktree_path TEXT,
    pr_number INTEGER,
    pr_state TEXT,
    head_sha TEXT,
    ci_state TEXT NOT NULL DEFAULT 'none',
    review_state TEXT NOT NULL DEFAULT 'none',
    last_handled_head_sha TEXT,
    ci_fix_attempts INTEGER NOT NULL DEFAULT 0,
    last_handled_feedback_event_id INTEGER,
    feedback_attempts INTEGER NOT NULL DEFAULT 0,
    merge_state TEXT NOT NULL DEFAULT 'unknown',
    last_handled_conflict_head_sha TEXT,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    trigger TEXT NOT NULL,
    input_text TEXT NOT NULL,
    source_head_sha TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    exit_status TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS runs_one_open_per_task
    ON runs(task_id) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    run_id INTEGER REFERENCES runs(id),
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS events_by_task ON events(task_id, id);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
"""

FINISHED_PR_STATES = (PrState.MERGED, PrState.CLOSED)

# 後來才加的欄位：（資料表, 欄位, 定義）。舊的資料庫在啟動時補上。
# 新增欄位時這裡與上面的 SCHEMA 都要寫。
ADDED_COLUMNS = (
    ("tasks", "last_handled_feedback_event_id", "INTEGER"),
    ("tasks", "feedback_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "merge_state", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("tasks", "last_handled_conflict_head_sha", "TEXT"),
)

_write_locks: weakref.WeakKeyDictionary[aiosqlite.Connection, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)
CommitListener = Callable[[], None]
_commit_listeners: weakref.WeakKeyDictionary[aiosqlite.Connection, list[CommitListener]] = (
    weakref.WeakKeyDictionary()
)


class NotFoundError(LookupError):
    """查詢的資料列不存在。"""


class RunAlreadyActiveError(RuntimeError):
    """任務已經有一個未結束的 run。"""


class TaskNotRunnableError(RuntimeError):
    """任務已封存，或 PR 已合併或關閉，不能再建立 run。"""


@dataclass(frozen=True)
class Project:
    """一個已登記的本機 repo。"""

    id: int
    name: str
    repo_path: str
    github_repo: str | None
    default_branch: str
    require_review: bool


@dataclass(frozen=True)
class Task:
    """一個任務，以及 Poller 對 GitHub 狀態的快取。"""

    id: int
    project_id: int
    title: str
    prompt: str
    agent_type: str
    model: str | None
    session_id: str | None
    branch: str | None
    worktree_path: str | None
    pr_number: int | None
    pr_state: str | None
    head_sha: str | None
    ci_state: str
    review_state: str
    last_handled_head_sha: str | None
    ci_fix_attempts: int
    last_handled_feedback_event_id: int | None
    feedback_attempts: int
    merge_state: str
    last_handled_conflict_head_sha: str | None
    archived_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Run:
    """任務的一次執行。"""

    id: int
    task_id: int
    trigger: RunTrigger
    input_text: str
    source_head_sha: str | None
    started_at: str
    ended_at: str | None
    exit_status: ExitStatus | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None


@dataclass(frozen=True)
class Event:
    """一筆稽核紀錄。"""

    id: int
    task_id: int
    run_id: int | None
    ts: str
    type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class TaskUsage:
    """任務累計的花費與 token 數。`is_complete` 為否表示有 run 的用量未知。"""

    cost_usd: float
    tokens: int
    is_complete: bool


@dataclass(frozen=True)
class TaskSnapshot:
    """推導看板欄位與自動修復條件所需的全部資料。"""

    task: Task
    require_review: bool
    last_run: Run | None
    usage: TaskUsage
    pending_feedback: int = 0
    """PR 上還沒餵回 agent 的留言數。"""

    @property
    def has_open_run(self) -> bool:
        """任務是否有 run 正在執行。未結束的 run 一定是最後一個。"""
        return self.last_run is not None and self.last_run.ended_at is None


@asynccontextmanager
async def _transaction(conn: aiosqlite.Connection) -> AsyncIterator[None]:
    """拿到連線的寫入鎖後執行一個交易：正常離開就 commit，否則 rollback。"""
    async with _write_locks.setdefault(conn, asyncio.Lock()):
        try:
            yield
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
    for listener in _commit_listeners.get(conn, ()):
        listener()


def on_commit(conn: aiosqlite.Connection, listener: CommitListener) -> None:
    """登記一個函式，這條連線每次有交易 commit 之後都會呼叫它。

    所有寫入都經過同一種交易，所以這是「資料有變」唯一的通知點。
    """
    _commit_listeners.setdefault(conn, []).append(listener)


@asynccontextmanager
async def consistent_read(conn: aiosqlite.Connection) -> AsyncIterator[None]:
    """區塊內的讀取不會看到別人做到一半的交易。

    所有模組共用一條連線，同一條連線讀得到還沒 commit 的寫入。要把結果交給外部
    （API 的回應、推送給瀏覽器的事件）的讀取都要包在這裡面。不能在交易內使用。
    """
    async with _write_locks.setdefault(conn, asyncio.Lock()):
        yield


def utc_now() -> str:
    """目前的 UTC 時間，ISO 8601 字串。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


async def connect(db_path: str | Path) -> aiosqlite.Connection:
    """開啟資料庫連線並套用必要的 pragma；資料庫所在的目錄不存在時會建立。"""
    await asyncio.to_thread(Path(db_path).parent.mkdir, parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    return conn


async def init_schema(conn: aiosqlite.Connection) -> None:
    """建立資料表、索引與 trigger（已存在則略過），並替舊的資料庫補上後來才加的欄位。"""
    await conn.executescript(SCHEMA)
    for table, column, definition in ADDED_COLUMNS:
        # 資料表與欄位名稱只會來自上面的常數，不是外部輸入。
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        if column not in {row["name"] for row in await cursor.fetchall()}:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    await conn.commit()


async def upsert_project(
    conn: aiosqlite.Connection,
    repo_path: str,
    *,
    github_repo: str | None = None,
    default_branch: str = "main",
    require_review: bool = False,
) -> Project:
    """登記一個本機 repo；已登記過就更新它的設定。"""
    async with _transaction(conn):
        await conn.execute(
            "INSERT INTO projects (name, repo_path, github_repo, default_branch, require_review)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(repo_path) DO UPDATE SET github_repo = excluded.github_repo,"
            " default_branch = excluded.default_branch, require_review = excluded.require_review",
            (Path(repo_path).name, repo_path, github_repo, default_branch, int(require_review)),
        )
    project = await get_project_by_path(conn, repo_path)
    assert project is not None
    return project


async def list_projects(conn: aiosqlite.Connection) -> list[Project]:
    """列出所有已登記的專案，依編號。"""
    cursor = await conn.execute("SELECT * FROM projects ORDER BY id")
    return [_to_project(row) for row in await cursor.fetchall()]


async def get_project_by_path(conn: aiosqlite.Connection, repo_path: str) -> Project | None:
    """依 repo 路徑取得專案，沒有登記過則回傳 None。"""
    row = await _fetch_one(conn, "SELECT * FROM projects WHERE repo_path = ?", (repo_path,))
    return None if row is None else _to_project(row)


async def get_project(conn: aiosqlite.Connection, project_id: int) -> Project:
    """依主鍵取得專案。"""
    row = await _fetch_one(conn, "SELECT * FROM projects WHERE id = ?", (project_id,))
    if row is None:
        raise NotFoundError(f"project {project_id} not found")
    return _to_project(row)


async def create_task_with_initial_run(
    conn: aiosqlite.Connection,
    *,
    project_id: int,
    title: str,
    prompt: str,
    agent_type: str,
    model: str | None,
    worktree_path: str | None = None,
) -> tuple[Task, Run]:
    """在同一個交易內建立任務與它的 initial run。"""
    now = utc_now()
    async with _transaction(conn):
        task_cursor = await conn.execute(
            "INSERT INTO tasks (project_id, title, prompt, agent_type, model, worktree_path,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, title, prompt, agent_type, model, worktree_path, now, now),
        )
        task_id = _last_id(task_cursor)
        run_cursor = await conn.execute(
            "INSERT INTO runs (task_id, trigger, input_text, started_at) VALUES (?, ?, ?, ?)",
            (task_id, RunTrigger.INITIAL, prompt, now),
        )
        run_id = _last_id(run_cursor)
    return await get_task(conn, task_id), await get_run(conn, run_id)


async def create_run(
    conn: aiosqlite.Connection,
    task_id: int,
    trigger: RunTrigger,
    input_text: str,
    source_head_sha: str | None = None,
) -> Run:
    """為既有任務建立一個 run；任務不可執行或已有未結束的 run 時拒絕。

    使用者送訊息代表人已經介入，所以同一個交易內把自動修復與留言處理的次數歸零。
    """
    try:
        async with _transaction(conn):
            task = await get_task(conn, task_id)
            if task.archived_at is not None or task.pr_state in FINISHED_PR_STATES:
                raise TaskNotRunnableError(f"task {task_id} is archived or its PR is finished")
            run_id = await _insert_run(conn, task_id, trigger, input_text, source_head_sha)
            if trigger is RunTrigger.USER_MESSAGE:
                await conn.execute(
                    "UPDATE tasks SET ci_fix_attempts = 0, feedback_attempts = 0, updated_at = ?"
                    " WHERE id = ?",
                    (utc_now(), task_id),
                )
    except aiosqlite.IntegrityError as error:
        raise RunAlreadyActiveError(f"task {task_id} already has an active run") from error
    return await get_run(conn, run_id)


async def create_ci_failure_run(
    conn: aiosqlite.Connection,
    task_id: int,
    input_text: str,
    *,
    condition: Callable[[TaskSnapshot], bool],
) -> Run | None:
    """條件成立時建立一個 ci_failure run，並記下已處理的 commit、把修復次數加一。

    重新檢查條件、建立 run、更新兩個防迴圈欄位都在同一個交易內；
    條件不成立時什麼都不寫，回傳 None。
    """
    async with _transaction(conn):
        snapshot = await get_snapshot(conn, task_id)
        if not condition(snapshot):
            return None
        head_sha = snapshot.task.head_sha
        run_id = await _insert_run(conn, task_id, RunTrigger.CI_FAILURE, input_text, head_sha)
        await conn.execute(
            "UPDATE tasks SET last_handled_head_sha = ?, ci_fix_attempts = ci_fix_attempts + 1,"
            " updated_at = ? WHERE id = ?",
            (head_sha, utc_now(), task_id),
        )
    return await get_run(conn, run_id)


async def create_conflict_run(
    conn: aiosqlite.Connection,
    task_id: int,
    input_text: str,
    *,
    condition: Callable[[TaskSnapshot], bool],
) -> Run | None:
    """條件成立時建立一個 merge_conflict run，並記下已處理的 commit、把修復次數加一。

    做法和 `create_ci_failure_run` 相同，但用自己的「已處理」欄位：
    同一個 commit 可能既有衝突又有 CI 失敗，兩者不能互相蓋掉。修復次數則是共用的。
    """
    async with _transaction(conn):
        snapshot = await get_snapshot(conn, task_id)
        if not condition(snapshot):
            return None
        head_sha = snapshot.task.head_sha
        run_id = await _insert_run(conn, task_id, RunTrigger.MERGE_CONFLICT, input_text, head_sha)
        await conn.execute(
            "UPDATE tasks SET last_handled_conflict_head_sha = ?,"
            " ci_fix_attempts = ci_fix_attempts + 1, updated_at = ? WHERE id = ?",
            (head_sha, utc_now(), task_id),
        )
    return await get_run(conn, run_id)


async def create_feedback_run(
    conn: aiosqlite.Connection,
    task_id: int,
    *,
    build_prompt: Callable[[list[Event]], str],
    condition: Callable[[TaskSnapshot], bool],
) -> Run | None:
    """條件成立時建立一個 review_feedback run，內容是所有還沒處理的留言。

    讀出留言、建立 run、把「處理到哪一則」往前推、次數加一都在同一個交易內，
    所以同一則留言不會被餵回兩次，也不會有留言在中間被跳過。
    條件不成立時什麼都不寫，回傳 None。
    """
    async with _transaction(conn):
        snapshot = await get_snapshot(conn, task_id)
        pending = await list_pending_feedback(conn, task_id)
        if not pending or not condition(snapshot):
            return None
        run_id = await _insert_run(
            conn, task_id, RunTrigger.REVIEW_FEEDBACK, build_prompt(pending), None
        )
        await conn.execute(
            "UPDATE tasks SET last_handled_feedback_event_id = ?,"
            " feedback_attempts = feedback_attempts + 1, updated_at = ? WHERE id = ?",
            (pending[-1].id, utc_now(), task_id),
        )
    return await get_run(conn, run_id)


_PENDING_FEEDBACK = (
    " FROM events e JOIN tasks t ON t.id = e.task_id"
    " WHERE e.task_id = ? AND e.type = ?"
    " AND e.id > COALESCE(t.last_handled_feedback_event_id, 0)"
    " AND json_extract(e.payload, '$.ignored') IS NULL"
)


async def list_pending_feedback(conn: aiosqlite.Connection, task_id: int) -> list[Event]:
    """任務還沒餵回 agent 的留言，依記錄的順序。被略過的留言（例如機器人的）不算。"""
    cursor = await conn.execute(
        "SELECT e.*" + _PENDING_FEEDBACK + " ORDER BY e.id", (task_id, EventType.REVIEW_COMMENT)
    )
    return [_to_event(row) for row in await cursor.fetchall()]


async def list_feedback_ids(conn: aiosqlite.Connection, task_id: int) -> set[str]:
    """任務已經記錄過的留言在 GitHub 上的編號，包含被略過的。"""
    cursor = await conn.execute(
        "SELECT json_extract(payload, '$.github_id') AS github_id FROM events"
        " WHERE task_id = ? AND type = ?",
        (task_id, EventType.REVIEW_COMMENT),
    )
    return {row["github_id"] for row in await cursor.fetchall()}


async def record_feedback(
    conn: aiosqlite.Connection, task_id: int, payloads: list[dict[str, Any]]
) -> int:
    """把留言寫成 `review_comment` 事件，已經記過的略過；回傳新增了幾則要處理的留言。"""
    async with _transaction(conn):
        known = await list_feedback_ids(conn, task_id)
        added = 0
        for payload in payloads:
            if payload["github_id"] in known:
                continue
            known.add(payload["github_id"])
            await _insert_event(conn, task_id, None, EventType.REVIEW_COMMENT, payload)
            added += payload.get("ignored") is None
        if added:
            # 讓卡片的指紋跟著變，看板才會更新。
            await conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (utc_now(), task_id))
    return added


async def set_run_input_text(conn: aiosqlite.Connection, run_id: int, input_text: str) -> None:
    """在 agent 啟動前補上這次實際要送給它的內容（例如抓到的 CI log）。"""
    async with _transaction(conn):
        await conn.execute("UPDATE runs SET input_text = ? WHERE id = ?", (input_text, run_id))


async def get_snapshot(conn: aiosqlite.Connection, task_id: int) -> TaskSnapshot:
    """取得任務目前的快照：任務、專案是否要求 review、最後一個 run、累計用量。"""
    task = await get_task(conn, task_id)
    project = await get_project(conn, task.project_id)
    row = await _fetch_one(
        conn, "SELECT * FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    )
    pending = await _fetch_one(
        conn, "SELECT COUNT(*) AS total" + _PENDING_FEEDBACK, (task_id, EventType.REVIEW_COMMENT)
    )
    assert pending is not None
    return TaskSnapshot(
        task=task,
        require_review=project.require_review,
        last_run=None if row is None else _to_run(row),
        usage=await get_task_usage(conn, task_id),
        pending_feedback=int(pending["total"]),
    )


async def list_snapshots(
    conn: aiosqlite.Connection, project_id: int | None = None
) -> list[TaskSnapshot]:
    """列出任務的快照，依任務編號；可限定某一個專案。"""
    query, params = "SELECT id FROM tasks", ()
    if project_id is not None:
        query, params = query + " WHERE project_id = ?", (project_id,)
    cursor = await conn.execute(query + " ORDER BY id", params)
    return [await get_snapshot(conn, row["id"]) for row in await cursor.fetchall()]


async def list_task_fingerprints(conn: aiosqlite.Connection) -> dict[int, tuple[Any, ...]]:
    """每個任務一組值，卡片上任何會顯示的東西有變，這組值就會不同。

    一次查詢涵蓋所有任務，用來找出哪些卡片需要重新推導。
    """
    cursor = await conn.execute(
        "SELECT t.*,"
        " (SELECT MAX(id) FROM runs WHERE task_id = t.id) AS last_run_id,"
        " (SELECT COUNT(*) FROM runs WHERE task_id = t.id AND ended_at IS NOT NULL)"
        " AS ended_runs,"
        " (SELECT require_review FROM projects WHERE id = t.project_id) AS require_review"
        " FROM tasks t"
    )
    return {row["id"]: tuple(row) for row in await cursor.fetchall()}


async def list_tasks_with_open_pr(conn: aiosqlite.Connection) -> list[Task]:
    """列出 PR 開著的任務，也就是 Poller 要查詢的對象。

    已封存的任務也算：封存不會關掉 PR，之後它被合併或關閉時仍然要知道。
    """
    cursor = await conn.execute(
        "SELECT * FROM tasks WHERE pr_number IS NOT NULL AND pr_state = ? ORDER BY id",
        (PrState.OPEN,),
    )
    return [_to_task(row) for row in await cursor.fetchall()]


async def apply_github_state(
    conn: aiosqlite.Connection,
    task_id: int,
    *,
    expected_head_sha: str | None,
    pr_state: str,
    head_sha: str,
    ci_state: str,
    review_state: str,
    merge_state: str = MergeState.UNKNOWN,
) -> bool:
    """把 Poller 查到的 GitHub 狀態當成一組寫入，回傳是否有變化。

    「能不能合併」GitHub 要算一下才知道，剛 push 完或基底分支剛變動時會回報未知。
    同一個 commit 上已經知道答案時，未知不會把它蓋掉，否則這個值會來回跳動。

    兩種情況會丟棄這次結果：查詢期間 `head_sha` 被別人改掉（例如剛 push）；
    或 PR 還開著、GitHub 回報的卻是我們更早 push 過的 commit，表示它的資料還沒跟上最新的 push。
    PR 已經合併或關閉時不做第二項檢查：它的最新 commit 不會再變，停在舊的 commit 是正常的。
    有變化才寫一筆 `ci_status` 事件，沒變就什麼都不寫。
    """
    new_state = {
        "pr_state": pr_state,
        "head_sha": head_sha,
        "ci_state": ci_state,
        "review_state": review_state,
        "merge_state": merge_state,
    }
    async with _transaction(conn):
        task = await get_task(conn, task_id)
        if task.head_sha != expected_head_sha:
            return False
        if merge_state == MergeState.UNKNOWN and head_sha == task.head_sha:
            new_state["merge_state"] = task.merge_state
        is_lagging = (
            pr_state == PrState.OPEN
            and head_sha != task.head_sha
            and await _was_pushed_earlier(conn, task_id, head_sha)
        )
        if is_lagging:
            return False
        if all(getattr(task, name) == value for name, value in new_state.items()):
            return False
        await conn.execute(
            "UPDATE tasks SET pr_state = ?, head_sha = ?, ci_state = ?, review_state = ?,"
            " merge_state = ?, updated_at = ? WHERE id = ?",
            (*new_state.values(), utc_now(), task_id),
        )
        await _insert_event(
            conn, task_id, None, EventType.CI_STATUS, {"pr_number": task.pr_number, **new_state}
        )
    return True


async def get_task(conn: aiosqlite.Connection, task_id: int) -> Task:
    """依主鍵取得任務。"""
    row = await _fetch_one(conn, "SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise NotFoundError(f"task {task_id} not found")
    return _to_task(row)


async def get_run(conn: aiosqlite.Connection, run_id: int) -> Run:
    """依主鍵取得 run。"""
    row = await _fetch_one(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
    if row is None:
        raise NotFoundError(f"run {run_id} not found")
    return _to_run(row)


async def get_open_run(conn: aiosqlite.Connection, task_id: int) -> Run | None:
    """取得任務目前未結束的 run，沒有則回傳 None。"""
    row = await _fetch_one(
        conn, "SELECT * FROM runs WHERE task_id = ? AND ended_at IS NULL", (task_id,)
    )
    return None if row is None else _to_run(row)


async def list_runs(conn: aiosqlite.Connection, task_id: int) -> list[Run]:
    """列出任務的所有 run，依建立順序。"""
    cursor = await conn.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY id", (task_id,))
    return [_to_run(row) for row in await cursor.fetchall()]


async def list_open_runs(conn: aiosqlite.Connection) -> list[Run]:
    """列出所有未結束的 run。"""
    cursor = await conn.execute("SELECT * FROM runs WHERE ended_at IS NULL ORDER BY id")
    return [_to_run(row) for row in await cursor.fetchall()]


async def set_task_session_id(conn: aiosqlite.Connection, task_id: int, session_id: str) -> None:
    """保存任務的 agent session id。"""
    async with _transaction(conn):
        await conn.execute(
            "UPDATE tasks SET session_id = ?, updated_at = ? WHERE id = ?",
            (session_id, utc_now(), task_id),
        )


TASK_UPDATABLE_FIELDS = frozenset(
    {
        "branch",
        "worktree_path",
        "pr_number",
        "pr_state",
        "head_sha",
        "ci_state",
        "review_state",
        "merge_state",
    }
)


async def update_task(conn: aiosqlite.Connection, task_id: int, **fields: Any) -> None:
    """更新任務的 branch、worktree 或 GitHub 狀態快取欄位。"""
    unknown = set(fields) - TASK_UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"task fields cannot be updated: {', '.join(sorted(unknown))}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    async with _transaction(conn):
        await conn.execute(
            # 欄位名稱只會來自上面的白名單，不是外部輸入。
            f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?",
            (*fields.values(), utc_now(), task_id),
        )


async def archive_task(conn: aiosqlite.Connection, task_id: int) -> None:
    """把任務標成已封存。"""
    now = utc_now()
    async with _transaction(conn):
        await conn.execute(
            "UPDATE tasks SET archived_at = ?, updated_at = ? WHERE id = ?", (now, now, task_id)
        )


async def finish_run(
    conn: aiosqlite.Connection,
    run_id: int,
    exit_status: ExitStatus,
    *,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
) -> bool:
    """把 run 結案。已經結案的 run 不會被改寫，回傳是否真的寫入。"""
    async with _transaction(conn):
        cursor = await conn.execute(
            "UPDATE runs SET ended_at = ?, exit_status = ?, tokens_in = ?, tokens_out = ?,"
            " cost_usd = ? WHERE id = ? AND ended_at IS NULL",
            (utc_now(), exit_status, tokens_in, tokens_out, cost_usd, run_id),
        )
    return cursor.rowcount == 1


async def append_event(
    conn: aiosqlite.Connection,
    task_id: int,
    run_id: int | None,
    event_type: EventType | str,
    payload: dict[str, Any],
) -> int:
    """新增一筆事件，回傳它的 id。"""
    async with _transaction(conn):
        return await _insert_event(conn, task_id, run_id, event_type, payload)


async def list_events(
    conn: aiosqlite.Connection, task_id: int, run_id: int | None = None
) -> list[Event]:
    """列出任務的事件，依寫入順序；可限定某一個 run。"""
    query = "SELECT * FROM events WHERE task_id = ?"
    params: tuple[Any, ...] = (task_id,)
    if run_id is not None:
        query += " AND run_id = ?"
        params += (run_id,)
    cursor = await conn.execute(query + " ORDER BY id", params)
    return [_to_event(row) for row in await cursor.fetchall()]


async def list_events_after(
    conn: aiosqlite.Connection,
    after_id: int,
    *,
    limit: int,
    task_id: int | None = None,
    event_type: str | None = None,
) -> list[Event]:
    """列出編號大於 `after_id` 的事件，最多 `limit` 筆；可限定任務與事件型別。

    分頁與「斷線後補上漏掉的事件」共用這個查詢。
    """
    query, params = "SELECT * FROM events WHERE id > ?", [after_id]
    if task_id is not None:
        query, params = query + " AND task_id = ?", [*params, task_id]
    if event_type is not None:
        query, params = query + " AND type = ?", [*params, event_type]
    cursor = await conn.execute(query + " ORDER BY id LIMIT ?", (*params, limit))
    return [_to_event(row) for row in await cursor.fetchall()]


async def get_last_event_id(conn: aiosqlite.Connection) -> int:
    """目前最大的事件編號；還沒有任何事件時是 0。"""
    row = await _fetch_one(conn, "SELECT COALESCE(MAX(id), 0) AS last_id FROM events", ())
    assert row is not None
    return int(row["last_id"])


async def get_task_token_totals(conn: aiosqlite.Connection, task_id: int) -> tuple[int, int]:
    """任務所有 run 的輸入與輸出 token 數各自的總和；未知的 run 不計。"""
    row = await _fetch_one(
        conn,
        "SELECT COALESCE(SUM(tokens_in), 0) AS tokens_in,"
        " COALESCE(SUM(tokens_out), 0) AS tokens_out FROM runs WHERE task_id = ?",
        (task_id,),
    )
    assert row is not None
    return int(row["tokens_in"]), int(row["tokens_out"])


async def get_task_usage(conn: aiosqlite.Connection, task_id: int) -> TaskUsage:
    """加總任務所有 run 的花費與 token 數。"""
    row = await _fetch_one(
        conn,
        "SELECT COALESCE(SUM(cost_usd), 0) AS cost,"
        " COALESCE(SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)), 0) AS tokens,"
        " COALESCE(SUM(ended_at IS NOT NULL AND tokens_in IS NULL), 0) AS unknown_runs"
        " FROM runs WHERE task_id = ?",
        (task_id,),
    )
    assert row is not None
    return TaskUsage(
        cost_usd=float(row["cost"]), tokens=int(row["tokens"]), is_complete=row["unknown_runs"] == 0
    )


async def get_last_usage_raw(conn: aiosqlite.Connection, task_id: int) -> dict[str, Any] | None:
    """取得任務最近一次有紀錄的原始用量，供 adapter 把累計值換算成單輪。"""
    row = await _fetch_one(
        conn,
        "SELECT json_extract(payload, '$.usage_raw') AS usage_raw FROM events"
        " WHERE task_id = ? AND type = ? AND json_extract(payload, '$.usage_raw') IS NOT NULL"
        " ORDER BY id DESC LIMIT 1",
        (task_id, EventType.STATE_CHANGE),
    )
    return None if row is None else json.loads(row["usage_raw"])


async def _was_pushed_earlier(conn: aiosqlite.Connection, task_id: int, head_sha: str) -> bool:
    row = await _fetch_one(
        conn,
        "SELECT 1 FROM events WHERE task_id = ? AND type = ?"
        " AND json_extract(payload, '$.head_sha') = ? LIMIT 1",
        (task_id, EventType.PUSHED, head_sha),
    )
    return row is not None


async def _insert_run(
    conn: aiosqlite.Connection,
    task_id: int,
    trigger: RunTrigger,
    input_text: str,
    source_head_sha: str | None,
) -> int:
    cursor = await conn.execute(
        "INSERT INTO runs (task_id, trigger, input_text, source_head_sha, started_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (task_id, trigger, input_text, source_head_sha, utc_now()),
    )
    return _last_id(cursor)


async def _insert_event(
    conn: aiosqlite.Connection,
    task_id: int,
    run_id: int | None,
    event_type: EventType | str,
    payload: dict[str, Any],
) -> int:
    cursor = await conn.execute(
        "INSERT INTO events (task_id, run_id, ts, type, payload) VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, utc_now(), str(event_type), json.dumps(payload, ensure_ascii=False)),
    )
    return _last_id(cursor)


_TASK_FIELDS = frozenset(field.name for field in fields(Task))


def _to_task(row: aiosqlite.Row) -> Task:
    # 資料庫被比較新的版本開過的話會多出不認得的欄位；略過它們，舊版才不會讀不了。
    return Task(**{name: value for name, value in dict(row).items() if name in _TASK_FIELDS})


def _to_project(row: aiosqlite.Row) -> Project:
    data = dict(row)
    data["require_review"] = bool(data["require_review"])
    return Project(**data)


def _to_run(row: aiosqlite.Row) -> Run:
    data = dict(row)
    data["trigger"] = RunTrigger(data["trigger"])
    data["exit_status"] = None if data["exit_status"] is None else ExitStatus(data["exit_status"])
    return Run(**data)


def _to_event(row: aiosqlite.Row) -> Event:
    data = dict(row)
    data["payload"] = json.loads(data["payload"])
    return Event(**data)


async def _fetch_one(
    conn: aiosqlite.Connection, query: str, params: tuple[Any, ...]
) -> aiosqlite.Row | None:
    cursor = await conn.execute(query, params)
    return await cursor.fetchone()


def _last_id(cursor: aiosqlite.Cursor) -> int:
    row_id = cursor.lastrowid
    if row_id is None:
        raise RuntimeError("insert did not produce a row id")
    return row_id
