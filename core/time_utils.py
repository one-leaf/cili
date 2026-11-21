"""统一的时间格式与当前时间（避免各模块各写一份 strftime）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_str() -> str:
    """当前本地时间，格式 ``yyyy-MM-dd HH:mm:ss``（会话/状态文件统一口径）。"""
    return datetime.now().strftime(TS_FORMAT)


def parse_ts(value: Any, default: datetime | None = None) -> datetime | None:
    """解析统一时间戳；失败回退 ISO 格式，再失败返回 ``default``。"""
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value), TS_FORMAT)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return default
