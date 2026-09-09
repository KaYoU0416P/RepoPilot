"""进程内事件总线，给 SSE 用。

已知局限（面试要主动说）：这是**进程内**的。如果 API 和 worker 拆成两个进程，
订阅者就收不到别的进程发的事件。真要拆，换 Redis pub/sub 或 PG 的 LISTEN/NOTIFY。
现在 API + worker 同进程，够用，且不影响业务正确性 —— 事件只是实时展示，
真相始终在数据库里（客户端断线后轮询 GET /runs/{id} 拿到的结果是一样的）。
"""

import asyncio
from collections import defaultdict
from uuid import UUID

from repopilot.api.schemas import RunEvent
from repopilot.observability import get_logger

log = get_logger(__name__)

DONE = object()  # 流结束的哨兵


class EventBus:
    def __init__(self, replay_size: int = 200) -> None:
        self._subscribers: dict[UUID, list[asyncio.Queue]] = defaultdict(list)
        self._replay: dict[UUID, list[RunEvent]] = defaultdict(list)
        self._replay_size = replay_size

    def publish(self, run_id: UUID, event: RunEvent) -> None:
        buffer = self._replay[run_id]
        buffer.append(event)
        if len(buffer) > self._replay_size:
            del buffer[0]
        for queue in self._subscribers[run_id]:
            queue.put_nowait(event)

    def subscribe(self, run_id: UUID, *, replay: bool = True) -> asyncio.Queue:
        """晚到的订阅者先收历史事件，再收实时事件，不会漏掉开头。"""
        queue: asyncio.Queue = asyncio.Queue()
        if replay:
            for event in self._replay[run_id]:
                queue.put_nowait(event)
        self._subscribers[run_id].append(queue)
        return queue

    def unsubscribe(self, run_id: UUID, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(run_id)
        if subs and queue in subs:
            subs.remove(queue)
        if subs is not None and not subs:
            del self._subscribers[run_id]

    def close(self, run_id: UUID) -> None:
        for queue in self._subscribers.get(run_id, []):
            queue.put_nowait(DONE)

    def forget(self, run_id: UUID) -> None:
        self._replay.pop(run_id, None)
