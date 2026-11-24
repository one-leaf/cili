"""LLMClient 测试：工厂、结构化输出修复与 DGX 集成。"""


import pytest

from core.config import ResolvedModel
from core.llm import (
    AnthropicAdapter,
    LLMClient,
    Message,
    OpenAIAdapter,
    create_llm_client,
)
from test.support import make_dgx_config


class TestFactory:
    def test_create_anthropic_client(self):
        config = ResolvedModel(name="test", api_key="key", protocol="anthropic")
        client = create_llm_client(config)
        assert isinstance(client.adapter, AnthropicAdapter)
        client.close()

    def test_create_openai_client(self):
        config = ResolvedModel(name="gpt-4o", api_key="key", protocol="openai")
        client = create_llm_client(config)
        assert isinstance(client.adapter, OpenAIAdapter)
        client.close()

    def test_unknown_type_raises_error(self):
        config = ResolvedModel(name="test", api_key="key", protocol="unknown")
        with pytest.raises(ValueError, match="Unsupported protocol"):
            create_llm_client(config)


# ========== AnthropicAdapter ==========


class TestChatStructuredRetry:
    """chat_structured 解析失败时单次重试：坏 JSON 是 LLM 偶发失败，重试通常恢复。"""

    @staticmethod
    def _client(calls, arguments_first, arguments_second):
        config = ResolvedModel(
            name="gpt-4o", api_key="key", protocol="openai",
            base_url="https://api.openai.com",
        )
        adapter = OpenAIAdapter(config)

        def _resp(arguments):
            return 200, {}, {
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1", "type": "function",
                            "function": {"name": "output", "arguments": arguments},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            }

        class FakeTransport:
            def with_retry(self, op, **k):
                return op()

            def post(self, url, headers, body, timeout=None):
                calls.append(body)
                return _resp(arguments_first if len(calls) == 1 else arguments_second)

        return LLMClient(adapter=adapter, transport=FakeTransport(), config=config)

    def test_retries_once_on_bad_json(self):
        calls = []
        client = self._client(
            calls,
            '{"memories": [broken',  # 第一次：无法修复的坏 JSON（无完整元素边界）
            '{"memories": [{"type": "fact", "title": "ok"}]}',  # 第二次：合法
        )
        out = client.chat_structured(
            messages=[Message(role="user", content="hi")],
            output_schema={"type": "object", "properties": {}},
        )
        assert out == {"memories": [{"type": "fact", "title": "ok"}]}
        assert len(calls) == 2  # 只重试一次

    def test_gives_up_after_second_failure(self):
        calls = []
        client = self._client(calls, "{bad", "{bad}")  # 两次都坏 → 抛出
        with pytest.raises(ValueError, match="Failed to parse structured output"):
            client.chat_structured(
                messages=[Message(role="user", content="hi")],
                output_schema={"type": "object", "properties": {}},
            )
        assert len(calls) == 2  # 重试后仍失败才抛出


class TestChatStructuredTruncationRepair:
    """工具参数 JSON 被 max_tokens 截断时，_repair_truncated_json 抢救完整前缀。

    回归：整合/提取输出常在字符串中间、元素中间或元素结束后被截断，旧实现直接
    抛错导致整批失败；修复后保留已完成元素，仅丢弃末尾不完整元素。
    """

    @staticmethod
    def _client(calls, arguments):
        config = ResolvedModel(
            name="gpt-4o", api_key="key", protocol="openai",
            base_url="https://api.openai.com",
        )
        adapter = OpenAIAdapter(config)

        def _resp():
            return 200, {}, {
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1", "type": "function",
                            "function": {"name": "output", "arguments": arguments},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            }

        class FakeTransport:
            def with_retry(self, op, **k):
                return op()

            def post(self, url, headers, body, timeout=None):
                calls.append(body)
                return _resp()

        return LLMClient(adapter=adapter, transport=FakeTransport(), config=config)

    def test_missing_closing_brace_repaired(self):
        """截断在最后一个元素之后（缺 ]}）→ 补齐闭合括号后解析，无需重试。"""
        calls = []
        args = ('{"ops": [{"op": "store", "type": "fact", "title": "A", "content": "a"},'
                ' {"op": "update", "name": "b", "content": "b2"}')  # 缺末尾 ]}
        client = self._client(calls, args)
        out = client.chat_structured(
            messages=[Message(role="user", content="hi")],
            output_schema={"type": "object", "properties": {}},
        )
        assert out["ops"][1] == {"op": "update", "name": "b", "content": "b2"}
        assert len(calls) == 1  # 修复成功，不触发重试

    def test_truncated_mid_element_drops_tail(self):
        """截断在最后一个元素中间（未闭合字符串）→ 丢弃不完整尾部，保留完整元素。"""
        calls = []
        args = ('{"ops": [{"op": "store", "title": "ok", "content": "c"},'
                ' {"op": "update", "name": "x", "content": "partial')
        client = self._client(calls, args)
        out = client.chat_structured(
            messages=[Message(role="user", content="hi")],
            output_schema={"type": "object", "properties": {}},
        )
        assert out == {"ops": [{"op": "store", "title": "ok", "content": "c"}]}
        assert len(calls) == 1

    def test_unrepairable_truncation_still_raises(self):
        """无任何完整元素边界 → 无法修复，走单次重试后抛出。"""
        calls = []
        client = self._client(calls, '{"ops": [{"op": "store", "content": "ab')
        with pytest.raises(ValueError, match="Failed to parse structured output"):
            client.chat_structured(
                messages=[Message(role="user", content="hi")],
                output_schema={"type": "object", "properties": {}},
            )
        assert len(calls) == 2


class TestChatStructuredTextFallback:
    """end_turn（模型未调用工具但正文含 JSON）时从正文解析的兜底。

    回归：小模型/工具调用异常时常直接输出 '{"ops": [...]}' 而非调用工具，
    旧实现直接抛错导致提取/整合退化。
    """

    @staticmethod
    def _client(calls, *responses):
        config = ResolvedModel(
            name="gpt-4o", api_key="key", protocol="openai",
            base_url="https://api.openai.com",
        )
        adapter = OpenAIAdapter(config)

        def _resp(content):
            return 200, {}, {
                "choices": [{
                    "message": {"content": content},  # 无 tool_calls → end_turn
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            }

        class FakeTransport:
            def with_retry(self, op, **k):
                return op()

            def post(self, url, headers, body, timeout=None):
                calls.append(body)
                return _resp(responses[len(calls) - 1])

        return LLMClient(adapter=adapter, transport=FakeTransport(), config=config)

    def test_end_turn_text_json_parsed(self):
        calls = []
        client = self._client(
            calls,
            '{"memories": [{"type": "fact", "title": "t", "content": "c"}]}',
        )
        out = client.chat_structured(
            messages=[Message(role="user", content="hi")],
            output_schema={"type": "object", "properties": {}},
        )
        assert out == {"memories": [{"type": "fact", "title": "t", "content": "c"}]}
        assert len(calls) == 1  # 正文解析成功，无需重试

    def test_end_turn_fenced_json_parsed(self):
        """markdown 围栏包裹的 JSON 也能解析。"""
        calls = []
        client = self._client(calls, '```json\n{"ops": []}\n```')
        out = client.chat_structured(
            messages=[Message(role="user", content="hi")],
            output_schema={"type": "object", "properties": {}},
        )
        assert out == {"ops": []}
        assert len(calls) == 1

    def test_end_turn_prose_wrapping_json_parsed(self):
        """散文包裹的 JSON（'Here you go: {json}'）也能解析。"""
        calls = []
        client = self._client(calls, 'Here you go:\n{"ops": []}\nDone.')
        out = client.chat_structured(
            messages=[Message(role="user", content="hi")],
            output_schema={"type": "object", "properties": {}},
        )
        assert out == {"ops": []}
        assert len(calls) == 1

    def test_end_turn_prose_raises(self):
        """纯散文（无 JSON）仍失败并重试一次。"""
        calls = []
        client = self._client(calls, "No memories found.", "Still nothing.")
        with pytest.raises(ValueError, match="Expected tool call"):
            client.chat_structured(
                messages=[Message(role="user", content="hi")],
                output_schema={"type": "object", "properties": {}},
            )
        assert len(calls) == 2  # 重试后仍失败才抛出


# ========== DGX 集成测试（真实 LLM 调用） ==========



@pytest.mark.integration


class TestLLMClientDGX:
    """使用 DGX 本地端点的真实 LLM 调用测试，覆盖 Anthropic 和 OpenAI 协议。

    依赖 dgx_available fixture：服务器不可达时整类跳过（A45 §2.1）。
    """

    @pytest.fixture(params=["anthropic", "openai"], ids=["anthropic", "openai"])
    def dgx_client(self, request, dgx_available):
        """创建 DGX LLMClient，参数化为两种协议。"""
        config = make_dgx_config(request.param)
        client = create_llm_client(config.model_for_role("master"))
        yield client, request.param
        client.close()

    def test_connection(self, dgx_client):
        """test_connection() 对 DGX 端点返回成功。"""
        client, protocol = dgx_client
        success, message = client.test_connection()
        assert success is True, f"[{protocol}] test_connection() failed: {message}"

    def test_chat_basic(self, dgx_client):
        """chat() 基本调用：返回文本响应。"""
        client, protocol = dgx_client
        response = client.chat(
            messages=[Message(role="user", content="Say 'hello' in one word.")],
            max_tokens=500,
        )
        text = response.get_text()
        assert len(text) > 0, f"[{protocol}] Should return non-empty text"
        assert response.stop_reason in ("end_turn", "stop", "length", "max_tokens"), \
            f"[{protocol}] Unexpected stop_reason: {response.stop_reason}"

    def test_chat_chinese(self, dgx_client):
        """chat() 中文支持。"""
        client, protocol = dgx_client
        response = client.chat(
            messages=[Message(role="user", content="用一句话介绍北京")],
            max_tokens=1000,
        )
        text = response.get_text()
        # DGX 的 OpenAI 兼容层可能将全部 tokens 用于 reasoning，导致 text 为空
        # 此时 stop_reason 应为 length（截断）
        if not text:
            # anthropic: stop_reason='max_tokens'; openai: stop_reason='length'
            assert response.stop_reason in ("length", "max_tokens"), \
                f"[{protocol}] Empty text but stop_reason={response.stop_reason}"
        else:
            assert len(text) > 0

    def test_chat_stream(self, dgx_client):
        """chat_stream() 流式调用（通过 on_text 回调收集文本）。"""
        client, protocol = dgx_client
        text_parts = []

        response = client.chat_stream(
            messages=[Message(role="user", content="Count from 1 to 5, separated by commas.")],
            max_tokens=500,
            on_text=lambda t: text_parts.append(t),
        )

        # chat_stream 返回累积的 LLMResponse
        assert response is not None
        full_text = response.get_text()
        # DGX 的 OpenAI 兼容层可能将全部 tokens 用于 reasoning，导致 text 为空
        # 此时 stop_reason 应为 length/max_tokens（截断）——同 test_chat_chinese
        if not full_text:
            assert response.stop_reason in ("length", "max_tokens"), \
                f"[{protocol}] Empty streamed text but stop_reason={response.stop_reason}"
        # on_text 回调也应该收到内容（可能为空，取决于模型是否返回 reasoning）
        # 不强制断言 on_text 有内容，因为 reasoning 模型的 text 部分可能延迟到达

    def test_usage_returned(self, dgx_client):
        """chat() 返回 usage 信息。"""
        client, protocol = dgx_client
        response = client.chat(
            messages=[Message(role="user", content="1+1=?")],
            max_tokens=500,
        )
        assert response.usage is not None
        assert response.usage.input_tokens > 0, f"[{protocol}] Should have input_tokens"
        assert response.usage.output_tokens > 0, f"[{protocol}] Should have output_tokens"
