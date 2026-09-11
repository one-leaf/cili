"""HTTP transport layer for LLM API communication.

This module provides a thin HTTP transport that handles:
- Connection pooling (httpx client)
- Request/response handling
- SSE streaming
- Network error formatting

The transport is provider-agnostic — adapters handle protocol-specific
serialization and deserialization.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Callable, Iterable

import httpx

logger = logging.getLogger(__name__)

# Retry configuration
# 非流式调用（chat/压缩/兜底）由 transport 层重试；流式调用由 base_agent
# 统一管理（chat_stream 显式传 max_retries=0），避免双层重试。
_MAX_RETRIES = 2
_BASE_DELAY = 1.0  # seconds
_MAX_DELAY = 60.0  # seconds
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


class StreamErrorEvent(Exception):
    """Raised when the SSE stream carries an error event (e.g. model overloaded).

    The stream's partial response must be discarded and the call treated as a
    transient failure so the caller can retry.
    """
    pass


def _parse_sse_payload(payload: str, event_type: str | None) -> dict[str, Any]:
    """解析一条 SSE 事件（可能由多行 data: 累积而成）。

    错误事件或非法 JSON 一律抛出 StreamErrorEvent，让调用方把本次流视为
    失败并重试，而不是静默丢数据。
    """
    if event_type == "error":
        detail = payload[:200]
        try:
            err = json.loads(payload)
            msg = err.get("error", {}).get("message")
            if msg:
                detail = msg[:200]
        except (json.JSONDecodeError, AttributeError):
            pass
        logger.warning(f"[LLM] SSE error event: {detail}")
        raise StreamErrorEvent(f"SSE stream错误: {detail}")

    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        logger.warning(f"[LLM] SSE 数据解析失败，中止流: {payload[:150]!r}")
        raise StreamErrorEvent(f"SSE JSON 解析失败: {e}") from e


class HttpTransport:
    """HTTP transport for LLM API calls.

    Handles connection pooling, streaming, and retry logic.
    Provider-agnostic — adapters handle serialization.
    """

    def __init__(
        self,
        timeout: float = 600.0,
        connect_timeout: float = 30.0,
    ):
        """Initialize transport.

        Args:
            timeout: Default request timeout in seconds
            connect_timeout: Connection timeout in seconds
        """
        # 各阶段单独设超时（httpx 0.28 无 total 概念）：连接短、读写长，
        # 不设整体上限，避免长思考/长输出被总体超时打断。
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=timeout,
            write=timeout,
            pool=timeout,
        )
        self._client = httpx.Client(
            timeout=self._timeout,
            headers={"User-Agent": "cili-agent"},
        )

    @property
    def client(self) -> httpx.Client:
        """Access the underlying httpx client."""
        return self._client

    def post(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        timeout: httpx.Timeout | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        """Send a POST request and return response.

        Args:
            url: API endpoint URL
            headers: HTTP headers
            body: Request body (JSON)
            timeout: Optional timeout override

        Returns:
            (status_code, response_headers, response_body)

        Raises:
            httpx.HTTPStatusError: If response status >= 400
        """
        resp = self._client.post(
            url,
            headers=headers,
            json=body,
            timeout=timeout,
        )

        # Parse response body
        try:
            body_data = resp.json()
        except json.JSONDecodeError:
            body_data = {"_raw": resp.text}

        return resp.status_code, dict(resp.headers), body_data

    def stream(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        stop_check: Callable[[], bool] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> Iterable[dict[str, Any]]:
        """Stream SSE events from API.

        Args:
            url: API endpoint URL
            headers: HTTP headers
            body: Request body (JSON)
            stop_check: Optional callable; if returns True, stream is interrupted
            timeout: Optional per-phase timeout override

        Yields:
            Parsed JSON events from the stream

        Raises:
            httpx.HTTPStatusError: If response status >= 400
            InterruptedError: If stop_check returns True
            StreamErrorEvent: On error events or malformed JSON (aborts the stream)
        """
        with self._client.stream("POST", url, headers=headers, json=body, timeout=timeout) as resp:
            if resp.status_code >= 400:
                try:
                    error_body = resp.read().decode("utf-8", errors="replace")
                    logger.error(f"[LLM] API 错误 {resp.status_code}: {error_body[:500]}")
                except Exception:
                    pass
                resp.raise_for_status()

            current_event = None
            data_lines: list[str] = []

            def flush():
                """将累积的多行 data 作为一条事件产出（SSE 规范：按空行分事件）。"""
                nonlocal current_event, data_lines
                if not data_lines:
                    current_event = None
                    return
                payload = "\n".join(data_lines)
                data_lines = []
                ev_type = current_event
                current_event = None
                yield _parse_sse_payload(payload, ev_type)

            for line in resp.iter_lines():
                # Check for interruption
                if stop_check and stop_check():
                    raise InterruptedError("Stream interrupted by user")

                line = line.strip()
                if not line:
                    # 空行 = 事件终止符，刷新累积的 data
                    yield from flush()
                    continue

                # Parse SSE event type
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                    continue

                # Accumulate SSE data lines (multiple data: lines = one event)
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        yield from flush()  # 先刷新之前的累积，再结束
                        break
                    data_lines.append(payload)
                    continue

                # Ignore other SSE fields (id:, retry:, comments)

            # End of stream: flush any remaining accumulated data
            yield from flush()

    def close(self) -> None:
        """Close the HTTP client and release resources."""
        self._client.close()

    # ========== Retry logic ==========

    @staticmethod
    def should_retry(status_code: int) -> bool:
        """Return True if this status code is retryable."""
        return status_code in _RETRY_STATUS_CODES

    @staticmethod
    def retry_delay(attempt: int, retry_after: str | None = None) -> float:
        """Compute delay for exponential backoff with jitter.

        If the server provided a Retry-After header, respect it (with small jitter).
        Otherwise use exponential backoff: 1s, 2s, 4s, 8s ... capped at _MAX_DELAY.
        """
        if retry_after:
            try:
                return max(0.0, float(retry_after)) + random.uniform(0, 0.5)
            except (ValueError, TypeError):
                pass
        delay = min(_BASE_DELAY * (2 ** attempt), _MAX_DELAY)
        # Add jitter: ±25%
        return delay * random.uniform(0.75, 1.25)

    @staticmethod
    def format_network_error(e: httpx.TransportError) -> str:
        """Format a network error into a short, actionable Chinese message."""
        if isinstance(e, httpx.ConnectTimeout):
            return "连接超时 (connect timeout)"
        if isinstance(e, httpx.ReadTimeout):
            return "读取超时 (read timeout)"
        if isinstance(e, httpx.WriteTimeout):
            return "写入超时 (write timeout)"
        if isinstance(e, httpx.TimeoutException):
            return "请求超时 (timeout)"
        if isinstance(e, httpx.ConnectError):
            return f"连接失败: {e}"
        if isinstance(e, httpx.NetworkError):
            return f"网络错误: {e}"
        return f"传输错误: {e}"

    def interruptible_sleep(
        self,
        duration: float,
        stop_check: Callable[[], bool] | None = None,
    ) -> None:
        """Sleep for duration seconds, checking stop_check every 0.5s."""
        elapsed = 0.0
        interval = 0.5
        while elapsed < duration:
            if stop_check and stop_check():
                raise InterruptedError("Stopped by user during retry wait")
            sleep_time = min(interval, duration - elapsed)
            time.sleep(sleep_time)
            elapsed += sleep_time

    def with_retry(
        self,
        operation: Callable[[], Any],
        total_timeout: float | None = None,
        stop_check: Callable[[], bool] | None = None,
        max_retries: int | None = None,
    ) -> Any:
        """Execute an operation with automatic retry on transient errors.

        Args:
            operation: Callable that performs the HTTP request
            total_timeout: Optional total time limit in seconds
            stop_check: Optional callable; if returns True, retry is aborted
            max_retries: Override retry count (defaults to _MAX_RETRIES).
                Stream callers should pass 0 since base_agent owns stream retries.

        Returns:
            Result of the operation

        Raises:
            The last exception if all retries fail
            InterruptedError: If stop_check returns True
        """
        if max_retries is None:
            max_retries = _MAX_RETRIES
        start_time = time.time()
        retry_count = 0

        for attempt in range(max_retries + 1):
            # Check stop before each attempt
            if stop_check and stop_check():
                raise InterruptedError("Stopped by user during retry")

            try:
                return operation()

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                retry_count = attempt + 1

                if not self.should_retry(status) or attempt == max_retries:
                    raise

                retry_after = e.response.headers.get("retry-after")
                delay = self.retry_delay(attempt, retry_after)

                if total_timeout and time.time() - start_time + delay > total_timeout:
                    raise RuntimeError(
                        f"Total retry time exceeded {total_timeout}s after {retry_count} retries. "
                        f"Last error: HTTP {status}"
                    ) from e

                logger.warning(f"[LLM] {status} 错误，{delay:.0f}s 后重试 ({attempt + 1}/{max_retries})")
                self.interruptible_sleep(delay, stop_check)

            except httpx.TransportError as e:
                retry_count = attempt + 1

                if attempt == max_retries:
                    raise

                delay = self.retry_delay(attempt)

                if total_timeout and time.time() - start_time + delay > total_timeout:
                    raise RuntimeError(
                        f"Total retry time exceeded {total_timeout}s after {retry_count} retries. "
                        f"Last error: {e}"
                    ) from e

                logger.warning(f"[LLM] {self.format_network_error(e)}，{delay:.0f}s 后重试 ({attempt + 1}/{max_retries})")
                self.interruptible_sleep(delay, stop_check)
