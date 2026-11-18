"""统一的时间格式与当前时间（避免各模块各写一份 strftime）。"""

from __future__ import annotations

from datetime import datetime

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_str() -> str:
    """当前本地时间，格式 ``yyyy-MM-dd HH:mm:ss``（会话/状态文件统一口径）。"""
    return datetime.now().strftime(TS_FORMAT)
