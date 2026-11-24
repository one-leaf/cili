"""LLM Adapter 测试：Anthropic / OpenAI 序列化与 URL 拼接。"""

import json

import pytest

from core.config import ResolvedModel
from core.llm import (
    AnthropicAdapter,
    Message,
    OpenAIAdapter,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    create_llm_client,
)
from core.llm.adapter import join_api_url
from core.llm.types import block_from_dict


class TestAnthropicAdapter:
    @pytest.fixture
    def adapter(self):
        config = ResolvedModel(
            name="claude-sonnet-4-6",
            api_key="test-key-123",
            base_url="https://api.anthropic.com",
            max_output_tokens=4096,
            protocol="anthropic",
        )
        return AnthropicAdapter(config)

    def test_api_url(self, adapter):
        assert adapter.api_url == "https://api.anthropic.com/v1/messages"

    def _body_with_reasoning(self, reasoning: str) -> dict:
        config = ResolvedModel(
            name="claude-sonnet-4-6",
            api_key="key",
            base_url="https://api.anthropic.com",
            protocol="anthropic",
            reasoning=reasoning,
        )
        return AnthropicAdapter(config).serialize(
            [Message(role="user", content="hi")],
            system="", tools=None, model="claude-sonnet-4-6", max_tokens=32768,
        )

    def test_thinking_disabled_for_off(self):
        """reasoning='off' 显式发 thinking.disabled。

        回归：部分网关（litellm 等）对「不传 thinking」的默认是**开**思考，
        原先只「不发」等于没关（实测单次判定 ~27s vs 关掉后 ~6s）。
        """
        assert self._body_with_reasoning("off")["thinking"] == {"type": "disabled"}

    def test_thinking_absent_for_default(self):
        """reasoning='default' 不发 thinking 参数，交给服务端默认。"""
        assert "thinking" not in self._body_with_reasoning("default")

    def test_thinking_budget_for_high(self):
        assert self._body_with_reasoning("high")["thinking"] == {
            "type": "enabled", "budget_tokens": 10000,
        }

    def test_thinking_skipped_for_chat_model(self):
        """model_type='chat' 不发送 thinking，即使配置了 medium 档位。"""
        config = ResolvedModel(
            name="chat-model",
            api_key="key",
            base_url="https://api.anthropic.com",
            protocol="anthropic",
            reasoning="medium",
            model_type="chat",
        )
        body = AnthropicAdapter(config).serialize(
            [Message(role="user", content="hi")],
            system="", tools=None, model="chat-model", max_tokens=100,
        )
        assert "thinking" not in body

    def test_translate_stream_signature_delta(self, adapter):
        """Anthropic 流式 signature_delta 必须转成 StreamChunk，供 thinking 重放。"""
        events = [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "sig-abc"}},
            {"type": "content_block_stop", "index": 0},
        ]
        chunks = list(adapter.translate_stream(events))
        sig = [c for c in chunks if c.type == "signature_delta"]
        assert len(sig) == 1
        assert sig[0].data["signature"] == "sig-abc"

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
        # 块列表内容（agent 会话的典型形态），最后一块可打缓存断点
        messages = [Message(role="user", content=[TextBlock(text="hi")])]
        tools = [{"name": "read", "description": "read files"}]
        body = adapter.serialize(messages, system="You are helpful", tools=tools, model="claude-sonnet-4-6", max_tokens=4096, stream=True)
        # 官方端点启用 prompt cache：system 静态区带 scope='global' 缓存
        assert body["system"] == [{
            "type": "text",
            "text": "You are helpful",
            "cache_control": {"type": "ephemeral", "scope": "global"},
        }]
        # tools 最后一项打 cache_control 断点
        assert body["tools"][-1].get("cache_control") == {"type": "ephemeral"}
        # 最后一条消息的最后一块打 messages 尾部缓存断点
        assert body["messages"][-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert body["stream"] is True

    def test_serialize_string_content_untouched(self, adapter):
        # 字符串内容保持原样（不打 cache_control，也不崩）
        messages = [Message(role="user", content="hi")]
        body = adapter.serialize(messages, system="You are helpful", tools=None, model="claude-sonnet-4-6", max_tokens=4096)
        assert body["messages"][-1]["content"] == "hi"

    def test_backend_session_headers(self):
        """backend 预设 → 请求头按 {session_id} 展开；无会话整条省略"""
        from core.config import ResolvedModel

        cfg = ResolvedModel(name="m", api_key="k", base_url="https://x.example.com", backend="litellm")
        adapter = AnthropicAdapter(cfg)
        headers = adapter.build_request_headers(session_id="session-123")
        assert headers["x-litellm-session-id"] == "session-123"
        assert "x-litellm-session-id" not in adapter.build_request_headers()

    def test_configured_headers_session_placeholder(self):
        """headers 值支持 {session_id} 插值；显式值覆盖预设"""
        from core.config import ResolvedModel

        cfg = ResolvedModel(
            name="m", api_key="k", base_url="https://x.example.com",
            backend="litellm", headers={"X-Session-ID": "{session_id}", "X-Extra": "static"},
        )
        adapter = AnthropicAdapter(cfg)
        headers = adapter.build_request_headers(session_id="s1")
        assert headers["X-Session-ID"] == "s1"
        assert headers["X-Extra"] == "static"
        no_session = adapter.build_request_headers()
        assert "X-Session-ID" not in no_session and no_session["X-Extra"] == "static"

    def test_auth_type_bearer_overrides_default(self):
        """auth_type=bearer 强制 Authorization，替换 anthropic 默认 x-api-key"""
        from core.config import ResolvedModel

        cfg = ResolvedModel(name="m", api_key="k", base_url="https://x.example.com", auth_type="bearer")
        headers = AnthropicAdapter(cfg).build_request_headers()
        assert headers["Authorization"] == "Bearer k"
        assert "x-api-key" not in headers
        assert headers["anthropic-version"] == "2023-06-01"  # 协议头保留

    def test_auth_type_none_drops_auth(self):
        from core.config import ResolvedModel

        cfg = ResolvedModel(name="m", api_key="k", base_url="https://x.example.com", auth_type="none")
        headers = AnthropicAdapter(cfg).build_request_headers()
        assert "x-api-key" not in headers and "Authorization" not in headers

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
        config = ResolvedModel(
            name="gpt-4o",
            api_key="sk-test-key",
            base_url="https://api.openai.com",
            max_output_tokens=4096,
            protocol="openai",
        )
        return OpenAIAdapter(config)

    def test_api_url(self, adapter):
        assert adapter.api_url == "https://api.openai.com/v1/chat/completions"

    def _body(self, reasoning: str, model_type: str = "reasoning") -> dict:
        config = ResolvedModel(
            name="gpt-4o",
            api_key="sk-test-key",
            base_url="https://api.openai.com",
            protocol="openai",
            reasoning=reasoning,
            model_type=model_type,
        )
        return OpenAIAdapter(config).serialize(
            [Message(role="user", content="hi")],
            system="", tools=None, model="gpt-4o", max_tokens=100,
        )

    def test_reasoning_effort_passed_for_reasoning_model(self):
        assert self._body("medium")["reasoning_effort"] == "medium"

    def test_reasoning_effort_skipped_for_chat_model(self):
        assert "reasoning_effort" not in self._body("medium", model_type="chat")

    def test_parse_modelscope_null_fields(self, adapter):
        """ModelScope 响应带显式 null 字段（tool_calls/function_calls、空 content），parse 不得崩。"""
        data = {
            "id": "chatcmpl-x",
            "object": "chat.completion",
            "model": "deepseek-ai/DeepSeek-V4.1-Flash",
            "choices": [{
                "index": 0,
                "delta": {"role": None, "content": "", "tool_calls": None,
                          "function_calls": None, "reasoning_content": ""},
                "finish_reason": "length",
                "message": {"role": "assistant", "content": "", "tool_calls": None,
                            "function_calls": None, "reasoning_content": "We need answer."},
            }],
            "usage": {"prompt_tokens": 31, "completion_tokens": 8, "total_tokens": 39},
        }
        blocks, stop_reason, usage = adapter.parse_response(data)
        assert [type(b).__name__ for b in blocks] == ["ReasoningBlock"]   # content 空串不产 TextBlock
        assert stop_reason == "length"          # 未知 finish_reason 原样透传
        assert usage.input_tokens == 31
        assert usage.output_tokens == 8

    def test_translate_stream_tolerates_null_tool_calls(self, adapter):
        """流式 chunk 的 delta.tool_calls / delta 为显式 null 时不得崩（ModelScope 实际行为）。"""
        events = [
            {"usage": None,
             "choices": [{"delta": {"role": "assistant", "content": "Hi", "tool_calls": None},
                          "finish_reason": None}]},
            {"choices": [{"delta": None, "finish_reason": "stop"}]},
        ]
        chunks = list(adapter.translate_stream(events))
        assert any(c.type == "text_delta" for c in chunks)
        assert any(c.type == "finish" and c.data["stop_reason"] == "end_turn" for c in chunks)

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

    def test_convert_messages_reasoning_only_assistant(self, adapter):
        """P1-20 回归：纯 reasoning 助手消息不得被静默丢弃。"""
        from core.llm.types import ReasoningBlock

        messages = [Message(role="assistant", content=[ReasoningBlock(text="思考过程")])]
        result = adapter._convert_messages(messages, system="")
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] is None
        assert result[0]["reasoning_content"] == "思考过程"

    def test_convert_messages_user_with_tool_result(self, adapter):
        messages = [Message(
            role="user",
            content=[
                TextBlock(text="Here is the result"),
                ToolResultBlock(tool_use_id="t1", content="file content"),
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

    def test_convert_messages_user_tool_result_preserves_text(self, adapter):
        """含 tool_result 的 user 消息中的文本不能被丢弃（S10 回归）。"""
        messages = [Message(
            role="user",
            content=[
                TextBlock(text="请执行这个任务"),
                ToolResultBlock(tool_use_id="t1", content="file content"),
            ],
        )]
        result = adapter._convert_messages(messages, system="")
        tool_msgs = [m for m in result if m["role"] == "tool"]
        user_msgs = [m for m in result if m["role"] == "user"]
        assert len(tool_msgs) == 1
        # 文本必须保留，以独立 user 消息形式发给模型
        assert any("请执行这个任务" in m["content"] for m in user_msgs), \
            f"用户文本被丢弃: {result}"

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

    def test_parse_response_refusal(self, adapter):
        """OpenAI refusal 字段不能被静默丢弃（S11 回归）。"""
        data = {
            "choices": [{
                "message": {"content": None, "refusal": "I can't help with that."},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        blocks, stop_reason, usage = adapter.parse_response(data)
        assert len(blocks) == 1
        assert isinstance(blocks[0], TextBlock)
        assert "I can't help with that." in blocks[0].text
        assert stop_reason == "end_turn"

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

    def test_parse_arguments_legacy_raw_wrapper_marked(self):
        """旧会话落盘的 {"_raw": ...} 包装（无标记）→ parse_arguments 补 _parse_error。"""
        block = ToolCallBlock(
            id="call_1", name="write",
            arguments='{"_raw": "{\\"file_path\\": \\"a.md\\", \\"content\\": \\"abc"}',
        )
        args = block.parse_arguments()
        assert args["_parse_error"] is True
        assert args["_raw"].startswith('{"file_path"')

    def test_to_dict_preserves_parse_error(self):
        """落盘序列化保留 _parse_error，重载后再执行不会被误报成缺必填参数。"""
        block = ToolCallBlock(
            id="call_1", name="write",
            arguments='{"file_path": "a.md", "content": "abc',
        )
        serialized = block.to_dict()
        assert serialized["input"]["_parse_error"] is True
        reloaded = block_from_dict(serialized)
        assert reloaded.parse_arguments()["_parse_error"] is True

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
        config = ResolvedModel(
            name="gpt-4o",
            api_key="key",
            base_url="https://api.openai.com/",
            protocol="openai",
        )
        adapter = OpenAIAdapter(config)
        assert adapter.base_url == "https://api.openai.com"
        assert adapter.api_url == "https://api.openai.com/v1/chat/completions"

    def test_base_url_with_version_suffix_not_duplicated(self):
        """base_url 自带 /v1（ModelScope 等厂商文档惯例）时不再重复拼版本段。"""
        for base in ("https://api-inference.modelscope.cn/v1",
                     "https://api-inference.modelscope.cn/v1/"):
            config = ResolvedModel(
                name="deepseek-ai/DeepSeek-V4.1-Flash",
                api_key="key",
                base_url=base,
                protocol="openai",
            )
            adapter = OpenAIAdapter(config)
            assert adapter.api_url == "https://api-inference.modelscope.cn/v1/chat/completions"


class TestJoinApiUrl:
    """base_url 与 api_path 的重复段合并。"""

    def test_merge_single_version_segment(self):
        assert join_api_url("https://host/v1", "/v1/chat/completions") == "https://host/v1/chat/completions"
        assert join_api_url("https://host/v1/", "/v1/chat/completions") == "https://host/v1/chat/completions"
        assert join_api_url("https://host/api/v1", "/v1/messages") == "https://host/api/v1/messages"

    def test_merge_multiple_segments(self):
        assert join_api_url("https://host/v1/chat", "/v1/chat/completions") == "https://host/v1/chat/completions"

    def test_no_overlap_unchanged(self):
        assert join_api_url("https://host", "/v1/chat/completions") == "https://host/v1/chat/completions"
        assert join_api_url("http://gw/llm", "/v1/chat/completions") == "http://gw/llm/v1/chat/completions"

    def test_similar_segment_not_merged(self):
        """/v11 只是形似 /v1，不得误合并。"""
        assert join_api_url("https://host/v11", "/v1/chat/completions") == "https://host/v11/v1/chat/completions"

    def test_full_path_already_present(self):
        """path 整段已在 base 里时不再追加，且不留尾斜杠。"""
        assert join_api_url("https://host/v1/messages", "/v1/messages") == "https://host/v1/messages"


# ========== A23-A26: 重试 / SSE 边界 / base_url / 超时 ==========


class TestAdapterBaseUrl:
    """base_url 校验。"""

    def test_empty_base_url_raises(self):
        config = ResolvedModel(name="m", api_key="k", base_url="", protocol="anthropic")
        with pytest.raises(ValueError, match="不能为空"):
            AnthropicAdapter(config)

    def test_invalid_base_url_raises(self):
        config = ResolvedModel(name="m", api_key="k", base_url="not a url", protocol="anthropic")
        with pytest.raises(ValueError, match="无效的 base_url"):
            AnthropicAdapter(config)

    def test_backend_headers_all_presets(self):
        """当前预设的会话头名单；codex / claude-code / opencode 已彻底移除。"""
        from core.config import ResolvedModel
        from core.llm.adapter import BACKEND_SESSION_HEADERS

        assert "custom" not in BACKEND_SESSION_HEADERS
        assert BACKEND_SESSION_HEADERS["openrouter"] == {"x-session-id": "{session_id}"}
        assert BACKEND_SESSION_HEADERS["opencode-go"] == {"x-opencode-session": "{session_id}"}
        assert BACKEND_SESSION_HEADERS["litellm"] == {"x-litellm-session-id": "{session_id}"}
        for removed in ("codex", "claude-code", "opencode"):
            assert removed not in BACKEND_SESSION_HEADERS

        for backend, expected in BACKEND_SESSION_HEADERS.items():
            cfg = ResolvedModel(name="m", api_key="k", base_url="https://x.example.com", backend=backend)
            headers = AnthropicAdapter(cfg).build_request_headers(session_id="s")
            for header_name, template in expected.items():
                assert headers[header_name] == "s"

    def test_api_path_override(self):
        """provider.api_path 覆盖协议默认路径。"""
        cfg = ResolvedModel(
            name="m", api_key="k", base_url="https://api.deepseek.com",
            protocol="openai-responses", api_path="/responses",
        )
        assert create_llm_client(cfg).adapter.api_url == "https://api.deepseek.com/responses"

    def test_preset_static_headers(self):
        """Kimi 预设的 UA 头自动携带，显式 headers 同名覆盖。"""
        cfg = ResolvedModel(
            name="m", api_key="k", base_url="https://api.kimi.com/coding",
            protocol="anthropic", auth_type="bearer", backend="kimi-coding",
        )
        headers = AnthropicAdapter(cfg).build_request_headers(session_id="s")
        assert headers["User-Agent"] == "claude-code/0.1.0"

        cfg.headers = {"User-Agent": "custom-agent"}
        headers = AnthropicAdapter(cfg).build_request_headers(session_id="s")
        assert headers["User-Agent"] == "custom-agent"
