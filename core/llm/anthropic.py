"""Anthropic Messages API adapter.

Endpoint: /v1/messages
Headers: x-api-key, anthropic-version
SSE events: message_start, content_block_start/delta/stop, message_delta
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from core.llm.adapter import Adapter, merge_consecutive_same_role
from core.llm.types import (
    ContentBlock,
    Message,
    ReasoningBlock,
    StreamChunk,
    TextBlock,
    ToolCallBlock,
    UsageData,
    block_to_dict,
)

logger = logging.getLogger(__name__)


class AnthropicAdapter(Adapter):
    """Adapter for Anthropic Messages API."""

    # 可标记 cache_control 的块类型（thinking 块的缓存行为未确认，跳过）
    _CACHEABLE_BLOCK_TYPES = ("text", "tool_use", "tool_result", "image")

    @property
    def api_path(self) -> str:
        return "/v1/messages"

    @property
    def _prompt_cache_enabled(self) -> bool:
        """根据 cache_control 配置决定是否启用 prompt cache。

        cache_control 值：
        - "auto"：使用 Anthropic 协议即启用（默认）
        - "true"：强制启用
        - "false"：强制禁用
        """
        setting = self.config.cache_control
        if setting == "false":
            return False
        if setting == "true":
            return True
        # auto：使用 Anthropic 协议即启用
        return True

    def default_auth_headers(self) -> dict[str, str]:
        """auth_type="auto" 的默认认证头。"""
        return {"x-api-key": self.config.api_key}

    def protocol_headers(self) -> dict[str, str]:
        return {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def serialize(
        self,
        messages: list[Message],
        system: str | list[str],
        tools: list[dict[str, Any]] | None,
        model: str,
        max_tokens: int,
        stream: bool = False,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Serialize to Anthropic Messages API format.

        Converts internal tool_call format to Anthropic's tool_use format.
        Merges consecutive same-role messages for Bedrock compatibility.

        system 支持 list[str]（含 DYNAMIC_BOUNDARY，用于缓存分块）。
        """
        # Merge consecutive same-role messages (Bedrock rejects them)
        messages = merge_consecutive_same_role(messages)
        anthropic_messages = self._convert_messages(messages)

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": anthropic_messages,
        }

        self._apply_thinking_config(body)

        if tools:
            body["tools"] = tools

        # prompt cache 需在 tools 放入 body 后调用（它要给 tools[-1] 加 cache_control）
        # system 由 _apply_prompt_cache 内部处理（支持 str / list[str]）
        self._apply_prompt_cache(body, system, anthropic_messages)

        if stream:
            body["stream"] = True

        return body

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """将内部 Message 转为 Anthropic 格式（tool_call→tool_use、tool_call_id→tool_use_id）。

        发送前归一化思考块，避免思考模式下的多轮/跨模型 400：
        - thinking 关闭时丢弃历史思考块（禁用思考的请求不接受 thinking 块）；
        - 对不校验签名的上游（DeepSeek 等 Anthropic 兼容端点），给带 tool_use
          但缺思考块（或思考文本为空）的轮次补非空占位 thinking 块——空文本
          同样被判定为未回传（实测 400 "must be passed back"）。
        """
        drop_thinking = self.config.reasoning == "off"
        fill_missing = not drop_thinking and self._accepts_unsigned_thinking()
        anthropic_messages = []
        for msg in messages:
            if isinstance(msg.content, str):
                anthropic_messages.append({
                    "role": msg.role,
                    "content": msg.content,
                })
                continue

            # Convert content blocks to Anthropic format
            content = []
            for block in msg.content:
                if drop_thinking and isinstance(block, ReasoningBlock):
                    continue
                block_dict = block_to_dict(block)
                # Convert tool_call to tool_use (Anthropic's format)
                if block_dict.get("type") == "tool_call":
                    block_dict["type"] = "tool_use"
                    # Parse arguments string to input dict
                    arguments = block_dict.get("arguments", "")
                    try:
                        block_dict["input"] = json.loads(arguments) if arguments else {}
                    except json.JSONDecodeError:
                        block_dict["input"] = {"_raw": arguments}
                    block_dict.pop("arguments", None)
                # Convert tool_call_id to tool_use_id (OpenAI → Anthropic)
                elif block_dict.get("type") == "tool_result":
                    if "tool_call_id" in block_dict:
                        block_dict["tool_use_id"] = block_dict.pop("tool_call_id")
                content.append(block_dict)

            if fill_missing:
                self._fill_unsigned_thinking(msg, content)

            if not content:
                continue
            anthropic_messages.append({
                "role": msg.role,
                "content": content,
            })
        return anthropic_messages

    # 校验 thinking 签名的官方端点：这些地址不能补空签名块
    _STRICT_SIGNATURE_MARKERS = ("anthropic.com", "amazonaws.com", "googleapis.com")

    def _accepts_unsigned_thinking(self) -> bool:
        """上游是否接受无签名 thinking 块。

        DeepSeek 官方及同类第三方网关（litellm / new-api / one-api 等）不校验
        thinking 签名，只要求带 tool_use 的 assistant 轮存在 thinking 块。
        官方 Anthropic / Bedrock / Vertex 会校验签名，不能补空块。
        只看 endpoint 地址，不依赖模型别名，网关改映射不影响判断。
        """
        base_url = self.base_url.lower()
        return not any(marker in base_url for marker in self._STRICT_SIGNATURE_MARKERS)

    # 补位思考文本：DeepSeek 等端点把空 thinking 文本视为「未回传」（实测 400），
    # 占位内容必须非空。
    _SYNTHETIC_THINKING_TEXT = "(reasoning omitted)"

    @classmethod
    def _fill_unsigned_thinking(cls, msg: Message, content: list[dict[str, Any]]) -> None:
        """给带 tool_use 的 assistant 轮补齐非空思考块。

        跨模型切换后历史轮次可能没有思考块，或只有空文本；DeepSeek 等不校验
        签名的上游两种都拒绝（"must be passed back"），发送前统一补占位文本。
        """
        if msg.role != "assistant":
            return
        if not any(b.get("type") == "tool_use" for b in content):
            return
        thinking = [b for b in content if b.get("type") == "thinking"]
        if not thinking:
            # redacted_thinking 原样回传，不掺入占位块
            if not any(b.get("type") == "redacted_thinking" for b in content):
                content.insert(0, {
                    "type": "thinking",
                    "thinking": cls._SYNTHETIC_THINKING_TEXT,
                    "signature": "",
                })
            return
        for block in thinking:
            if not block.get("thinking"):
                block["thinking"] = cls._SYNTHETIC_THINKING_TEXT
                block.setdefault("signature", "")

    def _apply_thinking_config(self, body: dict[str, Any]) -> None:
        """Extended thinking：按 reasoning 档位设置思考（anthropic 取值域，§models-provider-design）。

        - ``default``：不发送 thinking 参数，交给服务端默认
        - ``off``：显式 ``{"type": "disabled"}``。必须显式发——部分网关（litellm 等）
          对「不传」的默认是**开**思考，只「不发」等于没关（实测：关掉后单次判定
          从 ~27s 降到 ~6s）
        - ``low``/``medium``/``high``：``thinking.budget_tokens`` 1024/4096/10000
        """
        effort = self.config.reasoning
        if effort == "off":
            body["thinking"] = {"type": "disabled"}
            return
        if effort in ("low", "medium", "high"):
            budget_map = {
                "low": 1024,
                "medium": 4096,
                "high": 10000,
            }
            budget_tokens = budget_map[effort]
            body["thinking"] = {"type": "enabled", "budget_tokens": budget_tokens}
        # default / 其他值：不发送 thinking 参数，也不发送 temperature（已移除）

    def _apply_prompt_cache(
        self,
        body: dict[str, Any],
        system: str | list[str],
        anthropic_messages: list[dict[str, Any]],
    ) -> None:
        """Prompt cache：4 断点布局——system 静态区 + tools + messages[-3] + messages[-1]。

        system 支持 list[str]（含 DYNAMIC_BOUNDARY）或 str（向后兼容）。
        静态区用 scope='global' 跨用户共享，动态区不缓存。
        非官方端点（中转/网关）不识别 cache_control 会直接 400，由
        _prompt_cache_enabled 统一关闭。
        """
        if not self._prompt_cache_enabled:
            # 非官方端点：system 直接 join 为字符串（或透传 str）
            if isinstance(system, list):
                body["system"] = "\n\n".join(s for s in system
                                              if s != "__CILI_DYNAMIC_BOUNDARY__")
            elif system:
                body["system"] = system
            return

        from core.prompt_builder import DYNAMIC_BOUNDARY

        # ── system 分块：按 DYNAMIC_BOUNDARY 分割静态/动态区 ──────────────
        static_parts: list[str] = []
        dynamic_parts: list[str] = []
        past_boundary = False

        blocks = system if isinstance(system, list) else ([system] if system else [])
        for block in blocks:
            if block == DYNAMIC_BOUNDARY:
                past_boundary = True
                continue
            if not block:
                continue
            if past_boundary:
                dynamic_parts.append(block)
            else:
                static_parts.append(block)

        system_list: list[dict[str, Any]] = []
        if static_parts:
            system_list.append({
                "type": "text",
                "text": "\n\n".join(static_parts),
                "cache_control": {"type": "ephemeral", "scope": "global"},
            })
        if dynamic_parts:
            # 动态区不标 cache_control（session 特定，不 global）
            system_list.append({
                "type": "text",
                "text": "\n\n".join(dynamic_parts),
            })
        if system_list:
            body["system"] = system_list

        # ── tools 断点：最后一个 tool schema 加 cache_control ──────────────
        tools = body.get("tools")
        if tools and isinstance(tools, list):
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}

        # ── 消息断点：messages[-3]（稳定区尾部）+ messages[-1]（最新尾部）──
        n = len(anthropic_messages)
        # messages[-3] 断点：tool-use loop 中，这条之前的内容在多轮内稳定
        if n >= 3:
            third_last_content = anthropic_messages[-3].get("content")
            if isinstance(third_last_content, list) and third_last_content:
                last_block = third_last_content[-1]
                if last_block.get("type") in self._CACHEABLE_BLOCK_TYPES:
                    last_block["cache_control"] = {"type": "ephemeral"}
        # messages[-1] 断点：捕获最新上下文
        if n >= 1:
            last_content = anthropic_messages[-1].get("content")
            if isinstance(last_content, list) and last_content:
                last_block = last_content[-1]
                if last_block.get("type") in self._CACHEABLE_BLOCK_TYPES:
                    last_block["cache_control"] = {"type": "ephemeral"}

    def parse_response(self, data: dict[str, Any]) -> tuple[list[ContentBlock], str, UsageData]:
        """Parse Anthropic API response."""
        content_blocks: list[ContentBlock] = []

        for block in data.get("content", []):
            block_type = block.get("type", "")
            if block_type == "text":
                content_blocks.append(TextBlock(text=block.get("text", "")))
            elif block_type == "thinking":
                content_blocks.append(ReasoningBlock(
                    text=block.get("thinking", ""),
                    signature=block.get("signature"),
                ))
            elif block_type == "redacted_thinking":
                content_blocks.append(ReasoningBlock(redacted_data=block.get("data", "")))
            elif block_type == "tool_use":
                # Store arguments as raw JSON string
                input_data = block.get("input", {})
                arguments = json.dumps(input_data, ensure_ascii=False) if input_data else ""
                content_blocks.append(ToolCallBlock(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=arguments,
                ))

        usage = UsageData.from_anthropic(data.get("usage", {}))
        stop_reason = data.get("stop_reason", "")

        return content_blocks, stop_reason, usage

    def translate_stream(self, events: Iterable[dict[str, Any]]) -> Iterable[StreamChunk]:
        """Translate Anthropic SSE events to StreamChunks."""
        current_block_index = -1

        for event in events:
            evt_type = event.get("type", "")

            if evt_type == "message_start":
                msg = event.get("message", {})
                u = msg.get("usage", {})
                if u:
                    usage = UsageData.from_anthropic(u)
                    chunk_data = {"usage": usage}
                    if msg.get("model"):
                        chunk_data["model"] = str(msg["model"])
                    yield StreamChunk(type="usage", data=chunk_data)

            elif evt_type == "content_block_start":
                current_block_index += 1
                block = event.get("content_block", {})
                block_type = block.get("type", "")

                if block_type == "text":
                    yield StreamChunk.block_start(current_block_index, "text")
                elif block_type == "tool_use":
                    yield StreamChunk.block_start(current_block_index, "tool_call")
                    # Emit id and name as deltas
                    yield StreamChunk.tool_call_delta(
                        current_block_index,
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                    )
                elif block_type == "thinking":
                    yield StreamChunk.block_start(current_block_index, "reasoning")
                    signature = block.get("signature", "")
                    if signature:
                        yield StreamChunk.signature_delta(current_block_index, signature)
                elif block_type == "redacted_thinking":
                    yield StreamChunk.block_start(current_block_index, "reasoning")
                    yield StreamChunk.redacted_block(current_block_index, block.get("data", ""))

            elif evt_type == "content_block_delta":
                delta = event.get("delta", {})
                delta_type = delta.get("type", "")

                if delta_type == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield StreamChunk.text_delta(current_block_index, text)

                elif delta_type == "thinking_delta":
                    text = delta.get("thinking", "")
                    if text:
                        yield StreamChunk.reasoning_delta(current_block_index, text)

                elif delta_type == "signature_delta":
                    signature = delta.get("signature", "")
                    if signature:
                        yield StreamChunk.signature_delta(current_block_index, signature)

                elif delta_type == "input_json_delta":
                    args = delta.get("partial_json", "")
                    if args:
                        yield StreamChunk.tool_call_delta(
                            current_block_index,
                            arguments=args,
                        )

                # LitellM 新版流式把签名直接挂在 delta.signature 上
                # （不再使用 signature_delta 事件类型），两种形态都要收。
                if delta_type != "signature_delta":
                    signature = delta.get("signature", "")
                    if signature:
                        yield StreamChunk.signature_delta(current_block_index, signature)

            elif evt_type == "content_block_stop":
                yield StreamChunk.block_end(current_block_index)

            elif evt_type == "message_delta":
                delta = event.get("delta", {})
                stop_reason = delta.get("stop_reason", "")
                if stop_reason:
                    yield StreamChunk.finish_chunk(stop_reason)

                u = event.get("usage", {})
                if u:
                    usage = UsageData.from_anthropic(u)
                    yield StreamChunk.usage_chunk(usage)
