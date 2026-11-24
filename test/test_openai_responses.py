"""OpenAI Responses API adapter 测试。

覆盖请求序列化（instructions / input items / 扁平工具 / 思考回放）、
非流式解析、SSE 事件翻译与工厂注册。
"""

from __future__ import annotations

import json

import pytest

from core.config import ResolvedModel
from core.llm import BlockAssembler, create_llm_client
from core.llm.openai_responses import OpenAIResponsesAdapter
from core.llm.types import (
    ImageBlock,
    Message,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UsageData,
)


def make_config(**overrides) -> ResolvedModel:
    data = {
        "name": "gpt-5.1",
        "protocol": "openai-responses",
        "api_key": "test-key",
        "base_url": "https://api.openai.com/v1",
    }
    data.update(overrides)
    return ResolvedModel(**data)


# ========== 工厂与端点 ==========

def test_factory_creates_responses_adapter():
    client = create_llm_client(make_config())
    try:
        assert isinstance(client.adapter, OpenAIResponsesAdapter)
        assert client.adapter.api_url == "https://api.openai.com/v1/responses"
    finally:
        client.close()


def test_factory_keeps_openai_alias_for_chat_completions():
    from core.llm.openai import OpenAIAdapter

    for value in ("openai", "openai-compatible"):
        client = create_llm_client(make_config(protocol=value))
        try:
            assert isinstance(client.adapter, OpenAIAdapter)
        finally:
            client.close()


def test_chat_stream_uses_responses_endpoint_and_headers():
    """client 层接线：端点、认证头、stream 标记、会话缓存键。"""
    from core.llm.client import LLMClient

    config = make_config()
    adapter = OpenAIResponsesAdapter(config)
    captured: dict = {}

    class FakeTransport:
        def stream(self, url, headers, body, **kwargs):
            captured.update(url=url, headers=headers, body=body)
            return iter([
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {"type": "message", "id": "msg_1"}},
                {"type": "response.output_text.delta", "output_index": 0,
                 "item_id": "msg_1", "delta": "hi"},
                {"type": "response.completed", "response": {
                    "status": "completed",
                    "output": [{"type": "message", "id": "msg_1",
                                "content": [{"type": "output_text", "text": "hi"}]}],
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                }},
            ])

        def with_retry(self, op, **kwargs):
            return op()

        def close(self):
            pass

    client = LLMClient(adapter=adapter, transport=FakeTransport(), config=config)
    response = client.chat_stream(
        messages=[Message(role="user", content="hi")],
        session_id="session-123",
    )

    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["body"]["stream"] is True
    assert captured["body"]["prompt_cache_key"] == "session-123"
    assert response.stop_reason == "end_turn"
    assert response.get_text() == "hi"


# ========== 请求序列化 ==========

def test_serialize_basic_request():
    adapter = OpenAIResponsesAdapter(make_config())
    body = adapter.serialize(
        messages=[Message(role="user", content="你好")],
        system=["静态提示", "动态提示"],
        tools=[{
            "name": "read_file",
            "description": "读取文件",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }],
        model="gpt-5.1",
        max_tokens=4,
        stream=True,
        session_id="session-123",
    )

    assert body["model"] == "gpt-5.1"
    assert body["instructions"] == "静态提示\n\n动态提示"
    assert body["store"] is False
    assert body["stream"] is True
    assert body["max_output_tokens"] == 16  # Responses 下限
    assert body["prompt_cache_key"] == "session-123"
    assert body["tool_choice"] == "auto"
    assert body["tools"] == [{
        "type": "function",
        "name": "read_file",
        "description": "读取文件",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        "strict": False,
    }]
    assert body["input"] == [{
        "role": "user",
        "content": [{"type": "input_text", "text": "你好"}],
    }]
    # 默认 reasoning=medium
    assert body["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]


def test_serialize_filters_dynamic_boundary():
    from core.prompt_builder import DYNAMIC_BOUNDARY

    adapter = OpenAIResponsesAdapter(make_config())
    body = adapter.serialize(
        messages=[],
        system=["静态", DYNAMIC_BOUNDARY, "动态"],
        tools=None,
        model="gpt-5.1",
        max_tokens=100,
    )
    assert body["instructions"] == "静态\n\n动态"
    assert "tools" not in body
    assert "stream" not in body


def test_serialize_reasoning_off_and_chat_model():
    adapter = OpenAIResponsesAdapter(make_config(reasoning="off"))
    body = adapter.serialize([], "", None, "gpt-5.1", 100)
    assert body["reasoning"] == {"effort": "none"}
    assert "include" not in body

    chat = OpenAIResponsesAdapter(make_config(reasoning="medium", model_type="chat"))
    body = chat.serialize([], "", None, "gpt-4o", 100)
    assert "reasoning" not in body
    assert "include" not in body


def test_serialize_cache_disabled_omits_prompt_cache_key():
    adapter = OpenAIResponsesAdapter(make_config(cache_control="false"))
    body = adapter.serialize([], "", None, "gpt-5.1", 100, session_id="s1")
    assert "prompt_cache_key" not in body


def test_serialize_assistant_and_tool_result_replay():
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": "思考"}],
        "encrypted_content": "enc",
    }
    adapter = OpenAIResponsesAdapter(make_config())
    body = adapter.serialize(
        messages=[
            Message(role="user", content="读取文件"),
            Message(role="assistant", content=[
                ReasoningBlock(text="思考", signature=json.dumps(reasoning_item)),
                TextBlock(text="我来读取"),
                ToolCallBlock(
                    id="call_1", name="read_file", arguments='{"path":"a"}', item_id="fc_1"
                ),
            ]),
            Message(role="user", content=[
                ToolResultBlock(tool_use_id="call_1", content="文件内容"),
            ]),
        ],
        system="",
        tools=None,
        model="gpt-5.1",
        max_tokens=100,
    )

    assert body["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "读取文件"}]},
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "思考"}],
            "encrypted_content": "enc",
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "我来读取"}],
            "status": "completed",
        },
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": '{"path":"a"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "文件内容",
        },
    ]


def test_serialize_ignores_foreign_reasoning_signature():
    adapter = OpenAIResponsesAdapter(make_config(reasoning="medium"))
    body = adapter.serialize(
        [Message(role="assistant", content=[
            ReasoningBlock(text="anthropic thinking", signature="opaque-anthropic-signature"),
            TextBlock(text="回答"),
        ])],
        "",
        None,
        "gpt-5.1",
        100,
    )
    assert [item.get("type") for item in body["input"]] == ["message"]


def test_serialize_skips_reasoning_without_encrypted_content():
    """只有摘要、没有加密载荷的 reasoning item 不回放，避免配对校验 400。"""
    adapter = OpenAIResponsesAdapter(make_config())
    summary_only = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "思考"}],
    }
    body = adapter.serialize(
        [Message(role="assistant", content=[
            ReasoningBlock(text="思考", signature=json.dumps(summary_only)),
            TextBlock(text="回答"),
        ])],
        "",
        None,
        "gpt-5.1",
        100,
    )
    assert [item.get("type") for item in body["input"]] == ["message"]


def test_serialize_images_and_multimodal_tool_output():
    adapter = OpenAIResponsesAdapter(make_config())
    body = adapter.serialize(
        messages=[
            Message(role="user", content=[
                TextBlock(text="看图"),
                ImageBlock(data="aGVsbG8=", mime_type="image/png"),
            ]),
            Message(role="user", content=[
                ToolResultBlock(tool_use_id="call_2", content=[
                    {"type": "text", "text": "截图"},
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": "aGVsbG8=",
                    }},
                ]),
            ]),
        ],
        system="",
        tools=None,
        model="gpt-5.1",
        max_tokens=100,
    )

    # 相邻 user 消息会先合并，因此工具输出与图文消息在同一条输入里展开
    tool_item = body["input"][0]
    assert tool_item["type"] == "function_call_output"
    user_item = body["input"][1]
    assert user_item["content"] == [
        {"type": "input_text", "text": "看图"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,aGVsbG8=",
            "detail": "auto",
        },
    ]
    assert tool_item["output"] == [
        {"type": "input_text", "text": "截图"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,aGVsbG8=",
            "detail": "auto",
        },
    ]


# ========== 非流式解析 ==========

def test_parse_response_text_reasoning_and_tool_call():
    adapter = OpenAIResponsesAdapter(make_config())
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "思考"}],
        "encrypted_content": "enc",
    }
    data = {
        "status": "completed",
        "model": "gpt-5.1",
        "output": [
            reasoning_item,
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "你好"}],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": '{"path":"a"}',
            },
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 80},
        },
    }

    blocks, stop_reason, usage = adapter.parse_response(data)

    assert isinstance(blocks[0], ReasoningBlock)
    assert blocks[0].text == "思考"
    assert json.loads(blocks[0].signature) == reasoning_item
    assert isinstance(blocks[1], TextBlock)
    assert blocks[1].text == "你好"
    assert isinstance(blocks[2], ToolCallBlock)
    assert blocks[2].id == "call_1"
    assert blocks[2].name == "read_file"
    assert blocks[2].arguments == '{"path":"a"}'
    assert blocks[2].item_id == "fc_1"
    assert stop_reason == "tool_use"
    assert usage.input_tokens == 100
    assert usage.cache_read_tokens == 80


def test_parse_response_incomplete_and_refusal():
    adapter = OpenAIResponsesAdapter(make_config())
    blocks, stop_reason, _ = adapter.parse_response({
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "refusal", "refusal": "不能这样做"}],
        }],
    })
    assert stop_reason == "length"
    assert blocks[0].text == "[模型拒绝请求] 不能这样做"


def test_usage_from_responses():
    usage = UsageData.from_responses({
        "input_tokens": 10,
        "output_tokens": 5,
        "input_tokens_details": {"cached_tokens": 7, "cache_write_tokens": 2},
    })
    assert (usage.input_tokens, usage.output_tokens) == (10, 5)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (7, 2)


# ========== 流式翻译 ==========

def test_translate_stream_text_reasoning_and_tool_call():
    adapter = OpenAIResponsesAdapter(make_config())
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "思考"}],
        "encrypted_content": "enc",
    }
    events = [
        {"type": "response.created", "response": {"model": "gpt-5.1"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "reasoning", "id": "rs_1"}},
        {"type": "response.reasoning_summary_text.delta", "output_index": 0,
         "item_id": "rs_1", "delta": "思考"},
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning_item},
        {"type": "response.output_item.added", "output_index": 1,
         "item": {"type": "message", "id": "msg_1"}},
        {"type": "response.output_text.delta", "output_index": 1,
         "item_id": "msg_1", "delta": "你好"},
        {"type": "response.output_item.done", "output_index": 1,
         "item": {"type": "message", "id": "msg_1",
                  "content": [{"type": "output_text", "text": "你好"}]}},
        {"type": "response.output_item.added", "output_index": 2,
         "item": {"type": "function_call", "id": "fc_1",
                  "call_id": "call_1", "name": "read_file", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "output_index": 2,
         "item_id": "fc_1", "call_id": "call_1", "delta": '{"path":'},
        {"type": "response.function_call_arguments.delta", "output_index": 2,
         "item_id": "fc_1", "call_id": "call_1", "delta": '"a"}'},
        {"type": "response.output_item.done", "output_index": 2,
         "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                  "name": "read_file", "arguments": '{"path":"a"}'}},
        {"type": "response.completed", "response": {
            "status": "completed",
            "model": "gpt-5.1",
            "output": [reasoning_item,
                       {"type": "message", "id": "msg_1",
                        "content": [{"type": "output_text", "text": "你好"}]},
                       {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                        "name": "read_file", "arguments": '{"path":"a"}'}],
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "input_tokens_details": {"cached_tokens": 80}},
        }},
    ]

    chunks = list(adapter.translate_stream(events))
    assembler = BlockAssembler()
    for chunk in chunks:
        assembler.push(chunk)

    assert assembler.stop_reason == "tool_use"
    assert assembler.finished is True
    assert assembler.usage.input_tokens == 100
    assert assembler.usage.cache_read_tokens == 80

    blocks = assembler.blocks
    assert [type(b) for b in blocks] == [ReasoningBlock, TextBlock, ToolCallBlock]
    assert blocks[0].text == "思考"
    assert json.loads(blocks[0].signature)["encrypted_content"] == "enc"
    assert blocks[1].text == "你好"
    # 参数不因 output_item.done 重复追加
    assert blocks[2].id == "call_1"
    assert blocks[2].name == "read_file"
    assert blocks[2].arguments == '{"path":"a"}'
    assert blocks[2].item_id == "fc_1"


def test_translate_stream_completed_only_backfills_full_items():
    adapter = OpenAIResponsesAdapter(make_config())
    events = [{
        "type": "response.completed",
        "response": {
            "status": "completed",
            "model": "gpt-5.1",
            "output": [
                {"type": "message", "id": "msg_1",
                 "content": [{"type": "output_text", "text": "直接输出"}]},
                {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                 "name": "read_file", "arguments": '{"path":"a"}'},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
    }]

    assembler = BlockAssembler()
    for chunk in adapter.translate_stream(events):
        assembler.push(chunk)

    assert [type(b) for b in assembler.blocks] == [TextBlock, ToolCallBlock]
    assert assembler.blocks[0].text == "直接输出"
    assert assembler.blocks[1].arguments == '{"path":"a"}'
    assert assembler.stop_reason == "tool_use"


def test_translate_stream_failed_raises():
    adapter = OpenAIResponsesAdapter(make_config())
    events = [{
        "type": "response.failed",
        "response": {"error": {"code": "rate_limit_exceeded", "message": "slow down"}},
    }]
    with pytest.raises(RuntimeError, match="rate_limit_exceeded"):
        list(adapter.translate_stream(events))

    with pytest.raises(RuntimeError, match="OpenAI Responses 错误"):
        list(adapter.translate_stream([{"type": "error", "message": "boom"}]))
