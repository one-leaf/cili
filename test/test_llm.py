"""LLM client tests - covers factory, adapters, retry logic, response parsing."""

import json
import pytest
from unittest.mock import patch

from core.config import ModelConfig
from core.llm import (
    LLMClient,
    LLMResponse,
    AnthropicAdapter,
    OpenAIAdapter,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UsageData,
    create_llm_client,
    _RETRY_STATUS_CODES,
)
from core.llm.transport import HttpTransport


# ========== LLMResponse ==========

class TestLLMResponse:
    def test_default_fields(self):
        resp = LLMResponse(content=[])
        assert resp.content == []
        assert resp.stop_reason == ""
        assert resp.usage == UsageData()
        assert resp.headers == {}

    def test_with_values(self):
        blocks = [TextBlock(text="hello")]
        usage = UsageData(input_tokens=10, output_tokens=20)
        resp = LLMResponse(
            content=blocks,
            stop_reason="end_turn",
            usage=usage,
            headers={"x-test": "1"},
        )
        assert resp.content == blocks
        assert resp.stop_reason == "end_turn"
        assert resp.usage.input_tokens == 10
        assert resp.headers["x-test"] == "1"

    def test_get_text(self):
        resp = LLMResponse(content=[TextBlock(text="hello"), TextBlock(text=" world")])
        assert resp.get_text() == "hello world"

    def test_get_tool_calls(self):
        tc = ToolCallBlock(id="t1", name="read", arguments='{"path": "test.py"}')
        resp = LLMResponse(content=[TextBlock(text="hi"), tc])
        tool_calls = resp.get_tool_calls()
        assert len(tool_calls) == 1
        assert tool_calls[0].name == "read"


# ========== Retry Logic ==========

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


# ========== Factory ==========

class TestFactory:
    def test_create_anthropic_client(self):
        config = ModelConfig(name="test", api_key="key", interface_type="anthropic")
        client = create_llm_client(config)
        assert isinstance(client.adapter, AnthropicAdapter)
        client.close()

    def test_create_openai_client(self):
        config = ModelConfig(name="gpt-4o", api_key="key", interface_type="openai")
        client = create_llm_client(config)
        assert isinstance(client.adapter, OpenAIAdapter)
        client.close()

    def test_unknown_type_raises_error(self):
        config = ModelConfig(name="test", api_key="key", interface_type="unknown")
        with pytest.raises(ValueError, match="Unsupported interface_type"):
            create_llm_client(config)


# ========== AnthropicAdapter ==========

class TestAnthropicAdapter:
    @pytest.fixture
    def adapter(self):
        config = ModelConfig(
            name="claude-sonnet-4-6",
            api_key="test-key-123",
            base_url="https://api.anthropic.com",
            max_tokens=4096,
            interface_type="anthropic",
        )
        return AnthropicAdapter(config)

    def test_api_url(self, adapter):
        assert adapter.api_url == "https://api.anthropic.com/v1/messages"

    def test_build_headers(self, adapter):
        headers = adapter.build_headers()
        assert headers["x-api-key"] == "test-key-123"
        assert headers["anthropic-version"] == "2023-06-01"
        assert headers["content-type"] == "application/json"

    def test_serialize_minimal(self, adapter):
        messages = [Message(role="user", content="hi")]
        body = adapter.serialize(messages, system="", tools=None, model="claude-sonnet-4-6", max_tokens=4096)
        assert body["model"] == "claude-sonnet-4-6"
        assert body["max_tokens"] == 4096
        assert len(body["messages"]) == 1
        assert body["messages"][0]["role"] == "user"
        assert "system" not in body
        assert "tools" not in body
        assert "stream" not in body

    def test_serialize_full(self, adapter):
        messages = [Message(role="user", content="hi")]
        tools = [{"name": "read", "description": "read files"}]
        body = adapter.serialize(messages, system="You are helpful", tools=tools, model="claude-sonnet-4-6", max_tokens=4096, stream=True)
        assert body["system"] == "You are helpful"
        assert body["tools"] == tools
        assert body["stream"] is True

    def test_serialize_litellm_session(self, adapter):
        adapter._is_litellm_proxy = True
        messages = [Message(role="user", content="hi")]
        body = adapter.serialize(messages, system="", tools=None, model="claude-sonnet-4-6", max_tokens=4096, session_id="session-123")
        assert body["litellm_session_id"] == "session-123"

    def test_serialize_no_litellm_session_when_not_proxy(self, adapter):
        messages = [Message(role="user", content="hi")]
        body = adapter.serialize(messages, system="", tools=None, model="claude-sonnet-4-6", max_tokens=4096, session_id="session-123")
        assert "litellm_session_id" not in body

    def test_parse_response_text_only(self, adapter):
        data = {
            "content": [{"type": "text", "text": "Hello!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        blocks, stop_reason, usage = adapter.parse_response(data)
        assert len(blocks) == 1
        assert isinstance(blocks[0], TextBlock)
        assert blocks[0].text == "Hello!"
        assert stop_reason == "end_turn"
        assert usage.input_tokens == 10
        assert usage.output_tokens == 5

    def test_parse_response_with_tool_use(self, adapter):
        data = {
            "content": [
                {"type": "text", "text": "Let me read that."},
                {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "read",
                    "input": {"file_path": "test.py"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 20, "output_tokens": 15},
        }
        blocks, stop_reason, usage = adapter.parse_response(data)
        assert len(blocks) == 2
        assert isinstance(blocks[1], ToolCallBlock)
        assert blocks[1].id == "tool_1"
        assert blocks[1].name == "read"
        # Arguments are raw JSON string, parse to check
        args = blocks[1].parse_arguments()
        assert args["file_path"] == "test.py"
        assert stop_reason == "tool_use"

    def test_parse_response_cache_tokens(self, adapter):
        data = {
            "content": [],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 80,
                "cache_creation_input_tokens": 10,
            },
        }
        _, _, usage = adapter.parse_response(data)
        assert usage.cache_read_tokens == 80
        assert usage.cache_write_tokens == 10


# ========== OpenAIAdapter ==========

class TestOpenAIAdapter:
    @pytest.fixture
    def adapter(self):
        config = ModelConfig(
            name="gpt-4o",
            api_key="sk-test-key",
            base_url="https://api.openai.com",
            max_tokens=4096,
            interface_type="openai",
        )
        return OpenAIAdapter(config)

    def test_api_url(self, adapter):
        assert adapter.api_url == "https://api.openai.com/v1/chat/completions"

    def test_build_headers(self, adapter):
        headers = adapter.build_headers()
        assert headers["Authorization"] == "Bearer sk-test-key"
        assert headers["content-type"] == "application/json"

    # --- Tool conversion ---

    def test_convert_tools_none(self, adapter):
        assert adapter._convert_tools(None) is None

    def test_convert_tools_empty(self, adapter):
        assert adapter._convert_tools([]) is None

    def test_convert_tools_with_input_schema(self, adapter):
        tools = [{
            "name": "read",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        }]
        result = adapter._convert_tools(tools)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "read"
        assert result[0]["function"]["parameters"]["type"] == "object"

    def test_convert_tools_with_parameters_fallback(self, adapter):
        tools = [{
            "name": "bash",
            "description": "Run shell",
            "parameters": {"type": "object", "properties": {}},
        }]
        result = adapter._convert_tools(tools)
        assert result[0]["function"]["parameters"]["type"] == "object"

    # --- Message conversion ---

    def test_convert_messages_system(self, adapter):
        messages = [Message(role="user", content="hello")]
        result = adapter._convert_messages(messages, system="You are helpful")
        assert result[0] == {"role": "system", "content": "You are helpful"}
        assert result[1] == {"role": "user", "content": "hello"}

    def test_convert_messages_no_system(self, adapter):
        messages = [Message(role="user", content="hello")]
        result = adapter._convert_messages(messages, system="")
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_convert_messages_string_content(self, adapter):
        messages = [
            Message(role="user", content="question"),
            Message(role="assistant", content="answer"),
        ]
        result = adapter._convert_messages(messages, system="")
        assert result[0]["content"] == "question"
        assert result[1]["content"] == "answer"

    def test_convert_messages_assistant_with_tool_use(self, adapter):
        messages = [Message(
            role="assistant",
            content=[
                TextBlock(text="Let me read that."),
                ToolCallBlock(id="t1", name="read", arguments='{"path": "a.py"}'),
            ],
        )]
        result = adapter._convert_messages(messages, system="")
        msg = result[0]
        # OpenAI 格式：content 是字符串，tool_calls 在消息顶层
        assert msg["role"] == "assistant"
        assert msg["content"] == "Let me read that."
        assert "tool_calls" in msg
        tc = msg["tool_calls"][0]
        assert tc["function"]["name"] == "read"
        assert json.loads(tc["function"]["arguments"]) == {"path": "a.py"}

    def test_convert_messages_user_with_tool_result(self, adapter):
        messages = [Message(
            role="user",
            content=[
                TextBlock(text="Here is the result"),
                ToolResultBlock(tool_call_id="t1", content="file content"),
            ],
        )]
        result = adapter._convert_messages(messages, system="")
        # OpenAI 格式：tool_result 转换为独立的 role=tool 消息
        # 查找 tool 消息
        tool_msgs = [m for m in result if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        tool_msg = tool_msgs[0]
        assert tool_msg["tool_call_id"] == "t1"
        assert "file content" in tool_msg["content"]

    # --- Response parsing ---

    def test_parse_response_text(self, adapter):
        data = {
            "choices": [{
                "message": {"content": "Hello!", "tool_calls": []},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        blocks, stop_reason, usage = adapter.parse_response(data)
        assert isinstance(blocks[0], TextBlock)
        assert blocks[0].text == "Hello!"
        assert stop_reason == "end_turn"
        assert usage.input_tokens == 10
        assert usage.output_tokens == 5

    def test_parse_response_tool_calls(self, adapter):
        data = {
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "function": {
                            "name": "read",
                            "arguments": '{"path": "test.py"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 20, "completion_tokens": 15},
        }
        blocks, stop_reason, usage = adapter.parse_response(data)
        assert isinstance(blocks[0], ToolCallBlock)
        assert blocks[0].id == "call_1"
        assert blocks[0].name == "read"
        # Arguments are raw JSON string, parse to check
        args = blocks[0].parse_arguments()
        assert args["path"] == "test.py"
        assert stop_reason == "tool_use"

    def test_parse_response_invalid_json_args(self, adapter):
        data = {
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "function": {
                            "name": "bash",
                            "arguments": "invalid json{",
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {},
        }
        blocks, stop_reason, usage = adapter.parse_response(data)
        assert isinstance(blocks[0], ToolCallBlock)
        # Invalid JSON is stored as raw string, parse_arguments returns error dict
        args = blocks[0].parse_arguments()
        assert args.get("_raw") == "invalid json{"
        assert args.get("_parse_error") is True

    def test_parse_response_finish_reason_mapping(self, adapter):
        for openai_reason, expected in [
            ("stop", "end_turn"),
            ("tool_calls", "tool_use"),
            ("length", "length"),
        ]:
            data = {
                "choices": [{"message": {"content": "ok"}, "finish_reason": openai_reason}],
                "usage": {},
            }
            _, stop_reason, _ = adapter.parse_response(data)
            assert stop_reason == expected, f"{openai_reason} -> {expected}"

    def test_parse_response_cache_tokens_zero(self, adapter):
        data = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        _, _, usage = adapter.parse_response(data)
        assert usage.cache_read_tokens == 0
        assert usage.cache_write_tokens == 0

    # --- Serialize ---

    def test_serialize_basic(self, adapter):
        messages = [Message(role="user", content="hi")]
        body = adapter.serialize(messages, system="be helpful", tools=None, model="gpt-4o", max_tokens=4096)
        assert body["model"] == "gpt-4o"
        assert body["max_tokens"] == 4096
        # Check system message was converted
        assert body["messages"][0] == {"role": "system", "content": "be helpful"}
        assert body["messages"][1] == {"role": "user", "content": "hi"}

    def test_serialize_with_tools(self, adapter):
        messages = [Message(role="user", content="hi")]
        tools = [{"name": "read", "description": "read", "input_schema": {}}]
        body = adapter.serialize(messages, system="", tools=tools, model="gpt-4o", max_tokens=4096)
        assert "tools" in body
        assert body["tools"][0]["function"]["name"] == "read"

    def test_serialize_stream(self, adapter):
        messages = [Message(role="user", content="hi")]
        body = adapter.serialize(messages, system="", tools=None, model="gpt-4o", max_tokens=4096, stream=True)
        assert body["stream"] is True

    def test_base_url_trailing_slash_stripped(self):
        config = ModelConfig(
            name="gpt-4o",
            api_key="key",
            base_url="https://api.openai.com/",
            interface_type="openai",
        )
        adapter = OpenAIAdapter(config)
        assert adapter.base_url == "https://api.openai.com"
        assert adapter.api_url == "https://api.openai.com/v1/chat/completions"


# ========== DGX 集成测试（真实 LLM 调用） ==========

from test.conftest import DGX_BASE_URL, DGX_API_KEY, DGX_MODEL, make_dgx_config


class TestLLMClientDGX:
    """使用 DGX 本地端点的真实 LLM 调用测试，覆盖 Anthropic 和 OpenAI 协议。"""

    @pytest.fixture(params=["anthropic", "openai"], ids=["anthropic", "openai"])
    def dgx_client(self, request):
        """创建 DGX LLMClient，参数化为两种协议。"""
        config = make_dgx_config(request.param)
        client = create_llm_client(config.model)
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
        assert len(full_text) > 0, f"[{protocol}] Should return non-empty text via stream"
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

