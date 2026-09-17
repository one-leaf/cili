"""统一错误 taxonomy（core/llm/errors.py）单元测试。

覆盖 classify_llm_error 的 kind 判定、429 语义细分、retry-after 提取，
以及 format_llm_error 与 transport.should_retry 的兼容行为。
"""

import httpx
import pytest
from unittest.mock import MagicMock

from core.llm import LLMResponse, TextBlock
from core.llm.errors import (
    LLMErrorInfo,
    StreamErrorEvent,
    classify_llm_error,
    format_llm_error,
)


def _http_error(status: int, body: str = "", headers: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://x")
    resp = httpx.Response(status_code=status, request=req)
    if body:
        resp._content = body.encode("utf-8")
    if headers:
        resp.headers.update(headers)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


class TestClassifyNetwork:
    """网络类错误 → timeout / connection，可重试。"""

    def test_connect_timeout(self):
        info = classify_llm_error(httpx.ConnectTimeout("t"))
        assert info.kind == "timeout" and info.should_retry is True
        assert "超时" in info.message

    def test_read_timeout(self):
        info = classify_llm_error(httpx.ReadTimeout("t"))
        assert info.kind == "timeout" and info.should_retry is True

    def test_generic_timeout(self):
        info = classify_llm_error(httpx.TimeoutException("t"))
        assert info.kind == "timeout" and info.should_retry is True

    def test_connect_error(self):
        info = classify_llm_error(httpx.ConnectError("c"))
        assert info.kind == "connection" and info.should_retry is True
        assert "连接失败" in info.message

    def test_network_error(self):
        info = classify_llm_error(httpx.NetworkError("n"))
        assert info.kind == "connection" and info.should_retry is True

    def test_generic_transport_error(self):
        info = classify_llm_error(httpx.TransportError("t"))
        assert info.kind == "connection" and info.should_retry is True

    def test_stream_error_event_transient(self):
        info = classify_llm_error(StreamErrorEvent("sse error"))
        assert info.kind == "server" and info.should_retry is True


class TestClassifyHTTP:
    """HTTP 状态码 → 语义 kind。"""

    def test_401_auth(self):
        info = classify_llm_error(_http_error(401))
        assert info.kind == "auth" and info.should_retry is False
        assert info.status_code == 401

    def test_403_auth(self):
        info = classify_llm_error(_http_error(403))
        assert info.kind == "auth" and info.should_retry is False

    def test_402_quota(self):
        info = classify_llm_error(_http_error(402))
        assert info.kind == "quota" and info.should_retry is False

    def test_413_context_length(self):
        info = classify_llm_error(_http_error(413))
        assert info.kind == "context_length" and info.should_retry is False

    def test_5xx_server_retryable(self):
        for status in (500, 502, 503, 504):
            info = classify_llm_error(_http_error(status))
            assert info.kind == "server" and info.should_retry is True, status

    def test_400_bad_request(self):
        info = classify_llm_error(_http_error(400, "bad body"))
        assert info.kind == "bad_request" and info.should_retry is False
        assert "bad body" in info.message

    def test_unknown_status(self):
        info = classify_llm_error(_http_error(418))
        assert info.kind == "bad_request" and info.should_retry is False

    def test_generic_exception(self):
        info = classify_llm_error(ValueError("boom"))
        assert info.kind == "unknown" and info.should_retry is False
        assert "boom" in info.message


class Test429Subclassification:
    """429 语义细分：quota 不可重试 / rate_limit 可重试 / 未知默认重试。"""

    @pytest.mark.parametrize("body", [
        '{"error": {"type": "insufficient_quota"}}',
        '{"error": {"code": "quota_exceeded"}}',
        '{"type": "billing_hard_limit_reached"}',
        '余额不足，请充值',
        'exceeded your current quota',
    ])
    def test_non_retryable_quota(self, body):
        info = classify_llm_error(_http_error(429, body))
        assert info.kind == "quota", body
        assert info.should_retry is False, body
        assert "配额" in info.message or "余额" in info.message

    @pytest.mark.parametrize("body", [
        '{"error": {"type": "rate_limit_exceeded"}}',
        '{"error": {"code": "too_many_requests"}}',
        '{"error": {"code": "overloaded_error"}}',
        '请求过于频繁，请稍后再试',
        'rate limit reached',
    ])
    def test_retryable_rate_limit(self, body):
        info = classify_llm_error(_http_error(429, body))
        assert info.kind == "rate_limit", body
        assert info.should_retry is True, body

    def test_unknown_429_defaults_to_retry(self):
        info = classify_llm_error(_http_error(429, '{"unrelated": true}'))
        assert info.kind == "rate_limit"
        assert info.should_retry is True

    def test_429_empty_body_defaults_to_retry(self):
        info = classify_llm_error(_http_error(429))
        assert info.should_retry is True

    def test_429_type_and_code_extracted(self):
        info = classify_llm_error(_http_error(429, '{"error": {"type": "insufficient_quota", "code": "billing"}}'))
        assert info.error_type == "insufficient_quota"
        assert info.error_code == "billing"


class TestRetryAfter:
    """Retry-After 提取：优先头，其次响应体。"""

    def test_from_header(self):
        info = classify_llm_error(_http_error(429, '{"error": {"type": "rate_limit_exceeded"}}', {"retry-after": "3"}))
        assert info.retry_after_s == 3.0

    def test_from_body_error_object(self):
        info = classify_llm_error(_http_error(429, '{"error": {"type": "rate_limit_exceeded", "retry_after": 7}}'))
        assert info.retry_after_s == 7.0

    def test_header_wins_over_body(self):
        info = classify_llm_error(_http_error(429, '{"error": {"retry_after": 7}}', {"retry-after": "2"}))
        assert info.retry_after_s == 2.0

    def test_invalid_retry_after_ignored(self):
        info = classify_llm_error(_http_error(429, '{"error": {"retry_after": "abc"}}'))
        assert info.retry_after_s is None


class TestFormatAndTransport:
    """format_llm_error 兼容 + transport.should_retry 语义。"""

    def test_format_network_error_compat(self):
        assert "超时" in format_llm_error(httpx.ConnectTimeout("t"))
        assert "连接失败" in format_llm_error(httpx.ConnectError("c"))

    def test_format_http_error_compat(self):
        assert "认证失败" in format_llm_error(_http_error(401))
        assert "服务器错误" in format_llm_error(_http_error(503))

    def test_format_quota_message(self):
        msg = format_llm_error(_http_error(429, '{"error": {"type": "insufficient_quota"}}'))
        assert "配额" in msg

    def test_format_generic(self):
        assert "boom" in format_llm_error(ValueError("boom"))

    def test_format_via_package_reexport(self):
        from core.llm import format_llm_error as pkg_fmt
        from core.llm.types import format_llm_error as types_fmt
        assert pkg_fmt(httpx.ConnectTimeout("t")) == types_fmt(httpx.ConnectTimeout("t"))

    def test_should_retry_semantic_429(self):
        from core.llm.transport import HttpTransport
        t = HttpTransport()
        assert t.should_retry(429) is True
        assert t.should_retry(429, "余额不足") is False
        assert t.should_retry(429, '{"error": {"type": "rate_limit_exceeded"}}') is True
        assert t.should_retry(500) is True
        assert t.should_retry(401) is False
        assert t.should_retry(200) is False

    def test_llm_error_info_public(self):
        assert LLMErrorInfo(kind="quota", should_retry=False).kind == "quota"


class TestRunnerStreamingRetry:
    """Runner 流式三态：quota 立即放弃 / rate_limit 退避重试。"""

    @staticmethod
    def _always_raise(exc):
        def impl(**kw):
            raise exc
        return impl

    def _make_runner(self, chat_stream_impl, monkeypatch):
        from core.agent_runtime.runner import Runner
        agent = MagicMock()
        agent.client = MagicMock()
        agent.client.base_url = "http://fake"
        agent.client.chat_stream.side_effect = chat_stream_impl
        agent._on_text = None
        agent._on_thinking = None
        agent._stopped = False
        agent._session_id = "sess"
        agent.tool_schemas = []
        runner = Runner(agent)
        runner._prepare_messages_for_llm = MagicMock(return_value=[])
        # 重试退避 sleep 置空，避免真实等待
        monkeypatch.setattr("core.agent_runtime.runner.time.sleep", lambda *a, **k: None)
        return runner, agent

    def test_quota_429_gives_up_immediately(self, monkeypatch):
        """quota 429 第 1 次调用即放弃，不再 3 次重试。"""
        runner, agent = self._make_runner(
            self._always_raise(_http_error(429, '{"error": {"type": "insufficient_quota"}}')),
            monkeypatch,
        )
        with pytest.raises(RuntimeError, match="配额"):
            runner._call_llm_streaming("sys")
        assert agent.client.chat_stream.call_count == 1

    def test_rate_limit_retries_then_succeeds(self, monkeypatch):
        """rate_limit 429 重试后成功。"""
        calls = {"n": 0}

        def impl(**kw):
            calls["n"] += 1
            if calls["n"] < 2:
                raise _http_error(429, '{"error": {"type": "rate_limit_exceeded"}}')
            return LLMResponse(content=[TextBlock(text="ok")], stop_reason="end_turn")

        runner, agent = self._make_runner(impl, monkeypatch)
        resp = runner._call_llm_streaming("sys")
        assert resp.get_text() == "ok"
        assert calls["n"] == 2

    def test_rate_limit_exhausted_raises(self, monkeypatch):
        """rate_limit 429 持续失败至上限后抛出 RuntimeError。"""
        runner, agent = self._make_runner(
            self._always_raise(_http_error(429, '{"error": {"type": "rate_limit_exceeded"}}')),
            monkeypatch,
        )
        with pytest.raises(RuntimeError, match="限流"):
            runner._call_llm_streaming("sys")
        # max_retries=3 → 共 4 次调用（3 次退避重试 + 最终失败）
        assert agent.client.chat_stream.call_count == 4

    def test_network_error_retries(self, monkeypatch):
        """网络错误（瞬态）重试。"""
        calls = {"n": 0}

        def impl(**kw):
            calls["n"] += 1
            if calls["n"] < 2:
                raise httpx.ConnectError("boom")
            return LLMResponse(content=[TextBlock(text="ok")], stop_reason="end_turn")

        runner, agent = self._make_runner(impl, monkeypatch)
        resp = runner._call_llm_streaming("sys")
        assert resp.get_text() == "ok"
        assert calls["n"] == 2
