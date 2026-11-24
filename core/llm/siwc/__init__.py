"""SIWC（Sign in with ChatGPT）接入包。

对外只暴露管理器、登录流程与请求整形；token 解析发生在请求时（见 LLMClient），
本包的导入不发起任何网络请求。
"""

from __future__ import annotations

from core.llm.siwc.login import (
    cancel,
    complete_manual,
    poll,
    set_authorized_hook,
    start_login,
)
from core.llm.siwc.login import (
    shutdown as shutdown_login,
)
from core.llm.siwc.manager import (
    SiwcAuthRequired,
    SiwcTransientError,
    get_access_token,
    logout,
    save_login_result,
    status,
)
from core.llm.siwc.manager import (
    shutdown as shutdown_tokens,
)
from core.llm.siwc.profile import shape_request

__all__ = [
    "SiwcAuthRequired",
    "SiwcTransientError",
    "get_access_token",
    "save_login_result",
    "status",
    "logout",
    "shutdown_tokens",
    "start_login",
    "poll",
    "complete_manual",
    "cancel",
    "set_authorized_hook",
    "shutdown_login",
    "shape_request",
    "assert_oauth_endpoint",
]


def assert_oauth_endpoint(config) -> None:
    """OAuth provider 的端点与协议由代码锁定，防止 token 发往任意主机。"""
    from core.llm.siwc import const

    if (config.protocol or "").lower() != "openai-responses":
        raise ValueError("OAuth provider 仅支持 OpenAI Responses 协议")
    if (config.base_url or "").rstrip("/") != const.API_BASE:
        raise ValueError(f"OAuth provider 的 Base URL 固定为 {const.API_BASE}")
