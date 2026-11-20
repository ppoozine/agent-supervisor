"""Push database changes to every open SSE connection.

There are two kinds of messages:

- `event`: a row of the `events` table, carrying its id. After a reconnect, the events after a
  given id can be replayed.
- `task`: a task's card data changed. Not replayed; the reconnecting side refetches the board
  itself.

Not every write that moves a card has an event row (creating a run, for example), so card
changes are detected separately: after every database commit, compare each task's fingerprint
and re-derive a card only when it changed.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from agent_supervisor import db, status
from agent_supervisor.api import views
from agent_supervisor.supervisor import Supervisor

EVENT, TASK = "event", "task"
PAGE_SIZE = 500
MAX_PENDING_MESSAGES = 2000
IDLE_CHECK_SECONDS = 1.0
PING_EVERY_IDLE_CHECKS = 15
# One agent action writes several rows in a row; wait briefly and handle them together
# instead of querying after every commit.
SETTLE_SECONDS = 0.05
NEEDS_YOU = str(status.Column.NEEDS_YOU)
logger = logging.getLogger("agent_supervisor.feed")

CardListener = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class StreamMessage:
    """One message pushed to the browser. Only `event` messages have an id."""

    kind: str
    data: dict[str, Any]
    event_id: int | None = None

    def encode(self) -> str:
        """Format as SSE text."""
        lines = [] if self.event_id is None else [f"id: {self.event_id}"]
        lines += [f"event: {self.kind}", f"data: {json.dumps(self.data, ensure_ascii=False)}"]
        return "\n".join(lines) + "\n\n"


@dataclass(eq=False)
class Subscription:
    """Messages waiting to be sent on one connection. Too many means a slow reader: mark it
    overflowed and let the connection end itself."""

    queue: asyncio.Queue[StreamMessage] = field(
        default_factory=lambda: asyncio.Queue(MAX_PENDING_MESSAGES)
    )
    is_overflowed: bool = False


class ChangeFeed:
    """Detect database changes and distribute them to subscribers."""

    def __init__(self, supervisor: Supervisor, *, on_needs_you: CardListener | None = None) -> None:
        self._supervisor = supervisor
        self._conn = supervisor.conn
        self._subscriptions: set[Subscription] = set()
        self._changed = asyncio.Event()
        self._last_event_id = 0
        self._fingerprints: dict[int, tuple[Any, ...]] = {}
        self._on_needs_you = on_needs_you
        self._columns: dict[int, str] = {}

    async def start(self) -> None:
        """Record the current state as the starting point; only later changes are pushed.

        Also record each card's current column: a card already in Needs you does not count as
        "just entered".
        """
        async with db.consistent_read(self._conn):
            self._last_event_id = await db.get_last_event_id(self._conn)
            self._fingerprints = await db.list_task_fingerprints(self._conn)
            for task_id in self._fingerprints:
                self._columns[task_id] = (await self.card(task_id))["column"]
        db.on_commit(self._conn, self._changed.set)

    async def run(self) -> None:
        """Keep pushing changes until cancelled."""
        while True:
            await self._changed.wait()
            await asyncio.sleep(SETTLE_SECONDS)
            self._changed.clear()
            try:
                await self.publish_changes()
            except Exception:
                # If this job dies silently, the UI stops updating while the API looks fine.
                logger.exception("publishing changes failed; trying again on the next change")

    async def publish_changes(self) -> None:
        """Push events added since last time and cards that changed."""
        async with db.consistent_read(self._conn):
            events = await self._new_events()
            fingerprints = await db.list_task_fingerprints(self._conn)
            changed = [
                task_id
                for task_id, fingerprint in fingerprints.items()
                if self._fingerprints.get(task_id) != fingerprint
            ]
            cards = [await self.card(task_id) for task_id in changed]
            # Only advance once everything was read; if it fails midway, the next try restarts here.
            self._fingerprints = fingerprints
            if events:
                self._last_event_id = events[-1].id
        for event in events:
            self._publish(StreamMessage(EVENT, views.event_view(event), event.id))
        for card in cards:
            self._publish(StreamMessage(TASK, card))
            self._track_column(card)

    def _track_column(self, card: dict[str, Any]) -> None:
        """Notify once when a card enters Needs you from another column; not while it stays."""
        previous = self._columns.get(card["id"])
        self._columns[card["id"]] = card["column"]
        if card["column"] != NEEDS_YOU or previous == NEEDS_YOU or self._on_needs_you is None:
            return
        try:
            self._on_needs_you(card)
        except Exception:
            # Notifications are a side feature; a failure must not stop pushing.
            logger.exception("the needs-you listener failed for task %s", card["id"])

    async def _new_events(self) -> list[db.Event]:
        events: list[db.Event] = []
        last_id = self._last_event_id
        while True:
            page = await db.list_events_after(self._conn, last_id, limit=PAGE_SIZE)
            events += page
            if len(page) < PAGE_SIZE:
                return events
            last_id = page[-1].id

    async def card(self, task_id: int) -> dict[str, Any]:
        """Derive a card's current content. The caller must ensure read consistency."""
        snapshot = await db.get_snapshot(self._conn, task_id)
        return views.card_view(
            snapshot,
            status.board_status(snapshot, self._supervisor.limits),
            reports_cost=self._supervisor.reports_cost(snapshot.task.agent_type),
        )

    async def replay(self, after_id: int) -> list[StreamMessage]:
        """List already-pushed events with ids above `after_id`, to replay after a reconnect."""
        messages: list[StreamMessage] = []
        async with db.consistent_read(self._conn):
            last_id = after_id
            while last_id < self._last_event_id:
                page = await db.list_events_after(self._conn, last_id, limit=PAGE_SIZE)
                page = [event for event in page if event.id <= self._last_event_id]
                if not page:
                    break
                messages += [
                    StreamMessage(EVENT, views.event_view(event), event.id) for event in page
                ]
                last_id = page[-1].id
        return messages

    def subscribe(self) -> Subscription:
        """Start receiving subsequent messages."""
        subscription = Subscription()
        self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Stop receiving messages."""
        self._subscriptions.discard(subscription)

    def _publish(self, message: StreamMessage) -> None:
        for subscription in self._subscriptions:
            if subscription.is_overflowed:
                continue
            try:
                subscription.queue.put_nowait(message)
            except asyncio.QueueFull:
                # A slow connection must not hold up the others: drop it, and let it reconnect
                # and catch up by id.
                subscription.is_overflowed = True


async def stream_text(
    feed: ChangeFeed, last_event_id: int | None, is_closing: Callable[[], bool]
) -> AsyncIterator[str]:
    """The text one SSE connection sends.

    Subscribe first, then replay: events that occur during the replay are already in the
    queue, and duplicates are skipped by id afterwards. Ends when the daemon is shutting down
    or this connection has too many pending messages; the browser reconnects by itself.
    """
    subscription = feed.subscribe()
    try:
        yield ": connected\n\n"
        replayed_up_to = 0
        if last_event_id is not None:
            for message in await feed.replay(last_event_id):
                assert message.event_id is not None
                replayed_up_to = message.event_id
                yield message.encode()
        idle_checks = 0
        while not is_closing() and not subscription.is_overflowed:
            try:
                message = await asyncio.wait_for(subscription.queue.get(), IDLE_CHECK_SECONDS)
            except TimeoutError:
                idle_checks += 1
                if idle_checks % PING_EVERY_IDLE_CHECKS == 0:
                    yield ": ping\n\n"
                continue
            if message.event_id is not None and message.event_id <= replayed_up_to:
                continue
            yield message.encode()
    finally:
        feed.unsubscribe(subscription)
