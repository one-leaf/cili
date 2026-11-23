"""QQ SDK 装载与环境：API 域名、延迟导入与可用性查询。"""

from __future__ import annotations

import os

# 沙箱 / 正式 API 域名（未过审机器人只能沙箱收发）。
# ⚠️ 待实测确认；若官方沙箱域名不同，改这里或用环境变量 QQ_API_BASE 覆盖。
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
PROD_API_BASE = "https://api.sgroup.qq.com"

# 必须在导入 SDK **之前**落定环境变量：SDK 的 constants.py 在 import 时读它。
# setdefault 尊重用户显式设置的值（正式环境部署的常规做法）。
os.environ.setdefault("QQ_API_BASE", SANDBOX_API_BASE)

# 延迟 import：未启用 / 未安装时不影响 cili 启动
SDK_AVAILABLE = False
_IMPORT_ERROR: str = ""
try:  # pragma: no cover - 取决于环境是否装了 SDK
    from qqbot_agent_sdk import (  # type: ignore
        EventParser,
        QQApiClient,
        QQWebSocket,
        WSCallbacks,
    )
    from qqbot_agent_sdk.media_loader import MediaUploader  # type: ignore
    from qqbot_agent_sdk.session_store import WSSessionStore  # type: ignore

    SDK_AVAILABLE = True
except ImportError as e:  # pragma: no cover
    _IMPORT_ERROR = str(e)
    QQApiClient = QQWebSocket = WSCallbacks = EventParser = None  # type: ignore
    MediaUploader = WSSessionStore = None  # type: ignore


def sdk_available() -> bool:
    return SDK_AVAILABLE


def sdk_import_error() -> str:
    return _IMPORT_ERROR


def effective_api_base() -> str:
    """当前 SDK 实际使用的 API 域名（来自 import 时的环境变量）。"""
    return os.environ.get("QQ_API_BASE") or SANDBOX_API_BASE
