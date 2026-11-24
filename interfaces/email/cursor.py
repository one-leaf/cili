"""邮件 IMAP 游标与拦截统计（按账号分文件，读-改-写互不覆盖）。

文件不存在 = 首次启动：轮询层据此跳过历史邮件（§3.7）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.config import DATA_DIR
from core.fs_utils import atomic_write_json, load_json_or_backup

# 拦截记录保留条数（足够展示最近 24h 的分布）
_MAX_IGNORED_EVENTS = 200


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

