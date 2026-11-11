"""thinking/signature 跨轮重放回归测试。

覆盖线上 400 错误：
    The content[].thinking in the thinking mode must be passed back to the API.

根因有两类：
1. LitellM 的 OpenAI 兼容端点要求 assistant 消息回传 thinking_blocks（含
   signature），此前只回传 reasoning_content；
2. Anthropic 流式路径只认 signature_delta 事件，未收 LitellM 新版直接挂在
   delta.signature 上的签名。
"""

from core.config import ModelConfig
from core.llm import AnthropicAdapter, Message, OpenAIAdapter, TextBlock, ToolCallBlock
from core.llm.adapter import merge_consecutive_same_role
from core.llm.assembler import BlockAssembler
from core.llm.types import ReasoningBlock


def _anthropic_adapter(reasoning: str = "default") -> AnthropicAdapter:
    config = ModelConfig(
        name="claude-sonnet-4-6",
        api_key="k",
        base_url="https://api.anthropic.com",
        interface_type="anthropic",
        reasoning=reasoning,
    )
    return AnthropicAdapter(config)


def _openai_adapter() -> OpenAIAdapter:
    config = ModelConfig(
        name="claude-sonnet-4-6",
        api_key="k",
        base_url="https://gateway.example.com",
        interface_type="openai",
    )
    return OpenAIAdapter(config)


def _assemble(adapter, events) -> BlockAssembler:
    assembler = BlockAssembler()
    for chunk in adapter.translate_stream(events):
        assembler.push(chunk)
    return assembler


class TestAnthropicThinkingReplay:
    def test_signature_as_plain_delta_field(self):
        """LitellM 新版把 signature 直接挂在 delta 上，而非 signature_delta 事件。"""
        events = [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "想", "signature": "sig-x"}},
            {"type": "content_block_stop", "index": 0},
        ]
        block = _assemble(_anthropic_adapter(), events).blocks[0]
        assert block.text == "想"
        assert block.signature == "sig-x"

    def test_redacted_thinking_roundtrip(self):
        """redacted_thinking 必须存入并在下一轮原样回传。"""
        events = [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "redacted_thinking", "data": "enc-1"}},
            {"type": "content_block_stop", "index": 0},
        ]
        block = _assemble(_anthropic_adapter(), events).blocks[0]
        assert isinstance(block, ReasoningBlock)
        assert block.redacted_data == "enc-1"

        adapter = _anthropic_adapter()
        body = adapter.serialize(
            [Message(role="assistant", content=[block])],
            system="", tools=None, model="claude-sonnet-4-6", max_tokens=100,
        )
        assert body["messages"][0]["content"][0] == {
            "type": "redacted_thinking", "data": "enc-1",
        }

    def test_parse_response_keeps_signature(self):
        adapter = _anthropic_adapter()
        blocks, _, _ = adapter.parse_response({
            "content": [
                {"type": "thinking", "thinking": "想", "signature": "sig-1"},
                {"type": "text", "text": "答"},
            ],
            "stop_reason": "end_turn",
        })
        assert blocks[0].signature == "sig-1"


    def _deepseek_adapter(self, reasoning: str = "default") -> AnthropicAdapter:
        config = ModelConfig(
            name="deepseek-v4-pro",
            api_key="k",
            base_url="https://api.deepseek.com/anthropic",
            interface_type="anthropic",
            reasoning=reasoning,
            cache_control="false",
        )
        return AnthropicAdapter(config)

    def test_fills_synthetic_thinking_for_deepseek_tool_turn(self):
        """DeepSeek 不校验签名：缺思考块的 tool_use 轮补空 thinking 块。"""
        adapter = self._deepseek_adapter()
        body = adapter.serialize(
            [Message(role="assistant", content=[
                ToolCallBlock(id="t1", name="read", arguments="{}"),
            ])],
            system="", tools=None, model="deepseek-v4-pro", max_tokens=100,
        )
        content = body["messages"][0]["content"]
        assert content[0] == {
            "type": "thinking",
            "thinking": AnthropicAdapter._SYNTHETIC_THINKING_TEXT,
            "signature": "",
        }
        assert content[1]["type"] == "tool_use"

    def test_fills_for_third_party_gateway_alias(self):
        """第三方网关用不透明别名映射 DeepSeek 时也必须补块。"""
        config = ModelConfig(
            name="ac/dgx",
            api_key="k",
            base_url="http://gateway.example.com/llm",
            interface_type="anthropic",
            cache_control="false",
        )
        adapter = AnthropicAdapter(config)
        body = adapter.serialize(
            [Message(role="assistant", content=[
                ToolCallBlock(id="t1", name="read", arguments="{}"),
            ])],
            system="", tools=None, model="ac/dgx", max_tokens=100,
        )
        content = body["messages"][0]["content"]
        assert content[0] == {
            "type": "thinking",
            "thinking": AnthropicAdapter._SYNTHETIC_THINKING_TEXT,
            "signature": "",
        }

    def test_does_not_fill_for_real_anthropic(self):
        """真 Anthropic 校验签名，补空块会报 invalid signature，必须保持原样。"""
        adapter = _anthropic_adapter()
        body = adapter.serialize(
            [Message(role="assistant", content=[
                ToolCallBlock(id="t1", name="read", arguments="{}"),
            ])],
            system="", tools=None, model="claude-sonnet-4-6", max_tokens=100,
        )
        content = body["messages"][0]["content"]
        assert [b["type"] for b in content] == ["tool_use"]

    def test_keeps_existing_thinking_block(self):
        adapter = self._deepseek_adapter()
        body = adapter.serialize(
            [Message(role="assistant", content=[
                ReasoningBlock(text="想", signature="sig-1"),
                ToolCallBlock(id="t1", name="read", arguments="{}"),
            ])],
            system="", tools=None, model="deepseek-v4-pro", max_tokens=100,
        )
        content = body["messages"][0]["content"]
        assert [b["type"] for b in content] == ["thinking", "tool_use"]
        assert content[0]["thinking"] == "想"

    def test_rewrites_empty_thinking_text(self):
        """空文本 thinking 等同未回传（实测 400），发送前补成非空占位。"""
        adapter = self._deepseek_adapter()
        body = adapter.serialize(
            [Message(role="assistant", content=[
                ReasoningBlock(text=""),
                ToolCallBlock(id="t1", name="read", arguments="{}"),
            ])],
            system="", tools=None, model="deepseek-v4-pro", max_tokens=100,
        )
        content = body["messages"][0]["content"]
        assert [b["type"] for b in content] == ["thinking", "tool_use"]
        assert content[0]["thinking"] == AnthropicAdapter._SYNTHETIC_THINKING_TEXT
        assert content[0]["signature"] == ""

    def test_strips_history_thinking_when_off(self):
        adapter = self._deepseek_adapter("off")
        body = adapter.serialize(
            [Message(role="assistant", content=[
                ReasoningBlock(text="想", signature="sig-1"),
                TextBlock(text="答"),
            ])],
            system="", tools=None, model="deepseek-v4-pro", max_tokens=100,
        )
        content = body["messages"][0]["content"]
        assert [b["type"] for b in content] == ["text"]
        assert body["thinking"] == {"type": "disabled"}


class TestOpenAIThinkingReplay:
    def test_replays_thinking_blocks_with_signature(self):
        """assistant 消息必须回传 thinking_blocks，而不是只有 reasoning_content。"""
        messages = [Message(role="assistant", content=[
            ReasoningBlock(text="想法", signature="sig-1"),
            TextBlock(text="答案"),
            ToolCallBlock(id="t1", name="read", arguments="{}"),
        ])]
        msg = _openai_adapter()._convert_messages(messages, system="")[0]
        assert msg["reasoning_content"] == "想法"
        assert msg["thinking_blocks"] == [
            {"type": "thinking", "thinking": "想法", "signature": "sig-1"}
        ]
        assert msg["tool_calls"][0]["id"] == "t1"

    def test_replays_redacted_thinking(self):
        messages = [Message(role="assistant", content=[
            ReasoningBlock(redacted_data="enc-1"),
        ])]
        msg = _openai_adapter()._convert_messages(messages, system="")[0]
        assert msg["thinking_blocks"] == [
            {"type": "redacted_thinking", "data": "enc-1"}
        ]

    def test_unsigned_reasoning_not_sent_as_thinking_blocks(self):
        """普通 reasoning_content 模型（无 signature）不发 thinking_blocks，
        避免把该字段发给不认识它的非 Anthropic 兼容端点。"""
        messages = [Message(role="assistant", content=[ReasoningBlock(text="思考")])]
        msg = _openai_adapter()._convert_messages(messages, system="")[0]
        assert msg["reasoning_content"] == "思考"
        assert "thinking_blocks" not in msg

    def test_parse_response_prefers_thinking_blocks(self):
        blocks, _, _ = _openai_adapter().parse_response({
            "choices": [{
                "message": {
                    "content": "ok",
                    "reasoning_content": "想法",
                    "thinking_blocks": [
                        {"type": "thinking", "thinking": "想法", "signature": "sig-1"}
                    ],
                },
                "finish_reason": "stop",
            }],
            "usage": {},
        })
        reasoning = [b for b in blocks if isinstance(b, ReasoningBlock)]
        assert len(reasoning) == 1
        assert reasoning[0].text == "想法"
        assert reasoning[0].signature == "sig-1"

    def test_stream_reconciles_reasoning_content_and_thinking_blocks(self):
        """先流式 reasoning_content、后下发完整 thinking_blocks：不得重复文本。"""
        events = [
            {"choices": [{"delta": {"reasoning_content": "思考"}}]},
            {"choices": [{"delta": {"thinking_blocks": [
                {"type": "thinking", "thinking": "思考过程", "signature": "sig-1"}
            ]}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        assembler = _assemble(_openai_adapter(), events)
        reasoning = [b for b in assembler.blocks if isinstance(b, ReasoningBlock)]
        assert len(reasoning) == 1
        assert reasoning[0].text == "思考过程"
        assert reasoning[0].signature == "sig-1"

    def test_stream_accepts_provider_specific_fields(self):
        events = [
            {"choices": [{"delta": {"provider_specific_fields": {
                "thinking_blocks": [
                    {"type": "thinking", "thinking": "想", "signature": "sig-2"}
                ],
            }}}]},
        ]
        block = _assemble(_openai_adapter(), events).blocks[0]
        assert block.text == "想"
        assert block.signature == "sig-2"


class TestMergeGuard:
    def test_does_not_merge_assistant_messages_with_thinking(self):
        """含思考块的 assistant 消息不得合并（会破坏 thinking 顺序/签名绑定）。"""
        messages = [
            Message(role="assistant", content=[ReasoningBlock(text="a"), TextBlock(text="x")]),
            Message(role="assistant", content=[ReasoningBlock(text="b"), TextBlock(text="y")]),
        ]
        assert len(merge_consecutive_same_role(messages)) == 2
