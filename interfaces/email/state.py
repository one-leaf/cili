"""邮件接入端的状态与游标（会话状态共享实现见 ``interfaces/common/state.py``）。

两层分离：

- ``data/cili/tools/email/state.json``：账号 → owner + chats（与 QQ/微信同构）
- ``data/cili/tools/email/accounts/{id}.json``：IMAP 游标（UIDVALIDITY + last_uid）
  与拦截统计。按账号分文件——游标每轮落盘，读-改-写互不覆盖（与微信凭证分文件
  同理）。

文件不存在 = 首次启动：轮询层据此跳过历史邮件（§3.7）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.config import DATA_DIR
from core.fs_utils import atomic_write_json, load_json_or_backup
from interfaces.common.state import (
    AccountState as _AccountState,
)
from interfaces.common.state import (
    ChatState as _ChatState,
)
from interfaces.common.state import (
    ensure_session as _ensure_session,
)
from interfaces.common.state import (
    load_account_state as _load_account_state,
)
from interfaces.common.state import (
    resolve_current as _resolve_current,
)
from interfaces.common.state import (
    save_account_state as _save_account_state,
)

resolve_current = _resolve_current

_LEGACY_OWNER_KEY = "owner_email"
_LOG_TAG = "email"

# 拦截记录保留条数（足够展示最近 24h 的分布）
_MAX_IGNORED_EVENTS = 200


# ─── 会话状态（与 QQ/微信同构） ───────────────────────────────────────


def state_path() -> Path:
    """状态文件路径（惰性求值，便于测试 monkeypatch ``DATA_DIR``）。"""
    return DATA_DIR / "tools" / "email" / "state.json"


@dataclass
class EmailChatState(_ChatState):
    """一个发件人（聊天）的当前工作区/会话绑定。"""


@dataclass
class EmailAccountState(_AccountState):
    """一个账号的状态切片。"""

    CHAT_TYPE = EmailChatState


def load_account_state(account_id: str) -> EmailAccountState:
    return _load_account_state(
        account_id, state_path=state_path,
        account_cls=EmailAccountState, legacy_owner_key=_LEGACY_OWNER_KEY,
    )


def save_account_state(account_id: str, account: EmailAccountState) -> bool:
    return _save_account_state(
        account_id, account, state_path=state_path,
        legacy_owner_key=_LEGACY_OWNER_KEY, log_tag=_LOG_TAG,
    )


def ensure_session(
    account: EmailAccountState, chat_id: str, cfg, *, account_id: str = "",
) -> EmailChatState:
    return _ensure_session(
        account, chat_id, cfg,
        session_title="邮件会话", log_tag=_LOG_TAG,
        channel="email", account_id=account_id,
    )


# ─── IMAP 游标与拦截统计（按账号分文件） ─────────────────────────────


@dataclass
class CursorData:
    """一个账号的传输层状态。"""

    uidvalidity: int = 0
    last_uid: int = 0
    ignored: list[dict] = field(default_factory=list)   # [{reason, from, at}]

    @classmethod
    def from_dict(cls, data: dict | None) -> "CursorData":
        if not isinstance(data, dict):
            return cls()
        raw_events = data.get("ignored")
        events = [e for e in raw_events if isinstance(e, dict)] \
            if isinstance(raw_events, list) else []
        return cls(
            uidvalidity=int(data.get("uidvalidity") or 0),
            last_uid=int(data.get("last_uid") or 0),
            ignored=events[-_MAX_IGNORED_EVENTS:],
        )

    def to_dict(self) -> dict:
        return {
            "uidvalidity": self.uidvalidity,
            "last_uid": self.last_uid,
            "ignored": self.ignored,
        }


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def cursor_path(account_id: str) -> Path:
    """游标文件路径（账号 id = 邮箱地址，已由配置层校验无路径分隔符）。"""
    return DATA_DIR / "tools" / "email" / "accounts" / f"{account_id}.json"


def load_cursor(account_id: str) -> CursorData | None:
    """读游标；文件不存在返回 None（= 首次启动，跳过历史）。"""
    path = cursor_path(account_id)
    if not path.exists():
        return None
    return CursorData.from_dict(load_json_or_backup(path, {}))


def save_cursor(account_id: str, cursor: CursorData) -> bool:
    path = cursor_path(account_id)
    try:
        with _lock_for(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, cursor.to_dict())
        return True
    except OSError:
        return False


def add_ignored_event(cursor: CursorData, reason: str, sender: str) -> None:
    """在内存里的游标对象上追加一条拦截记录（调用方负责落盘）。"""
    cursor.ignored.append({
        "reason": reason,
        "from": sender,
        "at": time.time(),
    })
    cursor.ignored = cursor.ignored[-_MAX_IGNORED_EVENTS:]


def record_ignored(account_id: str, reason: str, sender: str) -> None:
    """记一条被拦截的入站（静默丢弃的观测点，§3.9）。失败只忽略。"""
    path = cursor_path(account_id)
    try:
        with _lock_for(path):
            cursor = CursorData.from_dict(load_json_or_backup(path, {}))
            add_ignored_event(cursor, reason, sender)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, cursor.to_dict())
    except OSError:
        return


def ignored_summary(account_id: str, *, window_seconds: float = 86400.0) -> dict:
    """最近窗口内的拦截汇总：总数、按原因分布、最近 5 条。"""
    cursor = load_cursor(account_id)
    if cursor is None:
        return {"total": 0, "by_reason": {}, "recent": []}
    cutoff = time.time() - window_seconds
    events = [e for e in cursor.ignored if float(e.get("at") or 0) >= cutoff]
    by_reason: dict[str, int] = {}
    for e in events:
        reason = str(e.get("reason") or "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    recent = [
        {
            "from": str(e.get("from") or ""),
            "reason": str(e.get("reason") or ""),
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(e.get("at") or 0))),
        }
        for e in events[-5:]
    ]
    return {"total": len(events), "by_reason": by_reason, "recent": recent}
