"""LLM 传输层测试：重试、SSE、超时与 429 细分。"""


import httpx
import pytest

from core.config import ResolvedModel
from core.llm import (
    LLMClient,
    Message,
    OpenAIAdapter,
)
from core.llm.transport import HttpTransport


class TestRetryLogic:
    def test_should_retry_retryable_codes(self):
        for code in [429, 500, 502, 503, 504]:
            assert HttpTransport.should_retry(code) is True

    def test_should_retry_non_retryable_codes(self):
        for code in [200, 201, 400, 401, 403, 404]:
            assert HttpTransport.should_retry(code) is False

    def test_retry_delay_with_retry_after(self):
        delay = HttpTransport.retry_delay(0, retry_after="2.0")
        assert 2.0 <= delay <= 2.5

    def test_retry_delay_exponential_backoff(self):
        # attempt 0 -> base 1s, attempt 1 -> 2s, attempt 2 -> 4s
        delays = [HttpTransport.retry_delay(i) for i in range(4)]
        # Should be approximately 1, 2, 4, 8 with jitter ±25%
        assert 0.75 <= delays[0] <= 1.25
        assert 1.5 <= delays[1] <= 2.5
        assert 3.0 <= delays[2] <= 5.0

    def test_retry_delay_invalid_retry_after(self):
        # Invalid retry-after should fall back to exponential
        delay = HttpTransport.retry_delay(0, retry_after="invalid")
        assert 0.75 <= delay <= 1.25


# ========== BlockAssembler ==========


class TestBlockAssembler:
    """BlockAssembler 流式累积与 usage 合并。"""

    def test_usage_merge_preserves_input_tokens(self):
        """Anthropic message_start 带完整 usage、message_delta 只带 output_tokens 时应合并而非覆盖。"""
        from core.llm.assembler import BlockAssembler
        from core.llm.types import StreamChunk, UsageData

        assembler = BlockAssembler()
        # 模拟 message_start 的完整 usage
        assembler.push(StreamChunk.usage_chunk(UsageData(
            input_tokens=100, output_tokens=10, cache_read_tokens=80, cache_write_tokens=20,
        )))
        # 模拟 message_delta 的 usage（只含 output_tokens）
        assembler.push(StreamChunk.usage_chunk(UsageData(output_tokens=25)))

        assert assembler.usage.input_tokens == 100
        assert assembler.usage.output_tokens == 25
        assert assembler.usage.cache_read_tokens == 80
        assert assembler.usage.cache_write_tokens == 20

    def test_usage_single_event(self):
        """单个 usage 事件（OpenAI 风格）仍正常。"""
        from core.llm.assembler import BlockAssembler
        from core.llm.types import StreamChunk, UsageData

        assembler = BlockAssembler()
        assembler.push(StreamChunk.usage_chunk(UsageData(input_tokens=5, output_tokens=3)))
        assert assembler.usage.input_tokens == 5
        assert assembler.usage.output_tokens == 3

    def test_text_block_accumulation(self):
        """text 块：block_start 后多个 text_delta 拼接。"""
        from core.llm.assembler import BlockAssembler
        from core.llm.types import StreamChunk

        a = BlockAssembler()
        a.push(StreamChunk.block_start(0, "text"))
        a.push(StreamChunk.text_delta(0, "Hello "))
        a.push(StreamChunk.text_delta(0, "world"))
        a.push(StreamChunk.block_end(0))
        assert a.get_text() == "Hello world"
        assert len(a.blocks) == 1

    def test_reasoning_and_signature(self):
        """reasoning 块：text_delta 拼接 + signature_delta 赋值。"""
        from core.llm.assembler import BlockAssembler
        from core.llm.types import StreamChunk

        a = BlockAssembler()
        a.push(StreamChunk.block_start(0, "reasoning"))
        a.push(StreamChunk.reasoning_delta(0, "思考"))
        a.push(StreamChunk.reasoning_delta(0, "过程"))
        a.push(StreamChunk.signature_delta(0, "sig-123"))
        a.push(StreamChunk.block_end(0))
        from core.llm.types import ReasoningBlock
        block = a.blocks[0]
        assert isinstance(block, ReasoningBlock)
        assert block.text == "思考过程"
        assert block.signature == "sig-123"

    def test_tool_call_id_overwrite_arguments_append(self):
        """L14：id/name/thought_signature 是完整值用赋值，arguments 是分片用 +=。"""
        from core.llm.assembler import BlockAssembler
        from core.llm.types import StreamChunk, ToolCallBlock

        a = BlockAssembler()
        a.push(StreamChunk.block_start(0, "tool_call"))
        # 首个 delta 携带 id/name，之后某分片又重复带 id（OpenAI 分片携带完整字段）
        a.push(StreamChunk.tool_call_delta(0, id="tool_1", name="bash",
                                           arguments='{"command":'))
        a.push(StreamChunk.tool_call_delta(0, id="tool_1", arguments=' "ls"}'))
        a.push(StreamChunk.block_end(0))
        block = a.get_tool_calls()[0]
        assert isinstance(block, ToolCallBlock)
        assert block.id == "tool_1"          # 赋值 → 不被分片二次拼接
        assert block.name == "bash"
        assert block.arguments == '{"command": "ls"}'  # += → 正确拼接

    def test_tool_call_thought_signature_overwrite(self):
        from core.llm.assembler import BlockAssembler
        from core.llm.types import StreamChunk
        a = BlockAssembler()
        a.push(StreamChunk.block_start(0, "tool_call"))
        a.push(StreamChunk.tool_call_delta(0, id="t1", thought_signature="A"))
        a.push(StreamChunk.tool_call_delta(0, id="t1", thought_signature="B"))
        assert a.get_tool_calls()[0].thought_signature == "B"  # 赋值非拼接

    def test_finish_sets_stop_reason(self):
        from core.llm.assembler import BlockAssembler
        from core.llm.types import StreamChunk
        a = BlockAssembler()
        a.push(StreamChunk.finish_chunk("tool_use"))
        assert a.stop_reason == "tool_use"
        assert a.finished is True

    def test_unknown_block_type_ignored(self):
        from core.llm.assembler import BlockAssembler
        from core.llm.types import StreamChunk
        a = BlockAssembler()
        a.push(StreamChunk.block_start(0, "mystery"))
        a.push(StreamChunk.text_delta(0, "x"))  # index 0 无对应块，静默忽略
        assert a.blocks == []
        assert a.get_text() == ""

    def test_reset_clears_state(self):
        from core.llm.assembler import BlockAssembler
        from core.llm.types import StreamChunk
        a = BlockAssembler()
        a.push(StreamChunk.block_start(0, "text"))
        a.push(StreamChunk.text_delta(0, "hi"))
        a.push(StreamChunk.finish_chunk("end_turn"))
        a.reset()
        assert a.blocks == []
        assert a.stop_reason == ""
        assert a.finished is False
        assert a.get_text() == ""

    def test_multiple_blocks_get_text(self):
        """多个 text 块顺序拼接，get_tool_calls 只返回工具调用块。"""
        from core.llm.assembler import BlockAssembler
        from core.llm.types import StreamChunk
        a = BlockAssembler()
        a.push(StreamChunk.block_start(0, "text"))
        a.push(StreamChunk.text_delta(0, "One"))
        a.push(StreamChunk.block_start(1, "tool_call"))
        a.push(StreamChunk.tool_call_delta(1, id="t1", name="bash", arguments="{}"))
        a.push(StreamChunk.block_start(2, "text"))
        a.push(StreamChunk.text_delta(2, "Two"))
        assert a.get_text() == "OneTwo"
        assert [tc.id for tc in a.get_tool_calls()] == ["t1"]


# ========== SSE 流 error 事件 ==========


class TestTransportSSEError:
    """SSE error 事件不再被吞，应抛出 StreamErrorEvent。"""

    def test_error_event_raises(self):
        """流中出现 error 事件时抛出 StreamErrorEvent，而不是静默跳过。"""
        from core.llm.transport import HttpTransport, StreamErrorEvent

        class FakeResp:
            status_code = 200

            def __init__(self, lines):
                self._lines = lines

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def iter_lines(self):
                return iter(self._lines)

        class FakeClient:
            def __init__(self, lines):
                self._lines = lines

            def stream(self, *args, **kwargs):
                return FakeResp(self._lines)

        transport = HttpTransport()
        transport._client = FakeClient([
            'event: message_start',
            'data: {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}',
            '',
            'event: error',
            'data: {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}',
        ])

        events = transport.stream("http://x", {}, {})
        with pytest.raises(StreamErrorEvent):
            list(events)


# ========== Factory ==========


def _make_http_error(status: int, body: str = "", headers: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://x")
    resp = httpx.Response(status_code=status, request=req)
    if body:
        resp._content = body.encode("utf-8")
    if headers:
        resp.headers.update(headers)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


class TestRetryDefaults:
    """非流式重试启用（A23）：_MAX_RETRIES >= 2，with_retry 生效。"""

    def test_max_retries_default_enabled(self):
        from core.llm import transport as transport_module
        assert transport_module._MAX_RETRIES >= 2

    def test_with_retry_retries_transient_then_succeeds(self, monkeypatch):
        from core.llm.transport import HttpTransport
        transport = HttpTransport()
        monkeypatch.setattr(transport, "interruptible_sleep", lambda *a, **k: None)
        calls = {"n": 0}

        def op():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _make_http_error(502)
            return "ok"

        result = transport.with_retry(op, max_retries=2)
        assert result == "ok"
        assert calls["n"] == 3

    def test_with_retry_max_retries_zero_raises_immediately(self):
        from core.llm.transport import HttpTransport
        transport = HttpTransport()

        def op():
            raise _make_http_error(502)

        with pytest.raises(httpx.HTTPStatusError):
            transport.with_retry(op, max_retries=0)

    def test_with_retry_total_timeout_aborts(self, monkeypatch):
        """总时长上限生效：重试之间累计超限即放弃。

        此前 chat() 从不传 total_timeout，该分支不可达 —— 网络持续抖动时
        重试会一路叠加。
        """
        from core.llm.transport import HttpTransport
        transport = HttpTransport()
        monkeypatch.setattr(transport, "interruptible_sleep", lambda *a, **k: None)
        calls = {"n": 0}

        def op():
            calls["n"] += 1
            raise _make_http_error(503)

        # 上限 0.001s：首次失败后 base delay(~1s) 已超限
        with pytest.raises(RuntimeError, match="重试超时"):
            transport.with_retry(op, total_timeout=0.001, max_retries=3)
        assert calls["n"] == 1, "超限后不应再重试"

    def test_chat_wires_total_retry_timeout(self):
        """chat() 必须把总时长上限传给 with_retry（防参数被摘掉）。"""
        from unittest.mock import MagicMock

        from core.llm.client import _TOTAL_RETRY_TIMEOUT_S, LLMClient

        client = LLMClient.__new__(LLMClient)
        client.max_tokens = 1024
        client.model_name = "test-model"
        client.adapter = MagicMock()
        client.adapter.api_url = "http://example.invalid"
        client.transport = MagicMock()
        client.transport.post.return_value = (200, {}, {})
        client.transport.with_retry.return_value = {}
        client.adapter.parse_response.return_value = ([], "end_turn", MagicMock())

        client.chat(messages=[])

        _, kwargs = client.transport.with_retry.call_args
        assert _TOTAL_RETRY_TIMEOUT_S > 0
        assert kwargs.get("total_timeout") == _TOTAL_RETRY_TIMEOUT_S

    # ---- 统一 taxonomy：429 语义细分（quota 不可重试 / rate_limit 可重试）----

    def test_quota_429_not_retried(self, monkeypatch):
        """配额/余额不足的 429 立即放弃，不消耗重试次数。"""
        from core.llm.transport import HttpTransport
        transport = HttpTransport()
        monkeypatch.setattr(transport, "interruptible_sleep", lambda *a, **k: None)
        calls = {"n": 0}

        def op():
            calls["n"] += 1
            raise _make_http_error(429, '{"error": {"type": "insufficient_quota"}}')

        with pytest.raises(httpx.HTTPStatusError):
            transport.with_retry(op, max_retries=2)
        assert calls["n"] == 1, "quota 429 不应重试"

    def test_quota_429_chinese_marker_not_retried(self, monkeypatch):
        """中文「余额不足」标记同样判定为不可重试。"""
        from core.llm.transport import HttpTransport
        transport = HttpTransport()
        monkeypatch.setattr(transport, "interruptible_sleep", lambda *a, **k: None)
        calls = {"n": 0}

        def op():
            calls["n"] += 1
            raise _make_http_error(429, "余额不足，请充值")

        with pytest.raises(httpx.HTTPStatusError):
            transport.with_retry(op, max_retries=2)
        assert calls["n"] == 1

    def test_rate_limit_429_retried_and_succeeds(self, monkeypatch):
        """限流 429 重试并成功。"""
        from core.llm.transport import HttpTransport
        transport = HttpTransport()
        monkeypatch.setattr(transport, "interruptible_sleep", lambda *a, **k: None)
        calls = {"n": 0}

        def op():
            calls["n"] += 1
            if calls["n"] < 2:
                raise _make_http_error(429, '{"error": {"code": "rate_limit_exceeded"}}')
            return "ok"

        assert transport.with_retry(op, max_retries=2) == "ok"
        assert calls["n"] == 2

    def test_rate_limit_429_honors_retry_after(self, monkeypatch):
        """限流 429 尊重 Retry-After 头作为退避时长。"""
        from core.llm.transport import HttpTransport
        transport = HttpTransport()
        delays = []
        monkeypatch.setattr(transport, "interruptible_sleep", lambda *a, **k: delays.append(a[0]))
        calls = {"n": 0}

        def op():
            calls["n"] += 1
            if calls["n"] < 2:
                raise _make_http_error(429, '{"error": {"type": "rate_limit_exceeded"}}', {"retry-after": "7"})
            return "ok"

        assert transport.with_retry(op, max_retries=2) == "ok"
        assert 7.0 <= delays[0] <= 7.6, f"应尊重 Retry-After≈7s，实际 {delays}"

    def test_rate_limit_429_retry_after_from_body(self, monkeypatch):
        """限流 429 尊重响应体里的 retry_after 字段。"""
        from core.llm.transport import HttpTransport
        transport = HttpTransport()
        delays = []
        monkeypatch.setattr(transport, "interruptible_sleep", lambda *a, **k: delays.append(a[0]))
        calls = {"n": 0}

        def op():
            calls["n"] += 1
            if calls["n"] < 2:
                raise _make_http_error(429, '{"error": {"type": "rate_limit_exceeded", "retry_after": 5}}')
            return "ok"

        assert transport.with_retry(op, max_retries=2) == "ok"
        assert 5.0 <= delays[0] <= 5.6, f"应尊重 body retry_after≈5s，实际 {delays}"


class _FakeSSEClient:
    """流式测试用假 client，返回构造好的 SSE 行序列。"""

    def __init__(self, lines):
        self._lines = lines

    def stream(self, *args, **kwargs):
        class FakeResp:
            status_code = 200

            def __init__(self, lines):
                self._lines = lines

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_lines(self):
                return iter(self._lines)

        return FakeResp(self._lines)


class TestTransportSSEMultiline:
    """多行 data 累积为单条事件（A24）。"""

    def test_multiline_data_accumulated(self):
        from core.llm.transport import HttpTransport
        transport = HttpTransport()
        transport._client = _FakeSSEClient([
            'data: {"type": "part1",',
            'data:  "value": "part2"}',
            '',
            'data: {"type": "done"}',
            '',
        ])
        events = list(transport.stream("http://x", {}, {}))
        assert len(events) == 2
        assert events[0]["type"] == "part1"
        assert events[0]["value"] == "part2"


class TestTransportSSEParseFailure:
    """SSE 非法 JSON 中止流而不是静默跳过（A24）。"""

    def test_invalid_json_aborts(self):
        from core.llm.transport import HttpTransport, StreamErrorEvent
        transport = HttpTransport()
        transport._client = _FakeSSEClient([
            'data: {invalid json',
            '',
        ])
        events = transport.stream("http://x", {}, {})
        with pytest.raises(StreamErrorEvent):
            list(events)


class TestChatStreamIncomplete:
    """流结束未收 finish chunk 且无内容 → 视为失败可重试（A24）。"""

    def test_empty_stream_raises(self):
        config = ResolvedModel(
            name="gpt-4o", api_key="key", protocol="openai",
            base_url="https://api.openai.com",
        )
        adapter = OpenAIAdapter(config)

        class FakeTransport:
            def stream(self, *a, **k):
                return iter([])

            def with_retry(self, op, **k):
                return op()

        client = LLMClient(adapter=adapter, transport=FakeTransport(), config=config)
        with pytest.raises(RuntimeError, match="完成信号"):
            client.chat_stream(messages=[Message(role="user", content="hi")])


class TestChatTimeoutParam:
    """chat() 暴露 timeout 入参并透传（A23）。"""

    def test_chat_passes_timeout_to_post(self):
        config = ResolvedModel(
            name="gpt-4o", api_key="key", protocol="openai",
            base_url="https://api.openai.com",
        )
        adapter = OpenAIAdapter(config)
        captured = {}

        class FakeTransport:
            def with_retry(self, op, **k):
                return op()

            def post(self, url, headers, body, timeout=None):
                captured["timeout"] = timeout
                return 200, {}, {
                    "choices": [{"message": {"content": "Hi", "tool_calls": []}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }

        client = LLMClient(adapter=adapter, transport=FakeTransport(), config=config)
        resp = client.chat(
            messages=[Message(role="user", content="hi")],
            timeout=httpx.Timeout(123.0),
        )
        assert captured["timeout"].read == 123.0
        assert resp.get_text() == "Hi"
