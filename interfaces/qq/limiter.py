"""被动回复配额管理。

QQ 单聊**每条用户消息最多被动回复 4 次**，窗口 60 分钟（官方「频率与时效规则」
表）。超出后必须降级为**主动消息**（配额宽松得多：20/qpm、1000 条/用户/天），
而不是丢弃输出。

参数照搬官方 TS 插件的 ``outbound/reply-limiter.ts``（limit=4、ttl=1h、
LRU 上限 10000）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

PASSIVE_LIMIT = 4
PASSIVE_TTL_SECONDS = 3600.0     # 官方文档：被动消息有效期 60 分钟
MAX_TRACKED = 10_000


@dataclass(frozen=True)
class LimitCheck:
    """一次配额检查的结果。"""

    allowed: bool
    remaining: int
    fallback_reason: str | None = None   # None / "expired" / "limit_exceeded"

    @property
    def should_fallback_to_proactive(self) -> bool:
        return not self.allowed


@dataclass
class _Tracked:
    count: int
    first_seen: float


class ReplyLimiter:
    """按 ``msg_id`` 跟踪被动回复次数（线程安全）。"""

    def __init__(
        self,
        limit: int = PASSIVE_LIMIT,
        ttl_seconds: float = PASSIVE_TTL_SECONDS,
        max_tracked: int = MAX_TRACKED,
    ) -> None:
        self.limit = limit
        self.ttl = ttl_seconds
        self.max_tracked = max_tracked
        self._messages: dict[str, _Tracked] = {}
        self._lock = threading.Lock()

    def check(self, msg_id: str) -> LimitCheck:
        """查询还能否对 ``msg_id`` 做被动回复（不消耗配额）。"""
        if not msg_id:
            # 没有 msg_id（如主动推送场景）→ 不走被动通道
            return LimitCheck(allowed=False, remaining=0, fallback_reason="no_msg_id")
        with self._lock:
            tracked = self._messages.get(msg_id)
            if tracked is None:
                return LimitCheck(allowed=True, remaining=self.limit)
            if time.time() - tracked.first_seen >= self.ttl:
                self._messages.pop(msg_id, None)
                return LimitCheck(allowed=False, remaining=0, fallback_reason="expired")
            remaining = max(0, self.limit - tracked.count)
            if remaining <= 0:
                return LimitCheck(allowed=False, remaining=0, fallback_reason="limit_exceeded")
            return LimitCheck(allowed=True, remaining=remaining)

    def record(self, msg_id: str) -> None:
        """记一次被动回复。"""
        if not msg_id:
            return
        now = time.time()
        with self._lock:
            tracked = self._messages.get(msg_id)
            if tracked is not None:
                tracked.count += 1
                return
            if len(self._messages) >= self.max_tracked:
                # dict 保持插入序 → 弹出最旧的一条（LRU 近似）
                oldest = next(iter(self._messages), None)
                if oldest is not None:
                    self._messages.pop(oldest, None)
            self._messages[msg_id] = _Tracked(count=1, first_seen=now)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "tracked_messages": len(self._messages),
                "total_replies": sum(t.count for t in self._messages.values()),
            }

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()
