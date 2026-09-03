"""OpenAI Chat Completions API adapter.

Endpoint: /v1/chat/completions
Headers: Authorization: Bearer
SSE events: choices[0].delta with finish_reason
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
)

logger = logging.getLogger(__name__)


class OpenAIAdapter(Adapter):
    """Adapter for OpenAI Chat Completions API."""

    @property
    def api_path(self) -> str:
        return "/v1/chat/completions"

    def build_headers(self) -> dict[str, str]:
        """Build headers for OpenAI API."""
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "content-type": "application/json",
        }

    def _convert_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """Convert tool schema to OpenAI native format.

        Anthropic: {"name", "description", "input_schema": {...}}
        OpenAI:    {"type": "function", "function": {"name", "description", "parameters"}}
        """
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", t.get("parameters", {})),
                },
            }
            for t in tools
        ]

    def _convert_messages(self, messages: list[Message], system: str) -> list[dict[str, Any]]:
        """Convert internal messages to OpenAI native format.

        Conversion rules:
        - assistant tool_call blocks → tool_calls field
        - tool role messages → role=tool messages
        - other messages pass through
        """
        openai_messages: list[dict[str, Any]] = []

        if system:
            openai_messages.append({"role": "system", "content": system})

        for msg in messages:
            role = msg.role
            content = msg.content

            # String content passes through directly
            if isinstance(content, str):
                openai_messages.append({"role": role, "content": content})
                continue

            # Handle None content (e.g., assistant messages with only tool_calls)
            if content is None:
                openai_messages.append({"role": role, "content": None})
                continue

            # Content is a list of blocks
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []
            images: list[dict[str, Any]] = []  # image_url blocks for user messages

            for block in content:
                block_dict = block_to_dict(block)
                btype = block_dict.get("type", "")

                if btype == "text":
                    text_parts.append(block_dict.get("text", ""))

                # Support both Anthropic format ("thinking") and legacy format ("reasoning")
                elif btype in ("reasoning", "thinking"):
                    # Anthropic: {"type": "thinking", "thinking": "..."}
                    # Legacy: {"type": "reasoning", "text": "..."}
                    text = block_dict.get("thinking", "") or block_dict.get("text", "")
                    reasoning_parts.append(text)

                elif btype == "image":
                    # Convert ImageBlock to OpenAI image_url format
                    source = block_dict.get("source", {})
                    if source.get("type") == "base64":
                        images.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
                            },
                        })

                # Support both Anthropic format ("tool_use") and legacy format ("tool_call")
                elif btype in ("tool_call", "tool_use"):
                    # Anthropic: {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
                    # Legacy: {"type": "tool_call", "id": "...", "name": "...", "arguments": "..."}
                    input_data = block_dict.get("input")
                    if input_data is not None:
                        if isinstance(input_data, dict):
                            arguments = json.dumps(input_data, ensure_ascii=False)
                        else:
                            arguments = str(input_data) if input_data else ""
                    else:
                        arguments = block_dict.get("arguments", "")
                        if not isinstance(arguments, str):
                            arguments = json.dumps(arguments, ensure_ascii=False)

                    tool_calls.append({
                        "id": block_dict.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block_dict.get("name", ""),
                            "arguments": arguments,
                        },
                    })

                elif btype == "tool_result":
                    tool_results.append(block_dict)

            # Assistant message: text + reasoning_content + tool_calls
            if tool_calls or (role == "assistant" and (text_parts or tool_calls)):
                msg_dict: dict[str, Any] = {"role": "assistant"}
                msg_dict["content"] = "\n".join(text_parts)
                # CoT passback: reasoning blocks concatenated back to the wire.
                # Reference: deepseek-harness serializeAssistant() — required on
                # tool-call turns, ignored elsewhere but keeps prefix stable.
                reasoning = "".join(reasoning_parts)
                if reasoning:
                    msg_dict["reasoning_content"] = reasoning
                if tool_calls:
                    msg_dict["tool_calls"] = tool_calls
                openai_messages.append(msg_dict)

            # Tool results: each becomes a separate role=tool message
            # Images from tool results are collected and added to a user message
            # (OpenAI API doesn't support images in role=tool messages)
            images_from_tools: list[dict[str, Any]] = []

            for tr in tool_results:
                tc = tr.get("content", "")
                if isinstance(tc, list):
                    # Extract text and images separately
                    text_parts_tool = []
                    for item in tc:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                text_parts_tool.append(item.get("text", ""))
                            elif item.get("type") == "image":
                                # Collect image for user message
                                source = item.get("source", {})
                                if source.get("type") == "base64":
                                    images_from_tools.append({
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
                                        }
                                    })
                            else:
                                text_parts_tool.append(str(item))
                        else:
                            text_parts_tool.append(str(item))
                    tc = "\n".join(text_parts_tool)
                elif not isinstance(tc, str):
                    tc = str(tc) if tc else ""

                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": tr.get("tool_call_id", tr.get("tool_use_id", "")),
                    "content": tc,
                })

            # If there are images from tool results, add a user message with them
            if images_from_tools:
                user_content: list[dict[str, Any]] = []
                # Include any text from the original message
                if text_parts:
                    user_content.append({"type": "text", "text": "\n".join(text_parts)})
                user_content.extend(images_from_tools)

                openai_messages.append({
                    "role": "user",
                    "content": user_content,
                })

            # User message with images (from paste) — multimodal content
            elif images and role == "user":
                user_content = []
                if text_parts:
                    user_content.append({"type": "text", "text": "\n".join(text_parts)})
                user_content.extend(images)
                openai_messages.append({"role": "user", "content": user_content})

            # User message with only text (no tool calls or results)
            elif not tool_calls and not tool_results and text_parts and role != "assistant":
                openai_messages.append({"role": role, "content": "\n".join(text_parts)})

        return openai_messages

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
        """Serialize to OpenAI Chat Completions API format."""
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": self._convert_messages(messages, system),
        }

        # Reasoning effort for reasoning models
        # Priority: config > auto-detect > none
        reasoning_effort = self.config.reasoning_effort
        if not reasoning_effort:
            model_lower = model.lower()
            is_reasoning_model = any(
                model_lower.startswith(prefix)
                for prefix in ("o1", "o3", "o4", "o5", "qwen3", "qwq", "deepseek-r1")
            )
            if is_reasoning_model:
                reasoning_effort = "medium"
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        else:
            if temperature is not None:
                body["temperature"] = temperature

        # LiteLLM proxy support
        if self._is_litellm_proxy and session_id:
            body["litellm_session_id"] = session_id
        elif self._is_litellm_proxy and not session_id:
            logger.debug(f"[OpenAI] LiteLLM proxy detected but no session_id provided")

        if tools:
            body["tools"] = self._convert_tools(tools)
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}

        return body

    def parse_response(self, data: dict[str, Any]) -> tuple[list[ContentBlock], str, UsageData]:
        """Parse OpenAI API response."""
        content_blocks: list[ContentBlock] = []
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})

        # Reasoning content (for o1, o3 models; or DGX which uses 'reasoning')
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if reasoning:
            content_blocks.append(ReasoningBlock(text=reasoning))

        # Text content
        if message.get("content"):
            content_blocks.append(TextBlock(text=message["content"]))

        # Tool calls - keep arguments as raw JSON string
        for tc in message.get("tool_calls", []):
            func = tc.get("function", {})
            content_blocks.append(ToolCallBlock(
                id=tc.get("id", ""),
                name=func.get("name", ""),
                arguments=func.get("arguments", ""),
            ))

        # Map finish_reason
        finish_reason = choice.get("finish_reason", "")
        if finish_reason == "stop":
            stop_reason = "end_turn"
        elif finish_reason == "tool_calls":
            stop_reason = "tool_use"
        else:
            stop_reason = finish_reason

        # Map usage
        usage = UsageData.from_openai(data.get("usage", {}))

        return content_blocks, stop_reason, usage

    def translate_stream(self, events: Iterable[dict[str, Any]]) -> Iterable[StreamChunk]:
        """Translate OpenAI SSE events to StreamChunks.

        OpenAI streaming is simpler than Anthropic - there's no explicit
        block_start/block_end. We synthesize them based on content type changes.
        """
        current_text_index = -1
        current_reasoning_index = -1
        tool_call_indices: dict[int, int] = {}  # openai_index -> chunk_index
        next_block_index = 0

        for event in events:
            # Extract usage if present
            u = event.get("usage", {})
            if u:
                usage = UsageData.from_openai(u)
                yield StreamChunk.usage_chunk(usage)

            choices = event.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta", {})
            finish_reason = choices[0].get("finish_reason")

            # Emit finish reason
            if finish_reason:
                if finish_reason == "stop":
                    stop_reason = "end_turn"
                elif finish_reason == "tool_calls":
                    stop_reason = "tool_use"
                else:
                    stop_reason = finish_reason
                yield StreamChunk.finish_chunk(stop_reason)

            # Handle reasoning content (standard) or reasoning (DGX)
            reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning_delta:
                if current_reasoning_index < 0:
                    current_reasoning_index = next_block_index
                    next_block_index += 1
                    yield StreamChunk.block_start(current_reasoning_index, "reasoning")
                yield StreamChunk.reasoning_delta(
                    current_reasoning_index,
                    reasoning_delta,
                )

            # Handle text content
            if "content" in delta and delta["content"]:
                if current_text_index < 0:
                    current_text_index = next_block_index
                    next_block_index += 1
                    yield StreamChunk.block_start(current_text_index, "text")
                yield StreamChunk.text_delta(current_text_index, delta["content"])

            # Handle tool calls
            if "tool_calls" in delta:
                for tc in delta["tool_calls"]:
                    openai_idx = tc.get("index", 0)

                    if openai_idx not in tool_call_indices:
                        # New tool call
                        chunk_idx = next_block_index
                        next_block_index += 1
                        tool_call_indices[openai_idx] = chunk_idx
                        yield StreamChunk.block_start(chunk_idx, "tool_call")

                        # Emit id and name if present
                        tc_id = tc.get("id", "")
                        tc_name = tc.get("function", {}).get("name", "")
                        if tc_id or tc_name:
                            yield StreamChunk.tool_call_delta(
                                chunk_idx,
                                id=tc_id or None,
                                name=tc_name or None,
                            )

                    # Emit arguments delta
                    args_chunk = tc.get("function", {}).get("arguments", "")
                    if args_chunk:
                        yield StreamChunk.tool_call_delta(
                            tool_call_indices[openai_idx],
                            arguments=args_chunk,
                        )

        # Close any open blocks
        if current_reasoning_index >= 0:
            yield StreamChunk.block_end(current_reasoning_index)
        if current_text_index >= 0:
            yield StreamChunk.block_end(current_text_index)
        for idx in tool_call_indices.values():
            yield StreamChunk.block_end(idx)
