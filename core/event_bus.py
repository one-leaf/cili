"""事件总线 - 跨会话/线程的轻量级 pub-sub，供全局 SSE 事件流订阅。

worker 子 agent 消息与工具输出实时事件由后台线程 publish，前端通过
GET /api/events 的全局 SSE 流消费。总线只做广播与过滤，不持久化；
断线期间丢失的事件由前端"重连/展开时全量重拉"兜底（见 web/static/sse-client.js）。
"""

from __future__ import annotations

import queue
import threading
from typing import Any

# 订阅者积压上限：publish 用 put_nowait，满则静默丢弃，绝不阻塞发布线程
_QUEUE_MAX = 512


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # queue.Queue -> (workspace_uuid 过滤, session_id 过滤)；以 queue 为 key
        # 便于 unsubscribe 精确移除；None 表示不过滤该维度
        self._subscribers: dict[queue.Queue, tuple[str | None, str | None]] = {}

    def subscribe(
        self,
        q: queue.Queue,
        workspace_uuid: str | None = None,
        session_id: str | None = None,
    ) -> queue.Queue:
        with self._lock:
            self._subscribers[q] = (workspace_uuid, session_id)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.pop(q, None)

    def publish(self, event: dict[str, Any]) -> None:
        ws = event.get("workspace_uuid")
        sid = event.get("session_id")
        with self._lock:
            items = list(self._subscribers.items())
        if not items:
            return
        for q, (sub_ws, sub_sid) in items:
            if sub_ws is not None and sub_ws != ws:
                continue
            if sub_sid is not None and sub_sid != sid:
                continue
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # 订阅者积压，静默丢弃


_event_bus = EventBus()


def get_event_bus() -> EventBus:
    return _event_bus
