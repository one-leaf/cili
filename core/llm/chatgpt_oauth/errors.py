"""SIWC（ChatGPT 套餐额度）错误码 → 通用错误分类。"""

from __future__ import annotations

from core.llm.errors import KIND_AUTH, KIND_BAD_REQUEST, KIND_QUOTA, KIND_SERVER

# SIWC（ChatGPT 套餐额度）错误码 → (kind, 可重试, 文案)
_SIWC_ERROR_MAP: dict[str, tuple[str, bool, str]] = {
    "subscription_sharing_user_not_eligible": (
        KIND_QUOTA, False, "当前 ChatGPT 账号或套餐不支持套餐额度调用",
    ),
    "subscription_sharing_usage_limit_exceeded": (
        KIND_QUOTA, False,
        "ChatGPT 套餐用量已达上限，可查看 https://chatgpt.com/settings/usage",
    ),
    "subscription_sharing_workplace_individual_usage_limit_exceeded": (
        KIND_QUOTA, False, "工作区管理员设置了个人用量上限，请联系管理员",
    ),
    "subscription_sharing_usage_unavailable": (
        KIND_SERVER, True, "ChatGPT 套餐额度暂时不可用，请稍后重试",
    ),
    "subscription_sharing_user_unavailable": (
        KIND_SERVER, True, "ChatGPT 账号数据暂时不可用，请稍后重试",
    ),
    "subscription_sharing_invalid_user": (
        KIND_AUTH, False, "ChatGPT 登录已失效，请重新登录",
    ),
    "subscription_sharing_route_not_supported": (
        KIND_BAD_REQUEST, False, "该请求不被套餐额度路由支持",
    ),
    "subscription_sharing_unsupported_capability": (
        KIND_BAD_REQUEST, False, "请求使用了套餐额度不支持的能力",
    ),
    "unsupported_country_region_territory": (
        KIND_AUTH, False, "当前区域不支持该能力，请检查网络/代理设置",
    ),
}


def lookup_error(code: str | None) -> tuple[str, bool, str] | None:
    normalized = (code or "").strip().lower()
    if not normalized:
        return None
    entry = _SIWC_ERROR_MAP.get(normalized)
    if entry is not None:
        return entry
    if normalized.startswith("chatpass_v2"):
        return KIND_AUTH, False, "客户端/授权配置不匹配，请重新登录"
    return None
