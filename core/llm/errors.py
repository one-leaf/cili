"""Unified LLM error taxonomy and retry policy.

错误分类唯一所有权：传输层 with_retry、Runner 流式/非流式循环共用
``classify_llm_error``，按 kind 决定「三态」中的放弃与否，并按
Retry-After（头或响应体）决定退避时长。

分类借鉴 nanobot providers/base.py：429 细分——配额/余额/欠费类
（insufficient_quota/billing...）不可重试；限流/过载类
（rate_limit_exceeded/too_many_requests/overloaded...）可重试；
未知 429 默认按限流重试。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---- 错误语义 kind ----
KIND_TIMEOUT = "timeout"
KIND_CONNECTION = "connection"
KIND_AUTH = "auth"
KIND_QUOTA = "quota"
KIND_RATE_LIMIT = "rate_limit"
KIND_SERVER = "server"
KIND_CONTEXT_LENGTH = "context_length"
KIND_BAD_REQUEST = "bad_request"
KIND_UNKNOWN = "unknown"

# 429 细分：不可重试（配额/余额/欠费）——error.type / error.code token 标记
_NON_RETRYABLE_429_TOKENS = frozenset({
    "insufficient_quota", "quota_exceeded", "quota_exhausted",
    "billing_hard_limit_reached", "insufficient_balance", "credit_balance_too_low",
    "billing_not_active", "payment_required",
})

# 429 细分：不可重试——响应体文本标记
_NON_RETRYABLE_429_TEXT = (
    "insufficient_quota", "insufficient quota", "quota exceeded", "quota exhausted",
    "billing hard limit", "billing_hard_limit_reached", "billing not active",
    "insufficient balance", "insufficient_balance", "credit balance too low",
    "payment required", "out of credits", "out of quota",
    "exceeded your current quota",
    "余额不足", "额度不足", "配额不足", "配额用尽", "欠费", "请充值",
)

# 429 细分：可重试（限流/过载）——token 标记
_RETRYABLE_429_TOKENS = frozenset({
    "rate_limit_exceeded", "rate_limit_error", "too_many_requests",
    "request_limit_exceeded", "requests_limit_exceeded", "overloaded_error",
})

# 429 细分：可重试——响应体文本标记
_RETRYABLE_429_TEXT = (
    "rate limit", "rate_limit", "too many requests", "retry after",
    "try again in", "temporarily unavailable", "overloaded",
    "concurrency limit",
    "速率限制", "访问量过大", "请求过于频繁", "频率限制",
)


class StreamErrorEvent(Exception):
    """Raised when the SSE stream carries an error event (e.g. model overloaded).

    The stream's partial response must be discarded and the call treated as a
    transient failure so the caller can retry.
    """
    pass


@dataclass
class LLMErrorInfo:
    """结构化 LLM 错误分类，供重试策略与用户文案共用。"""

    kind: str = KIND_UNKNOWN
    status_code: int | None = None
    error_type: str | None = None  # provider 语义 token，如 insufficient_quota
    error_code: str | None = None  # provider 语义 code，如 rate_limit_exceeded
    message: str = "LLM 请求失败"
    retry_after_s: float | None = None
    should_retry: bool = False


def _normalize_token(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).strip().lower()
    return token or None


def _extract_type_code(text: str | None) -> tuple[str | None, str | None]:
    """从响应体文本提取 error.type / error.code（兼容 error 嵌套对象）。"""
    if not text:
        return None, None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    err = data.get("error")
    if isinstance(err, dict):
        return _normalize_token(err.get("type")), _normalize_token(err.get("code"))
    return _normalize_token(data.get("type")), _normalize_token(data.get("code"))


def _extract_retry_after(body_text: str | None, headers) -> float | None:
    """提取重试等待秒数：优先 Retry-After 头，其次响应体 retry_after 字段。"""
    if headers is not None:
        try:
            raw = headers.get("retry-after")
            if raw:
                return max(0.0, float(raw))
        except (ValueError, TypeError):
            pass
    if not body_text:
        return None
    try:
        data = json.loads(body_text)
        if not isinstance(data, dict):
            return None
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        for key in ("retry_after", "retry-after"):
            val = err.get(key) if isinstance(err, dict) else None
            if val is None:
                val = data.get(key)
            if val is not None:
                return max(0.0, float(val))
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def _classify_429(body_text: str | None) -> tuple[str, bool, str | None, str | None]:
    """429 语义细分。返回 (kind, should_retry, error_type, error_code)。

    未知 429 默认按限流处理（可重试），与 nanobot 一致。
    """
    error_type, error_code = _extract_type_code(body_text)
    tokens = {t for t in (error_type, error_code) if t}
    if tokens & _NON_RETRYABLE_429_TOKENS:
        return KIND_QUOTA, False, error_type, error_code
    lowered = (body_text or "").lower()
    if any(m in lowered for m in _NON_RETRYABLE_429_TEXT):
        return KIND_QUOTA, False, error_type, error_code
    if tokens & _RETRYABLE_429_TOKENS:
        return KIND_RATE_LIMIT, True, error_type, error_code
    if any(m in lowered for m in _RETRYABLE_429_TEXT):
        return KIND_RATE_LIMIT, True, error_type, error_code
    return KIND_RATE_LIMIT, True, error_type, error_code


def _safe_response_text(response) -> str:
    try:
        text = response.text
    except Exception:
        return ""
    return text or ""


def classify_llm_error(e: Exception, base_url: str = "") -> LLMErrorInfo:
    """把任意 LLM 调用异常分类为结构化 LLMErrorInfo（三态重试的输入）。

    kind 决定是否瞬态：timeout/connection/server/rate_limit 可重试；
    auth/quota/context_length/bad_request 不可重试。
    """
    # 流内 error 事件：瞬态
    if isinstance(e, StreamErrorEvent):
        return LLMErrorInfo(
            kind=KIND_SERVER,
            message=f"LLM 流错误: {e}",
            should_retry=True,
        )
    if isinstance(e, httpx.ConnectTimeout):
        return LLMErrorInfo(
            kind=KIND_TIMEOUT,
            message=f"LLM 连接超时，请检查网络或 API 地址是否正确 ({base_url})",
            should_retry=True,
        )
    if isinstance(e, httpx.ReadTimeout):
        return LLMErrorInfo(
            kind=KIND_TIMEOUT,
            message=f"LLM 读取超时，服务器响应过慢 ({base_url})",
            should_retry=True,
        )
    if isinstance(e, httpx.TimeoutException):
        return LLMErrorInfo(
            kind=KIND_TIMEOUT,
            message=f"LLM 请求超时，请检查网络连接 ({base_url})",
            should_retry=True,
        )
    if isinstance(e, httpx.ConnectError):
        return LLMErrorInfo(
            kind=KIND_CONNECTION,
            message=f"LLM 连接失败: {e}，请检查 API 地址和网络",
            should_retry=True,
        )
    if isinstance(e, httpx.NetworkError):
        return LLMErrorInfo(
            kind=KIND_CONNECTION,
            message=f"LLM 网络错误: {e}",
            should_retry=True,
        )
    if isinstance(e, httpx.TransportError):
        return LLMErrorInfo(
            kind=KIND_CONNECTION,
            message=f"LLM 传输错误: {e}",
            should_retry=True,
        )
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        body_text = _safe_response_text(e.response)
        error_type, error_code = _extract_type_code(body_text)
        retry_after = _extract_retry_after(body_text, e.response.headers)
        if status in (401, 403):
            info = LLMErrorInfo(
                kind=KIND_AUTH, status_code=status,
                message=f"LLM API 认证失败 (HTTP {status})，请检查 API Key 是否正确",
                error_type=error_type, error_code=error_code,
            )
        elif status == 402:
            info = LLMErrorInfo(
                kind=KIND_QUOTA, status_code=status,
                message="LLM API 余额不足 (HTTP 402)，请充值或更换 API Key",
                error_type=error_type, error_code=error_code,
            )
        elif status == 413:
            info = LLMErrorInfo(
                kind=KIND_CONTEXT_LENGTH, status_code=status,
                message="LLM 请求体过大 (HTTP 413)，上下文过长",
                error_type=error_type, error_code=error_code,
            )
        elif status == 429:
            kind, should_retry, _, _ = _classify_429(body_text)
            if kind == KIND_QUOTA:
                message = (
                    f"LLM API 配额/余额不足（{error_type or error_code or 'HTTP 429'}），"
                    "请充值或更换 API Key"
                )
            else:
                message = "LLM API 请求过于频繁 (HTTP 429)，上游限流"
            info = LLMErrorInfo(
                kind=kind, status_code=status, message=message,
                error_type=error_type, error_code=error_code,
                should_retry=should_retry,
            )
        elif status >= 500:
            info = LLMErrorInfo(
                kind=KIND_SERVER, status_code=status,
                message=f"LLM 服务器错误 (HTTP {status}): {body_text[:200]}",
                error_type=error_type, error_code=error_code,
                should_retry=True,
            )
        else:
            info = LLMErrorInfo(
                kind=KIND_BAD_REQUEST, status_code=status,
                message=f"LLM 错误 {status}: {body_text[:200]}",
                error_type=error_type, error_code=error_code,
            )
        if retry_after is not None:
            info.retry_after_s = retry_after
        return info
    return LLMErrorInfo(kind=KIND_UNKNOWN, message=f"LLM 请求失败: {e}")


def format_llm_error(e: Exception, base_url: str = "") -> str:
    """Format an LLM API exception into a short, actionable Chinese message.

    Shared by agent loop and runner to avoid duplicating the isinstance chain.
    """
    return classify_llm_error(e, base_url).message
