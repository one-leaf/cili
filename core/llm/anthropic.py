"""Anthropic Messages API adapter.

Endpoint: /v1/messages
Headers: x-api-key, anthropic-version
SSE events: message_start, content_block_start/delta/stop, message_delta
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable
from urllib.parse import urlparse

from core.config import ModelConfig
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

    def build_headers(self) -> dict[str, str]:
        """Build headers for Anthropic API."""
        return {
            "x-api-key": self.config.api_key,
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
        temperature: float | None = None,
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

        self._apply_thinking_config(body, temperature)
        # LiteLLM proxy support
        self._apply_litellm_extras(body, session_id)

        if tools:
            body["tools"] = tools

        # prompt cache 需在 tools 放入 body 后调用（它要给 tools[-1] 加 cache_control）
        # system 由 _apply_prompt_cache 内部处理（支持 str / list[str]）
        self._apply_prompt_cache(body, system, anthropic_messages)

        if stream:
            body["stream"] = True

        return body

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """将内部 Message 转为 Anthropic 格式（tool_call→tool_use、tool_call_id→tool_use_id）。"""
        anthropic_messages = []
        for msg in messages:
            if isinstance(msg.content, str):
                anthropic_messages.append({
                    "role": msg.role,
                    "content": msg.content,
                })
            else:
                # Convert content blocks to Anthropic format
                content = []
                for block in msg.content:
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
                anthropic_messages.append({
                    "role": msg.role,
                    "content": content,
                })
        return anthropic_messages

    def _apply_thinking_config(self, body: dict[str, Any], temperature: float | None) -> None:
        """Extended thinking：配置 reasoning_effort 时开启 thinking（与 temperature 互斥）。"""
        effort = self.config.reasoning_effort
        if effort:
            # budget_tokens 按档位映射（经验值）：low/medium/high 分别为
            # 1024/4096/10000，未命中档位时回退 4096（medium）
            budget_map = {
                "low": 1024,
                "medium": 4096,
                "high": 10000,
            }
            budget_tokens = budget_map.get(effort, 4096)
            body["thinking"] = {"type": "enabled", "budget_tokens": budget_tokens}
        else:
            # No thinking, use temperature if specified
            if temperature is not None:
                body["temperature"] = temperature

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
        current_block_type = ""

        for event in events:
            evt_type = event.get("type", "")

            if evt_type == "message_start":
                msg = event.get("message", {})
                u = msg.get("usage", {})
                if u:
                    usage = UsageData.from_anthropic(u)
                    yield StreamChunk.usage_chunk(usage)

            elif evt_type == "content_block_start":
                current_block_index += 1
                block = event.get("content_block", {})
                block_type = block.get("type", "")
                current_block_type = block_type

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

                elif delta_type == "input_json_delta":
                    args = delta.get("partial_json", "")
                    if args:
                        yield StreamChunk.tool_call_delta(
                            current_block_index,
                            arguments=args,
                        )

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
