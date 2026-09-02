"""Anthropic Messages API adapter.

Endpoint: /v1/messages
Headers: x-api-key, anthropic-version
SSE events: message_start, content_block_start/delta/stop, message_delta
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from core.config import ModelConfig
from core.llm.adapter import Adapter
from core.llm.types import (
    ContentBlock,
    Message,
    ReasoningBlock,
    StreamChunk,
    TextBlock,
    ToolCallBlock,
    UsageData,
    block_to_dict,
    blocks_to_dicts,
)

logger = logging.getLogger(__name__)


class AnthropicAdapter(Adapter):
    """Adapter for Anthropic Messages API."""

    @property
    def api_path(self) -> str:
        return "/v1/messages"

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
        system: str,
        tools: list[dict[str, Any]] | None,
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        stream: bool = False,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Serialize to Anthropic Messages API format.

        Converts internal tool_call format to Anthropic's tool_use format.
        """
        # Convert messages to Anthropic format
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
                    # Convert reasoning → thinking (Anthropic's format)
                    if block_dict.get("type") == "reasoning":
                        block_dict = {
                            "type": "thinking",
                            "thinking": block_dict.get("text", ""),
                        }
                        if block_dict.get("signature"):
                            block_dict["signature"] = block_dict.pop("signature")
                    # Convert tool_call to tool_use (Anthropic's format)
                    elif block_dict.get("type") == "tool_call":
                        block_dict["type"] = "tool_use"
                        # Parse arguments string to input dict
                        arguments = block_dict.get("arguments", "")
                        try:
                            block_dict["input"] = json.loads(arguments) if arguments else {}
                        except json.JSONDecodeError:
                            block_dict["input"] = {"_raw": arguments}
                        block_dict.pop("arguments", None)
                    # Convert tool_call_id to tool_use_id
                    elif block_dict.get("type") == "tool_result":
                        block_dict["tool_use_id"] = block_dict.pop("tool_call_id", "")
                    content.append(block_dict)
                anthropic_messages.append({
                    "role": msg.role,
                    "content": content,
                })

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": anthropic_messages,
        }

        # Enable extended thinking for non-streaming requests
        if not stream:
            body["thinking"] = {"type": "enabled", "budget_tokens": 4096}
        else:
            # Streaming requires temperature when thinking is disabled
            if temperature is not None:
                body["temperature"] = temperature

        # LiteLLM proxy support
        if self._is_litellm_proxy and session_id:
            body["litellm_session_id"] = session_id
        elif self._is_litellm_proxy and not session_id:
            logger.debug(f"[Anthropic] LiteLLM proxy detected but no session_id provided")

        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True

        return body

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
