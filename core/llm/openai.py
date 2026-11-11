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
    # Anthropic thinking 块（含 signature / redacted），供 thinking_blocks 原样回传
    reasoning: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)


class OpenAIAdapter(Adapter):
    """Adapter for OpenAI Chat Completions API."""

    @property
    def api_path(self) -> str:
        return "/v1/chat/completions"

    def default_auth_headers(self) -> dict[str, str]:
        """auth_type="auto" 的默认认证头。"""
        return {"Authorization": f"Bearer {self.config.api_key}"}

    def protocol_headers(self) -> dict[str, str]:
        return {"content-type": "application/json"}

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

    def _convert_messages(self, messages: list[Message], system: str | list[str]) -> list[dict[str, Any]]:
        """Convert internal messages to OpenAI native format.

        Conversion rules:
        - assistant tool_call blocks → tool_calls field
        - tool role messages → role=tool messages
        - other messages pass through
        """
        openai_messages: list[dict[str, Any]] = []

        if system:
            # OpenAI 不支持 cache_control，list[str] 直接 join 为单字符串
            # 过滤掉 DYNAMIC_BOUNDARY 标记（仅 Anthropic 使用）
            from core.prompt_builder import DYNAMIC_BOUNDARY
            if isinstance(system, list):
                system = "\n\n".join(s for s in system if s != DYNAMIC_BOUNDARY)
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
        # P1-20：纯 reasoning 助手消息也必须发，否则整条静默丢失（原条件
        # 末尾的 parts.tool_calls 是冗余分支，由 parts.reasoning 取代）
        if parts.tool_calls or (role == "assistant" and (parts.text or parts.reasoning)):
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
                thinking_block: dict[str, Any] = {
                    "type": "thinking",
                    "thinking": block_dict.get("thinking", "") or block_dict.get("text", ""),
                }
                if block_dict.get("signature"):
                    thinking_block["signature"] = block_dict["signature"]
                parts.reasoning.append(thinking_block)

            elif btype == "redacted_thinking":
                parts.reasoning.append(
                    {"type": "redacted_thinking", "data": block_dict.get("data", "")}
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
        # CoT passback: reasoning text concatenated back to the wire.
        # Reference: deepseek-harness serializeAssistant() — required on
        # tool-call turns, ignored elsewhere but keeps prefix stable.
        reasoning = "".join(
            b.get("thinking", "") for b in parts.reasoning if b.get("type") == "thinking"
        )
        if reasoning:
            msg_dict["reasoning_content"] = reasoning
        # LitellM 的 OpenAI 兼容端点要求 thinking_blocks（含 signature）原样回传；
        # 缺了它，Anthropic 上游在工具调用后的下一轮会 400
        # "The content[].thinking in the thinking mode must be passed back to the API."
        replayable = any(
            b.get("type") == "redacted_thinking" or b.get("signature")
            for b in parts.reasoning
        )
        if replayable and self.config.reasoning != "off":
            msg_dict["thinking_blocks"] = parts.reasoning
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

    @staticmethod
    def _extract_thinking_blocks(obj: dict[str, Any]) -> list[dict[str, Any]]:
        """提取 LitellM/OpenRouter 风格的 thinking_blocks（含 signature）。

        非流式位于 message.thinking_blocks，流式位于 delta.thinking_blocks，
        部分版本放在 provider_specific_fields.thinking_blocks。
        """
        blocks = obj.get("thinking_blocks")
        if not blocks:
            psf = obj.get("provider_specific_fields")
            if isinstance(psf, dict):
                blocks = psf.get("thinking_blocks")
        return [b for b in (blocks or []) if isinstance(b, dict)]

    @staticmethod
    def _thinking_block_to_content(block: dict[str, Any]) -> ReasoningBlock:
        """thinking_blocks 项 → ReasoningBlock（保留 signature / redacted data）。"""
        if block.get("type") == "redacted_thinking":
            return ReasoningBlock(redacted_data=block.get("data", ""))
        return ReasoningBlock(
            text=block.get("thinking", "") or block.get("text", ""),
            signature=block.get("signature") or None,
        )

    def _prompt_cache_enabled(self) -> bool:
        """根据 cache_control 配置决定是否启用 prompt cache。

        OpenAI 标准 API 不支持显式 cache_control，但部分兼容网关（如 OpenRouter 上的 Anthropic 模型）支持。

        cache_control 值：
        - "auto"（默认）：不启用（OpenAI 标准 API 使用隐式前缀缓存）
        - "true"：强制启用（适用于支持 cache_control 的兼容网关）
        - "false"：强制禁用
        """
        setting = self.config.cache_control
        if setting == "true":
            return True
        # auto 和 false 都不启用
        return False

    def _apply_prompt_cache(
        self,
        body: dict[str, Any],
        system: str | list[str],
        openai_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> None:
        """Prompt cache：对支持 cache_control 的兼容网关启用缓存。

        使用 Anthropic 格式的 cache_control，在 system 最后一段、tools 最后一个、messages 最后一条
        的最后一个 text content 上添加 cache_control 标记。

        注意：标准 OpenAI API 不支持此字段，只有部分兼容网关（如 OpenRouter 上的 Anthropic 模型）支持。
        """
        if not self._prompt_cache_enabled():
            return

        # system 最后一段加 cache_control
        if "system" in body and isinstance(body["system"], list) and body["system"]:
            last_sys = body["system"][-1]
            if isinstance(last_sys, dict) and "text" in last_sys:
                last_sys["cache_control"] = {"type": "ephemeral"}

        # tools 最后一个加 cache_control
        if tools and isinstance(tools, list):
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
            body["tools"] = tools

        # messages 最后一条的最后一个 text content 加 cache_control
        if openai_messages:
            last_msg = openai_messages[-1]
            content = last_msg.get("content")
            if isinstance(content, list) and content:
                # 找到最后一个 text 类型的 content
                for i in range(len(content) - 1, -1, -1):
                    if content[i].get("type") == "text":
                        content[i]["cache_control"] = {"type": "ephemeral"}
                        break

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
        """Serialize to OpenAI Chat Completions API format."""
        # Merge consecutive same-role messages (OpenAI requires alternating roles)
        messages = merge_consecutive_same_role(messages)

        # 处理 system 消息：如果启用 cache_control，转为 list 格式
        if self._prompt_cache_enabled():
            from core.prompt_builder import DYNAMIC_BOUNDARY
            if isinstance(system, list):
                sys_list = [{"type": "text", "text": s} for s in system if s != DYNAMIC_BOUNDARY and s]
            elif system:
                sys_list = [{"type": "text", "text": system}]
            else:
                sys_list = []
            converted_messages = self._convert_messages(messages, "")  # 不传 system，后面单独处理
        else:
            sys_list = None
            converted_messages = self._convert_messages(messages, system)

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": converted_messages,
        }

        if sys_list is not None:
            body["system"] = sys_list

        # Reasoning effort for reasoning models（openai 取值域，§models-provider-design）
        # default：不发送，由 API/网关默认行为决定；off → "none"；其余直传
        reasoning = self.config.reasoning
        if reasoning and reasoning != "default":
            body["reasoning_effort"] = "none" if reasoning == "off" else reasoning

        if tools:
            body["tools"] = self._convert_tools(tools)
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}

        # 应用 prompt cache（如果启用）
        self._apply_prompt_cache(body, system, body["messages"], body.get("tools"))

        return body

    def parse_response(self, data: dict[str, Any]) -> tuple[list[ContentBlock], str, UsageData]:
        """Parse OpenAI API response."""
        content_blocks: list[ContentBlock] = []
        # "choices" 存在但为空列表时，默认值 [{}] 不生效 → 用 or 兜底避免 IndexError
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}

        # LitellM 把 Anthropic thinking 以 thinking_blocks（含 signature）下发；
        # 有它时优先，否则退回 reasoning_content（o1/o3、DGX 等）。
        thinking_blocks = self._extract_thinking_blocks(message)
        for tb in thinking_blocks:
            content_blocks.append(self._thinking_block_to_content(tb))
        if not thinking_blocks:
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
        # .get(key, []) 兜不住「键存在但为 null」（ModelScope 实测会发 null），用 or [] 兜底
        for tc in message.get("tool_calls") or []:
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
        usage = UsageData.from_openai(data.get("usage") or {})

        return content_blocks, stop_reason, usage

    def translate_stream(self, events: Iterable[dict[str, Any]]) -> Iterable[StreamChunk]:
        """Translate OpenAI SSE events to StreamChunks.

        OpenAI streaming is simpler than Anthropic - there's no explicit
        block_start/block_end. We synthesize them based on content type changes.
        """
        current_text_index = -1
        current_reasoning_index = -1
        current_reasoning_text = ""
        thinking_texts: dict[int, str] = {}      # thinking_blocks 序号 -> 已发送文本
        thinking_block_idx: dict[int, int] = {}  # thinking_blocks 序号 -> chunk index
        thinking_redacted: set[int] = set()
        tool_call_indices: dict[int, int] = {}  # openai_index -> chunk_index
        next_block_index = 0

        for event in events:
            # Extract usage if present（chunk 带 model 时一并传给 assembler）
            u = event.get("usage", {})
            if u:
                usage = UsageData.from_openai(u)
                chunk_data = {"usage": usage}
                if event.get("model"):
                    chunk_data["model"] = str(event["model"])
                yield StreamChunk(type="usage", data=chunk_data)

            choices = event.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta") or {}
            finish_reason = choices[0].get("finish_reason")

            # Emit finish reason
            if finish_reason:
                yield StreamChunk.finish_chunk(self._map_finish_reason(finish_reason))

            # LitellM/OpenRouter 的 OpenAI 兼容流把思考块放在 thinking_blocks，
            # signature 随块下发；thinking 模式下必须在工具调用后的下一轮原样带回。
            thinking_blocks = self._extract_thinking_blocks(delta)
            for pos, tb in enumerate(thinking_blocks):
                if pos not in thinking_block_idx:
                    # 首块复用已由 reasoning_content 开出的块，避免同一段思考
                    # 被拆成两个块（先流式增量、后下发带签名的完整块）。
                    if pos == 0 and current_reasoning_index >= 0:
                        chunk_idx = current_reasoning_index
                        thinking_texts[pos] = current_reasoning_text
                    else:
                        chunk_idx = next_block_index
                        next_block_index += 1
                        thinking_texts[pos] = ""
                        yield StreamChunk.block_start(chunk_idx, "reasoning")
                    thinking_block_idx[pos] = chunk_idx

                idx = thinking_block_idx[pos]
                if tb.get("type") == "redacted_thinking":
                    if pos not in thinking_redacted:
                        thinking_redacted.add(pos)
                        yield StreamChunk.redacted_block(idx, tb.get("data", ""))
                    continue

                text = tb.get("thinking", "") or tb.get("text", "")
                prev = thinking_texts.get(pos, "")
                if text and text != prev:
                    if text.startswith(prev):
                        # 累计式快照：只发新增后缀
                        yield StreamChunk.reasoning_delta(idx, text[len(prev):])
                        thinking_texts[pos] = text
                    elif not prev.startswith(text):
                        # 增量式分片：直接拼接
                        yield StreamChunk.reasoning_delta(idx, text)
                        thinking_texts[pos] = prev + text
                signature = tb.get("signature")
                if signature:
                    yield StreamChunk.signature_delta(idx, signature)

            # Handle reasoning content (standard) or reasoning (DGX)
            reasoning_delta = (
                None if thinking_blocks
                else delta.get("reasoning_content") or delta.get("reasoning")
            )
            if reasoning_delta:
                if current_reasoning_index < 0:
                    current_reasoning_index = next_block_index
                    next_block_index += 1
                    yield StreamChunk.block_start(current_reasoning_index, "reasoning")
                current_reasoning_text += reasoning_delta
                yield StreamChunk.reasoning_delta(
                    current_reasoning_index,
                    reasoning_delta,
                )
            elif not thinking_blocks:
                # 签名也可能单独挂在 delta.signature 上（无 thinking_blocks 时）
                signature = delta.get("signature", "")
                if signature and current_reasoning_index >= 0:
                    yield StreamChunk.signature_delta(current_reasoning_index, signature)

            # Handle text content
            if "content" in delta and delta["content"]:
                if current_text_index < 0:
                    current_text_index = next_block_index
                    next_block_index += 1
                    yield StreamChunk.block_start(current_text_index, "text")
                yield StreamChunk.text_delta(current_text_index, delta["content"])

            # Handle tool calls
            if "tool_calls" in delta:
                for tc in delta["tool_calls"] or []:
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
        closed: set[int] = set()
        if current_reasoning_index >= 0:
            yield StreamChunk.block_end(current_reasoning_index)
            closed.add(current_reasoning_index)
        if current_text_index >= 0:
            yield StreamChunk.block_end(current_text_index)
            closed.add(current_text_index)
        for idx in tool_call_indices.values():
            yield StreamChunk.block_end(idx)
            closed.add(idx)
        for idx in thinking_block_idx.values():
            if idx not in closed:
                yield StreamChunk.block_end(idx)
