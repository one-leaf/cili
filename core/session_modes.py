"""会话模式常量与归一化（latest / dedicated）。

与通道注册表分离：通道声明（channels.py）不关心会话落点策略。
"""

from __future__ import annotations

SESSION_MODE_LATEST = "latest"
SESSION_MODE_DEDICATED = "dedicated"
SESSION_MODES = (SESSION_MODE_LATEST, SESSION_MODE_DEDICATED)
DEFAULT_SESSION_MODE = SESSION_MODE_DEDICATED


def normalize_session_mode(value: str | None, default: str = DEFAULT_SESSION_MODE) -> str:
    """模式字符串归一化：非法值回落默认。"""
    val = (value or "").strip().lower()
    return val if val in SESSION_MODES else default