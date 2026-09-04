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
# Transport 层不做重试（设 0），重试由 base_agent 统一管理
_MAX_RETRIES = 0
_BASE_DELAY = 1.0  # seconds
_MAX_DELAY = 60.0  # seconds
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


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
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)
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
    ) -> Iterable[dict[str, Any]]:
        """Stream SSE events from API.

        Args:
            url: API endpoint URL
            headers: HTTP headers
            body: Request body (JSON)
            stop_check: Optional callable; if returns True, stream is interrupted

        Yields:
            Parsed JSON events from the stream

        Raises:
            httpx.HTTPStatusError: If response status >= 400
            InterruptedError: If stop_check returns True
        """
        with self._client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                try:
                    error_body = resp.read().decode("utf-8", errors="replace")
                    logger.error(f"[LLM] API 错误 {resp.status_code}: {error_body[:500]}")
                except Exception:
                    pass
                resp.raise_for_status()

            for line in resp.iter_lines():
                # Check for interruption
                if stop_check and stop_check():
                    raise InterruptedError("Stream interrupted by user")

                line = line.strip()
                if not line:
                    continue

                # Parse SSE data line
                if line.startswith("data:"):
                    payload = line[5:].strip()
                else:
                    continue

                if payload == "[DONE]":
                    break

                try:
                    event = json.loads(payload)
                    yield event
                except json.JSONDecodeError:
                    continue

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
    ) -> Any:
        """Execute an operation with automatic retry on transient errors.

        Args:
            operation: Callable that performs the HTTP request
            total_timeout: Optional total time limit in seconds
            stop_check: Optional callable; if returns True, retry is aborted

        Returns:
            Result of the operation

        Raises:
            The last exception if all retries fail
            InterruptedError: If stop_check returns True
        """
        start_time = time.time()
        retry_count = 0

        for attempt in range(_MAX_RETRIES + 1):
            # Check stop before each attempt
            if stop_check and stop_check():
                raise InterruptedError("Stopped by user during retry")

            try:
                return operation()

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                retry_count = attempt + 1

                if not self.should_retry(status) or attempt == _MAX_RETRIES:
                    raise

                retry_after = e.response.headers.get("retry-after")
                delay = self.retry_delay(attempt, retry_after)

                if total_timeout and time.time() - start_time + delay > total_timeout:
                    raise RuntimeError(
                        f"Total retry time exceeded {total_timeout}s after {retry_count} retries. "
                        f"Last error: HTTP {status}"
                    ) from e

                print(f"[LLM] {status} 错误，{delay:.0f}s 后重试 ({attempt + 1}/{_MAX_RETRIES})")
                self.interruptible_sleep(delay, stop_check)

            except httpx.TransportError as e:
                retry_count = attempt + 1

                if attempt == _MAX_RETRIES:
                    raise

                delay = self.retry_delay(attempt)

                if total_timeout and time.time() - start_time + delay > total_timeout:
                    raise RuntimeError(
                        f"Total retry time exceeded {total_timeout}s after {retry_count} retries. "
                        f"Last error: {e}"
                    ) from e

                print(f"[LLM] {self.format_network_error(e)}，{delay:.0f}s 后重试 ({attempt + 1}/{_MAX_RETRIES})")
                self.interruptible_sleep(delay, stop_check)
