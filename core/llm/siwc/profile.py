"""SIWC 请求整形：裁剪预览期不支持的 Responses 字段。"""

from __future__ import annotations

from core.llm.siwc import const


def shape_request(body: dict) -> dict:
    """就地裁剪并返回 SIWC 允许的 Responses 请求体。"""
    for key in const.UNSUPPORTED_REQUEST_FIELDS:
        body.pop(key, None)
    body.pop("max_output_tokens", None)

    body["store"] = False
    body["stream"] = True

    items = body.get("input")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("role") == "system":
                item["role"] = "developer"

    if isinstance(body.get("tool_choice"), dict):
        body["tool_choice"] = "auto"

    tools = body.get("tools")
    if isinstance(tools, list):
        kept = [t for t in tools if isinstance(t, dict) and t.get("type") == "function"]
        if kept:
            if not const.SEND_STRICT_FALSE:
                for tool in kept:
                    tool.pop("strict", None)
            body["tools"] = kept
        else:
            body.pop("tools", None)
            body.pop("tool_choice", None)

    if not const.KEEP_PROMPT_CACHE_KEY:
        body.pop("prompt_cache_key", None)
    if not const.SEND_REASONING:
        body.pop("reasoning", None)
    if not const.SEND_INCLUDE_ENCRYPTED:
        body.pop("include", None)
    return body
