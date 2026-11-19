"""把資料庫裡的變化推送給所有開著的 SSE 連線。

訊息有兩種：

- `event`：`events` 表的一列，帶著它的編號。斷線重連時可以從某個編號之後補送。
- `task`：某個任務的卡片資料變了。不補送；重連的一方自己重新抓一次看板。

不是每個會讓卡片移動的寫入都有事件列（例如建立 run），所以卡片的變化另外偵測：
資料庫每次 commit 之後比對每個任務的指紋，有變才重新推導那張卡片。
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
# 一個 agent 動作會連續寫好幾筆；稍等一下再一起處理，不用每次 commit 都查一輪。
SETTLE_SECONDS = 0.05
NEEDS_YOU = str(status.Column.NEEDS_YOU)
logger = logging.getLogger("agent_supervisor.feed")

CardListener = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class StreamMessage:
    """推送給瀏覽器的一則訊息。只有 `event` 訊息有編號。"""

    kind: str
    data: dict[str, Any]
    event_id: int | None = None

    def encode(self) -> str:
        """排成 SSE 的文字格式。"""
        lines = [] if self.event_id is None else [f"id: {self.event_id}"]
        lines += [f"event: {self.kind}", f"data: {json.dumps(self.data, ensure_ascii=False)}"]
        return "\n".join(lines) + "\n\n"


@dataclass(eq=False)
class Subscription:
    """一條連線等著送出的訊息。積太多表示對方讀得太慢，標成溢出後由連線自己結束。"""

    queue: asyncio.Queue[StreamMessage] = field(
        default_factory=lambda: asyncio.Queue(MAX_PENDING_MESSAGES)
    )
    is_overflowed: bool = False


class ChangeFeed:
    """偵測資料庫的變化並分送給訂閱者。"""

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
        """記下目前的狀態當作起點，之後的變化才會被推送。

        每張卡片目前在哪一欄也記下來：原本就在 Needs you 的卡片不算「剛進去」。
        """
        async with db.consistent_read(self._conn):
            self._last_event_id = await db.get_last_event_id(self._conn)
            self._fingerprints = await db.list_task_fingerprints(self._conn)
            for task_id in self._fingerprints:
                self._columns[task_id] = (await self.card(task_id))["column"]
        db.on_commit(self._conn, self._changed.set)

    async def run(self) -> None:
        """持續把變化推送出去，直到被取消。"""
        while True:
            await self._changed.wait()
            await asyncio.sleep(SETTLE_SECONDS)
            self._changed.clear()
            try:
                await self.publish_changes()
            except Exception:
                # 這個工作無聲地停掉的話，畫面就不再更新，而 API 看起來一切正常。
                logger.exception("publishing changes failed; trying again on the next change")

    async def publish_changes(self) -> None:
        """把上次之後新增的事件與有變化的卡片推送出去。"""
        async with db.consistent_read(self._conn):
            events = await self._new_events()
            fingerprints = await db.list_task_fingerprints(self._conn)
            changed = [
                task_id
                for task_id, fingerprint in fingerprints.items()
                if self._fingerprints.get(task_id) != fingerprint
            ]
            cards = [await self.card(task_id) for task_id in changed]
            # 全部讀成功才往前推進；中途失敗的話，下一次會從同一個地方重來。
            self._fingerprints = fingerprints
            if events:
                self._last_event_id = events[-1].id
        for event in events:
            self._publish(StreamMessage(EVENT, views.event_view(event), event.id))
        for card in cards:
            self._publish(StreamMessage(TASK, card))
            self._track_column(card)

    def _track_column(self, card: dict[str, Any]) -> None:
        """卡片從別的欄位進到 Needs you 時通知一次；停在裡面不重複通知。"""
        previous = self._columns.get(card["id"])
        self._columns[card["id"]] = card["column"]
        if card["column"] != NEEDS_YOU or previous == NEEDS_YOU or self._on_needs_you is None:
            return
        try:
            self._on_needs_you(card)
        except Exception:
            # 通知是順便的，出錯不能讓推送停下來。
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
        """推導一張卡片目前的內容。呼叫端要自己確保讀取的一致性。"""
        snapshot = await db.get_snapshot(self._conn, task_id)
        return views.card_view(
            snapshot,
            status.board_status(snapshot, self._supervisor.limits),
            reports_cost=self._supervisor.reports_cost(snapshot.task.agent_type),
        )

    async def replay(self, after_id: int) -> list[StreamMessage]:
        """列出編號大於 `after_id`、而且已經推送過的事件，給斷線重連的一方補上。"""
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
        """開始接收之後的訊息。"""
        subscription = Subscription()
        self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """不再接收訊息。"""
        self._subscriptions.discard(subscription)

    def _publish(self, message: StreamMessage) -> None:
        for subscription in self._subscriptions:
            if subscription.is_overflowed:
                continue
            try:
                subscription.queue.put_nowait(message)
            except asyncio.QueueFull:
                # 讀得太慢的連線不能拖住其他人：放棄它，讓對方重連後用編號補上。
                subscription.is_overflowed = True


async def stream_text(
    feed: ChangeFeed, last_event_id: int | None, is_closing: Callable[[], bool]
) -> AsyncIterator[str]:
    """一條 SSE 連線要送出的文字。

    先訂閱再補送：補送期間新發生的事件已經在佇列裡，之後照編號略過重複的。
    daemon 要關閉或這條連線積了太多訊息時結束；瀏覽器會自己重連。
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
