"""Web 适配器：进程内事件总线 + SSE 格式化。

心跳发消息时往总线 publish，所有连着的浏览器都会收到。
单进程、单用户，队列上限 64 条足够；满了丢最旧的，不阻塞心跳。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def publish(self, event: str, data: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                # 客户端掉线了，丢掉最旧的一条，别让心跳卡住
                try:
                    queue.get_nowait()
                    queue.put_nowait((event, data))
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    @staticmethod
    def format(event: str, data: dict[str, Any]) -> str:
        payload = json.dumps(data, ensure_ascii=False)
        return f"event: {event}\ndata: {payload}\n\n"

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
