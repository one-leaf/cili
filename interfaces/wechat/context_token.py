"""微信 context_token 台账：按用户维护发送预算与最近 token。"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _TokenEntry:
    token: str = ""
    received_at: float = 0.0     # 收到该 token 的时刻
    refreshed_at: float = 0.0    # 最近一次刷新时刻（刷新后重新计时）
    used: int = 0                # 已用该 token 发了几条


class ContextTokenStore:
    """按用户维护 context_token 的有效期、已用条数与剩余预算。

    配额是**服务端未公开**的经验值（约 8 条/上下文），所以这里做本地预算兜底：
    超预算就本地直接拒（不打 API），由 ``outbound.py`` 转成 defer。等 API 报错才
    知道超了的话，那条消息已经白发一次了。

    实例挂在各自账号的 runtime 上（多账号天然隔离），键是 user_id。
    """

    def __init__(self, *, max_age_seconds: float, budget: int) -> None:
        self._max_age = max_age_seconds
        self._budget = budget
        self._entries: dict[str, _TokenEntry] = {}

    def update(self, user_id: str, token: str, *, now: float | None = None) -> None:
        """记下用户最新带来的 context_token。

        token 换了 → 换一条新台账（配额计数归零）；token 没换 → 只刷新到达时间，
        **不重置已用计数**（配额是按 context_token 算的，不是按消息算的）。
        """
        if not user_id or not token:
            return
        ts = time.time() if now is None else now
        entry = self._entries.get(user_id)
        if entry is None or entry.token != token:
            self._entries[user_id] = _TokenEntry(token=token, received_at=ts, refreshed_at=ts)
        else:
            entry.received_at = ts

    def token(self, user_id: str) -> str:
        entry = self._entries.get(user_id)
        return entry.token if entry else ""

    def needs_refresh(self, user_id: str, *, now: float | None = None) -> bool:
        """token 是否老到该先刷一次（服务端 ~90–160s 失效，阈值取 60s）。"""
        entry = self._entries.get(user_id)
        if entry is None or not entry.token:
            return False
        ts = time.time() if now is None else now
        return (ts - entry.refreshed_at) >= self._max_age

    def mark_refreshed(self, user_id: str, token: str, *, now: float | None = None) -> None:
        """``getconfig`` 拿回新 token 后调用（换 token 不重置已用计数）。"""
        ts = time.time() if now is None else now
        entry = self._entries.get(user_id)
        if entry is None:
            self._entries[user_id] = _TokenEntry(token=token, received_at=ts, refreshed_at=ts)
            return
        if token and token != entry.token:
            entry.token = token
        entry.refreshed_at = ts

    def budget_left(self, user_id: str, *, reserve: int = 0) -> int:
        """还剩几条可发（``reserve`` 是留给后续步骤的余量）。"""
        entry = self._entries.get(user_id)
        used = entry.used if entry else 0
        return max(0, self._budget - used - reserve)

    def can_send(self, user_id: str, *, reserve: int = 0) -> bool:
        return self.budget_left(user_id, reserve=reserve) > 0

    def record_send(self, user_id: str) -> None:
        """成功发出一条后计数。"""
        entry = self._entries.get(user_id)
        if entry is None:
            self._entries[user_id] = _TokenEntry(used=1)
        else:
            entry.used += 1

    def drop(self, user_id: str) -> None:
        self._entries.pop(user_id, None)
