"""微信接入端的账号状态：登录凭证 + 长轮询游标 + context_token 台账。

**凭证与配置分开**：``setting.json`` 的 ``wechat`` 段放可编辑的参数（见 config.py），
这里放**扫码换来的**东西——``bot_token`` 等。分开的直接好处是 Web 配置接口天然
不泄露凭证（它只读写 setting.json）。

**为什么必须持久化 ``get_updates_buf``**：它是长轮询游标，丢了就会把服务端积压的
消息重新收一遍（用户看到重复回复）。

**为什么不持久化 context_token**：服务端 ~90–160s 就让它失效，而一次重启必然比这
更久；用户下一条消息会带来新 token，内存里存着就够。
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from core.config import DATA_DIR
from core.fs_utils import atomic_write_json, load_json_or_backup

logger = logging.getLogger(__name__)


def account_path() -> Path:
    """账号文件路径（惰性求值，便于测试 monkeypatch ``DATA_DIR``）。"""
    return DATA_DIR / "tools" / "wechat" / "account.json"


@dataclass
class WechatAccount:
    """扫码换来的凭证 + 长轮询游标。"""

    bot_token: str = ""
    base_url: str = ""
    ilink_user_id: str = ""
    ilink_bot_id: str = ""
    get_updates_buf: str = ""

    @classmethod
    def from_dict(cls, data: dict | None) -> "WechatAccount":
        if not isinstance(data, dict):
            return cls()
        return cls(
            bot_token=str(data.get("bot_token") or ""),
            base_url=str(data.get("base_url") or ""),
            ilink_user_id=str(data.get("ilink_user_id") or ""),
            ilink_bot_id=str(data.get("ilink_bot_id") or ""),
            get_updates_buf=str(data.get("get_updates_buf") or ""),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def has_token(self) -> bool:
        """运行时是否就绪（配置之外的判定，见 service.py）。"""
        return bool(self.bot_token)


def load_account() -> WechatAccount:
    """读取账号（文件缺失/损坏时返回空账号）。"""
    return WechatAccount.from_dict(load_json_or_backup(account_path(), {}))


def save_account(account: WechatAccount) -> bool:
    """原子写入账号。"""
    try:
        path = account_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, account.to_dict())
        return True
    except OSError as e:
        logger.warning(f"[wechat] 保存账号失败: {e}")
        return False


def clear_account() -> bool:
    """登出：删掉账号文件（下次启动会要求重新扫码）。"""
    try:
        account_path().unlink(missing_ok=True)
        return True
    except OSError as e:
        logger.warning(f"[wechat] 清除账号失败: {e}")
        return False


# ─── context_token 台账 ───────────────────────────────────────────────


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
