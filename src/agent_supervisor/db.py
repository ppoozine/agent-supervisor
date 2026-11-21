"""SQLite schema and queries. No ORM.

Invariants are enforced by the database itself:
- At most one unfinished run per task at a time (partial unique index)
- `events` can only be inserted, never updated or deleted (triggers)

All modules share one connection, so every write must first take that connection's write lock:
otherwise one side's commit would cut through another side's half-done transaction.
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

# Columns added later: (table, column, definition). Added to older databases at startup.
# When adding a column, write it both here and in SCHEMA above.
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
    """The queried row does not exist."""


class RunAlreadyActiveError(RuntimeError):
    """The task already has an unfinished run."""


class TaskNotRunnableError(RuntimeError):
    """The task is archived, or its PR is merged or closed, so no run can be created."""


@dataclass(frozen=True)
class Project:
    """A registered local repo."""

    id: int
    name: str
    repo_path: str
    github_repo: str | None
    default_branch: str
    require_review: bool


@dataclass(frozen=True)
class Task:
    """A task, plus the Poller's cache of its GitHub state."""

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
    """One execution of a task."""

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
    """An audit record."""

    id: int
    task_id: int
    run_id: int | None
    ts: str
    type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class TaskUsage:
    """A task's total cost and tokens. `is_complete` false means some run's usage is unknown."""

    cost_usd: float
    tokens: int
    is_complete: bool


@dataclass(frozen=True)
class TaskSnapshot:
    """All the data needed to derive the board column and auto-fix conditions."""

    task: Task
    require_review: bool
    last_run: Run | None
    usage: TaskUsage
    pending_feedback: int = 0
    """Number of PR comments not yet fed back to the agent."""

    @property
    def has_open_run(self) -> bool:
        """Whether the task has a running run. An unfinished run is always the last one."""
        return self.last_run is not None and self.last_run.ended_at is None


@asynccontextmanager
async def _transaction(conn: aiosqlite.Connection) -> AsyncIterator[None]:
    """Run a transaction under the write lock: commit on normal exit, otherwise roll back."""
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
    """Register a function this connection calls after every transaction it commits.

    All writes go through the same kind of transaction, so this is the single notification point for
    "data changed".
    """
    _commit_listeners.setdefault(conn, []).append(listener)


@asynccontextmanager
async def consistent_read(conn: aiosqlite.Connection) -> AsyncIterator[None]:
    """Reads inside the block do not see other half-done transactions.

    All modules share one connection, and a connection can read its own uncommitted writes. Reads
    whose results go outside (API responses, events pushed to the browser) must all be wrapped in
    this. Not usable inside a transaction.
    """
    async with _write_locks.setdefault(conn, asyncio.Lock()):
        yield


def utc_now() -> str:
    """The current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


async def connect(db_path: str | Path) -> aiosqlite.Connection:
    """Open a DB connection and apply the required pragmas; create the DB directory if missing."""
    await asyncio.to_thread(Path(db_path).parent.mkdir, parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    return conn


async def init_schema(conn: aiosqlite.Connection) -> None:
    """Create tables, indexes, and triggers if missing, and add later columns to older databases."""
    await conn.executescript(SCHEMA)
    for table, column, definition in ADDED_COLUMNS:
        # Table and column names only come from the constants above, never from external input.
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
    """Register a local repo; if already registered, update its settings."""
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
    """List all registered projects by ID."""
    cursor = await conn.execute("SELECT * FROM projects ORDER BY id")
    return [_to_project(row) for row in await cursor.fetchall()]


async def get_project_by_path(conn: aiosqlite.Connection, repo_path: str) -> Project | None:
    """Get a project by repo path; None if it is not registered."""
    row = await _fetch_one(conn, "SELECT * FROM projects WHERE repo_path = ?", (repo_path,))
    return None if row is None else _to_project(row)


async def get_project(conn: aiosqlite.Connection, project_id: int) -> Project:
    """Get a project by primary key."""
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
    """Create a task and its initial run in one transaction."""
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
    """Create a run for an existing task; refuse if it cannot run or already has an unfinished run.

    A user message means a person has stepped in, so the same transaction resets the auto-fix and PR
    comment attempt counts to zero.
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
    """If the conditions hold, create a ci_failure run, record the handled commit, add a fix.

    Rechecking the conditions, creating the run, and updating both loop guard columns all happen in
    one transaction;
    if the conditions don't hold, nothing is written and None is returned.
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
    """If the conditions hold, create a merge_conflict run, record the handled commit, add a fix.

    Same approach as `create_ci_failure_run`, but with its own "handled" column:
    one commit may have both a conflict and a CI failure, and neither may overwrite the other. The
    fix count is shared.
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
    """If the conditions hold, create a review_feedback run with all unhandled PR comments.

    Reading the comments, creating the run, advancing "handled up to which comment", and
    incrementing the count all happen in one transaction, so no comment is fed back twice and none
    is skipped in between.
    If the conditions don't hold, nothing is written and None is returned.
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
    """PR comments not yet fed back, in recorded order; skipped ones (e.g. from bots) excluded."""
    cursor = await conn.execute(
        "SELECT e.*" + _PENDING_FEEDBACK + " ORDER BY e.id", (task_id, EventType.REVIEW_COMMENT)
    )
    return [_to_event(row) for row in await cursor.fetchall()]


async def list_feedback_ids(conn: aiosqlite.Connection, task_id: int) -> set[str]:
    """GitHub IDs of the PR comments the task has already recorded, including skipped ones."""
    cursor = await conn.execute(
        "SELECT json_extract(payload, '$.github_id') AS github_id FROM events"
        " WHERE task_id = ? AND type = ?",
        (task_id, EventType.REVIEW_COMMENT),
    )
    return {row["github_id"] for row in await cursor.fetchall()}


async def record_feedback(
    conn: aiosqlite.Connection, task_id: int, payloads: list[dict[str, Any]]
) -> int:
    """Write PR comments as `review_comment` events, skipping known ones; return the new count."""
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
            # So the card's fingerprint changes too and the board gets updated.
            await conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (utc_now(), task_id))
    return added


async def set_run_input_text(conn: aiosqlite.Connection, run_id: int, input_text: str) -> None:
    """Before the agent starts, fill in what is actually sent to it (e.g. the fetched CI log)."""
    async with _transaction(conn):
        await conn.execute("UPDATE runs SET input_text = ? WHERE id = ?", (input_text, run_id))


async def get_snapshot(conn: aiosqlite.Connection, task_id: int) -> TaskSnapshot:
    """Get the task snapshot: task, whether the project requires review, last run, usage."""
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
    """List task snapshots by task ID; optionally limited to one project."""
    query, params = "SELECT id FROM tasks", ()
    if project_id is not None:
        query, params = query + " WHERE project_id = ?", (project_id,)
    cursor = await conn.execute(query + " ORDER BY id", params)
    return [await get_snapshot(conn, row["id"]) for row in await cursor.fetchall()]


async def list_task_fingerprints(conn: aiosqlite.Connection) -> dict[int, tuple[Any, ...]]:
    """One set of values per task that changes whenever anything shown on the card changes.

    One query covers all tasks, and is used to find which cards need re-deriving.
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
    """List tasks with an open PR, i.e. what the Poller queries.

    Archived tasks count too: archiving does not close the PR, and we still need to know when it is
    later merged or closed.
    """
    cursor = await conn.execute(
        "SELECT * FROM tasks WHERE pr_number IS NOT NULL AND pr_state = ? ORDER BY id",
        (PrState.OPEN,),
    )
    return [_to_task(row) for row in await cursor.fetchall()]


async def list_recently_closed_tasks(conn: aiosqlite.Connection, since: str) -> list[Task]:
    """List unarchived tasks whose PR is closed, updated at or after `since` (ISO 8601 UTC).

    A closed PR can be reopened on GitHub; a merged one cannot, so merged PRs are not listed.
    """
    cursor = await conn.execute(
        "SELECT * FROM tasks WHERE pr_number IS NOT NULL AND pr_state = ?"
        " AND archived_at IS NULL AND updated_at >= ? ORDER BY id",
        (PrState.CLOSED, since),
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
    """Write the GitHub state fetched by the Poller as one set; return whether anything changed.

    GitHub needs time to compute mergeability and reports unknown right after a push or a base
    branch change.
    Once the answer is known for the same commit, unknown does not overwrite it; otherwise the value
    would flip back and forth.

    The result is discarded in two cases: `head_sha` was changed by someone else during the query
    (e.g. a fresh push);
    or the PR is still open but GitHub reports a commit we pushed earlier, meaning its data has not
    caught up with the latest push.
    The second check is skipped once the PR is merged or closed: its latest commit will not change
    any more, so staying on an old commit is normal.
    A `ci_status` event is written only when something changed; otherwise nothing is written.
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
    """Get a task by primary key."""
    row = await _fetch_one(conn, "SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise NotFoundError(f"task {task_id} not found")
    return _to_task(row)


async def get_run(conn: aiosqlite.Connection, run_id: int) -> Run:
    """Get a run by primary key."""
    row = await _fetch_one(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
    if row is None:
        raise NotFoundError(f"run {run_id} not found")
    return _to_run(row)


async def get_open_run(conn: aiosqlite.Connection, task_id: int) -> Run | None:
    """Get the task's current unfinished run; None if there is none."""
    row = await _fetch_one(
        conn, "SELECT * FROM runs WHERE task_id = ? AND ended_at IS NULL", (task_id,)
    )
    return None if row is None else _to_run(row)


async def list_runs(conn: aiosqlite.Connection, task_id: int) -> list[Run]:
    """List all of the task's runs in creation order."""
    cursor = await conn.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY id", (task_id,))
    return [_to_run(row) for row in await cursor.fetchall()]


async def list_open_runs(conn: aiosqlite.Connection) -> list[Run]:
    """List all unfinished runs."""
    cursor = await conn.execute("SELECT * FROM runs WHERE ended_at IS NULL ORDER BY id")
    return [_to_run(row) for row in await cursor.fetchall()]


async def set_task_session_id(conn: aiosqlite.Connection, task_id: int, session_id: str) -> None:
    """Save the task's agent session id."""
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
    """Update the task's branch, worktree, or cached GitHub state columns."""
    unknown = set(fields) - TASK_UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"task fields cannot be updated: {', '.join(sorted(unknown))}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    async with _transaction(conn):
        await conn.execute(
            # Column names only come from the allowlist above, never from external input.
            f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?",
            (*fields.values(), utc_now(), task_id),
        )


async def archive_task(conn: aiosqlite.Connection, task_id: int) -> None:
    """Mark the task archived."""
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
    """Finish the run. A finished run is never rewritten; return whether a write happened."""
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
    """Insert an event and return its id."""
    async with _transaction(conn):
        return await _insert_event(conn, task_id, run_id, event_type, payload)


async def list_events(
    conn: aiosqlite.Connection, task_id: int, run_id: int | None = None
) -> list[Event]:
    """List the task's events in insertion order; optionally limited to one run."""
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
    """List events with ID above `after_id`, at most `limit`; optionally by task and event type.

    Pagination and "catch up on events missed while disconnected" share this query.
    """
    query, params = "SELECT * FROM events WHERE id > ?", [after_id]
    if task_id is not None:
        query, params = query + " AND task_id = ?", [*params, task_id]
    if event_type is not None:
        query, params = query + " AND type = ?", [*params, event_type]
    cursor = await conn.execute(query + " ORDER BY id LIMIT ?", (*params, limit))
    return [_to_event(row) for row in await cursor.fetchall()]


async def get_last_event_id(conn: aiosqlite.Connection) -> int:
    """The largest event ID so far; 0 if there are no events yet."""
    row = await _fetch_one(conn, "SELECT COALESCE(MAX(id), 0) AS last_id FROM events", ())
    assert row is not None
    return int(row["last_id"])


async def get_task_token_totals(conn: aiosqlite.Connection, task_id: int) -> tuple[int, int]:
    """Total input and output tokens across the task's runs; runs with unknown usage are skipped."""
    row = await _fetch_one(
        conn,
        "SELECT COALESCE(SUM(tokens_in), 0) AS tokens_in,"
        " COALESCE(SUM(tokens_out), 0) AS tokens_out FROM runs WHERE task_id = ?",
        (task_id,),
    )
    assert row is not None
    return int(row["tokens_in"]), int(row["tokens_out"])


async def get_task_usage(conn: aiosqlite.Connection, task_id: int) -> TaskUsage:
    """Sum the cost and token counts of all the task's runs."""
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
    """Get the task's latest raw usage, so the adapter can turn cumulative values into per-turn."""
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
    # A DB opened by a newer version has extra unknown columns; skip them so older versions can
    # still read it.
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
