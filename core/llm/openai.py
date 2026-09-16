"""OpenAI Chat Completions API adapter.

Endpoint: /v1/chat/completions
Headers: Authorization: Bearer
SSE events: choices[0].delta with finish_reason
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

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


@dataclass
class _MsgParts:
    """单条消息 content blocks 的分类结果，供 _convert_message 各分支消费。"""

    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)


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
            self._convert_message(openai_messages, msg)

        return openai_messages

    def _convert_message(self, out: list[dict[str, Any]], msg: Message) -> None:
        """将单条内部 Message 转为 OpenAI 消息并追加到 out。"""
        role = msg.role
        content = msg.content

        # String content passes through directly
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            return

        # Handle None content (e.g., assistant messages with only tool_calls)
        if content is None:
            out.append({"role": role, "content": None})
            return

        parts = self._classify_blocks(content)

        # Assistant message: text + reasoning_content + tool_calls
        if parts.tool_calls or (role == "assistant" and (parts.text or parts.tool_calls)):
            out.append(self._build_assistant_message(parts))

        # Tool results: each becomes a separate role=tool message；其中的图片收集
        # 起来并入后续 user 消息（OpenAI 不支持 role=tool 带图）
        images_from_tools = self._emit_tool_results(out, parts.tool_results)

        if images_from_tools:
            # Include any text from the original message
            out.append({
                "role": "user",
                "content": self._build_user_content("\n".join(parts.text), images_from_tools),
            })
        # User message with images (from paste) — multimodal content
        elif parts.images and role == "user":
            out.append({
                "role": "user",
                "content": self._build_user_content("\n".join(parts.text), parts.images),
            })
        # User message with tool results and leftover text: tool results already
        # emitted as role=tool, emit the remaining text as a user message.
        # （merge_consecutive_same_role 合并相邻 user 消息后会出现此形态，
        #  之前 text_parts 被静默丢弃，用户问话不发模型）
        elif parts.tool_results and parts.text and role == "user":
            out.append({"role": "user", "content": "\n".join(parts.text)})
        # User message with only text (no tool calls or results)
        elif not parts.tool_calls and not parts.tool_results and parts.text and role != "assistant":
            out.append({"role": role, "content": "\n".join(parts.text)})

    def _classify_blocks(self, content: list[ContentBlock]) -> _MsgParts:
        """将 content blocks 按类型分类，供 _convert_message 各分支消费。"""
        parts = _MsgParts()

        for block in content:
            block_dict = block_to_dict(block)
            btype = block_dict.get("type", "")

            if btype == "text":
                parts.text.append(block_dict.get("text", ""))

            # Support both Anthropic format ("thinking") and legacy format ("reasoning")
            elif btype in ("reasoning", "thinking"):
                # Anthropic: {"type": "thinking", "thinking": "..."}
                # Legacy: {"type": "reasoning", "text": "..."}
                parts.reasoning.append(
                    block_dict.get("thinking", "") or block_dict.get("text", "")
                )

            elif btype == "image":
                # Convert ImageBlock to OpenAI image_url format
                source = block_dict.get("source", {})
                if source.get("type") == "base64":
                    parts.images.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
                        },
                    })

            # Support both Anthropic format ("tool_use") and legacy format ("tool_call")
            elif btype in ("tool_call", "tool_use"):
                parts.tool_calls.append(self._to_openai_tool_call(block_dict))

            elif btype == "tool_result":
                parts.tool_results.append(block_dict)

        return parts

    def _to_openai_tool_call(self, block_dict: dict[str, Any]) -> dict[str, Any]:
        """将内部 tool_use/tool_call block 转为 OpenAI tool_calls 项。"""
        # Anthropic: {"type": "tool_use", "id", "name", "input": {...}}
        # Legacy: {"type": "tool_call", "id", "name", "arguments": "..."}
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

        tool_call_dict: dict[str, Any] = {
            "id": block_dict.get("id", ""),
            "type": "function",
            "function": {
                "name": block_dict.get("name", ""),
                "arguments": arguments,
            },
        }
        # Google Gemini: include thought_signature in extra_content.google
        thought_sig = block_dict.get("thought_signature", "")
        if thought_sig:
            tool_call_dict["extra_content"] = {
                "google": {"thought_signature": thought_sig}
            }
        return tool_call_dict

    def _build_assistant_message(self, parts: _MsgParts) -> dict[str, Any]:
        """组装 assistant 消息：content + reasoning_content + tool_calls。"""
        msg_dict: dict[str, Any] = {"role": "assistant"}
        # OpenAI 规范：仅含 tool_calls 时 content 必须为 null（空串会被部分
        # 兼容端点拒绝）
        content = "\n".join(parts.text)
        msg_dict["content"] = content if content else None
        # CoT passback: reasoning blocks concatenated back to the wire.
        # Reference: deepseek-harness serializeAssistant() — required on
        # tool-call turns, ignored elsewhere but keeps prefix stable.
        reasoning = "".join(parts.reasoning)
        if reasoning:
            msg_dict["reasoning_content"] = reasoning
        if parts.tool_calls:
            msg_dict["tool_calls"] = parts.tool_calls
        return msg_dict

    def _emit_tool_results(
        self,
        out: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """将 tool_result blocks 逐条转为 role=tool 消息，返回其中的图片供并入 user 消息。"""
        images_from_tools: list[dict[str, Any]] = []

        for tr in tool_results:
            tc = tr.get("content", "")
            if isinstance(tc, list):
                # Extract text and images separately
                text_parts_tool: list[str] = []
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

            out.append({
                "role": "tool",
                "tool_call_id": tr.get("tool_call_id", tr.get("tool_use_id", "")),
                "content": tc,
            })

        return images_from_tools

    def _build_user_content(self, text: str, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """组装 user 多模态 content：可选文本 + 图片列表。"""
        user_content: list[dict[str, Any]] = []
        if text:
            user_content.append({"type": "text", "text": text})
        user_content.extend(images)
        return user_content

    def _map_finish_reason(self, finish_reason: str) -> str:
        """将 OpenAI finish_reason 映射为中性 stop_reason。"""
        if finish_reason == "stop":
            return "end_turn"
        if finish_reason == "tool_calls":
            return "tool_use"
        return finish_reason

    def _extract_thought_signature(self, tc: dict[str, Any]) -> str:
        """提取 Google Gemini thought_signature（位于 extra_content.google）。"""
        return tc.get("extra_content", {}).get("google", {}).get("thought_signature", "")

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
        # Merge consecutive same-role messages (OpenAI requires alternating roles)
        messages = merge_consecutive_same_role(messages)

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": self._convert_messages(messages, system),
        }

        # Reasoning effort for reasoning models
        # Only send if explicitly configured; otherwise let API decide
        reasoning_effort = self.config.reasoning_effort
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        else:
            if temperature is not None:
                body["temperature"] = temperature

        # LiteLLM proxy support
        self._apply_litellm_extras(body, session_id)

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

        # OpenAI refusal: content 为 None 时拒绝原因放在 refusal 字段，
        # 不处理会得到「成功但空」的响应、拒绝原因丢失。
        refusal = message.get("refusal")
        if refusal and not message.get("content"):
            content_blocks.append(TextBlock(text=f"[模型拒绝请求] {refusal}"))

        # Tool calls - keep arguments as raw JSON string
        for tc in message.get("tool_calls", []):
            func = tc.get("function", {})
            content_blocks.append(ToolCallBlock(
                id=tc.get("id", ""),
                name=func.get("name", ""),
                arguments=func.get("arguments", ""),
                thought_signature=self._extract_thought_signature(tc),
            ))

        # Map finish_reason
        stop_reason = self._map_finish_reason(choice.get("finish_reason", ""))

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
                yield StreamChunk.finish_chunk(self._map_finish_reason(finish_reason))

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
                        tc_thought_sig = self._extract_thought_signature(tc)
                        if tc_id or tc_name or tc_thought_sig:
                            yield StreamChunk.tool_call_delta(
                                chunk_idx,
                                id=tc_id or None,
                                name=tc_name or None,
                                thought_signature=tc_thought_sig or None,
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
