"""OpenAI Responses API adapter.

Endpoint: /v1/responses
Headers: Authorization: Bearer
SSE events: response.output_item.added / response.output_text.delta /
response.function_call_arguments.delta / response.output_item.done /
response.completed | response.incomplete | response.failed

与 Chat Completions 的关键差异：
- messages → input items（message / function_call / function_call_output / reasoning）
- system prompt → 顶层 instructions
- tools 为扁平格式 {type: "function", name, description, parameters, strict}
- store=false：每回合重放完整历史，不依赖服务端会话
- reasoning 模型的思考重放靠 reasoning item 的 encrypted_content
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from core.llm.adapter import Adapter, merge_consecutive_same_role
from core.llm.errors import LLMStreamFailedError
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

# Responses API 要求 max_output_tokens 不低于 16
_MIN_OUTPUT_TOKENS = 16

# 需要原样回传的思考载荷（store=false 时服务端不保存会话）
_REASONING_INCLUDE = ["reasoning.encrypted_content"]


def _failed_error_fields(detail: Any) -> tuple[str, str, int | None]:
    """从 response.failed / error 事件提取 (code, message, status)。"""
    code = ""
    message = ""
    status = None
    node = detail
    if isinstance(node, dict) and isinstance(node.get("error"), dict):
        node = node["error"]
    if isinstance(node, dict):
        code = str(node.get("code") or node.get("type") or "")
        message = str(node.get("message") or "")
        raw_status = node.get("status")
        if isinstance(raw_status, int):
            status = raw_status
    if not message:
        message = str(detail)[:300]
    return code, message, status


class OpenAIResponsesAdapter(Adapter):
    """Adapter for OpenAI Responses API."""

    @property
    def default_api_path(self) -> str:
        return "/v1/responses"

    def default_auth_headers(self) -> dict[str, str]:
        """auth_type="auto" 的默认认证头。"""
        return {"Authorization": f"Bearer {self.config.api_key}"}

    def protocol_headers(self) -> dict[str, str]:
        return {"content-type": "application/json"}

    @property
    def is_siwc(self) -> bool:
        """OAuth（Sign in with ChatGPT）链路：走预览期请求整形。"""
        return getattr(self.config, "auth_type", "") == "oauth"

    @property
    def requires_streaming(self) -> bool:
        return self.is_siwc

    # ========== 请求序列化 ==========

    def _convert_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """Anthropic 工具 schema → Responses 扁平 function 格式。

        strict 显式给 false：省略时部分端点会把可选字段视为必填。
        """
        if not tools:
            return None
        return [
            {
                "type": "function",
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", t.get("parameters", {})),
                "strict": False,
            }
            for t in tools
        ]

    def _apply_reasoning(self, body: dict[str, Any]) -> None:
        """按 reasoning 档位设置 Responses 的 reasoning.effort。

        - default：不发送，交给服务端默认
        - off：effort="none"
        - 其余档位直传；同时请求 summary 以便界面展示思考摘要
        """
        if getattr(self.config, "model_type", "reasoning") == "chat":
            return
        effort = self.config.reasoning
        if not effort or effort == "default":
            return
        if effort == "off":
            body["reasoning"] = {"effort": "none"}
            return
        body["reasoning"] = {"effort": effort, "summary": "auto"}
        # store=false 时必须显式索取加密思考内容，才能在下一轮原样回传
        body["include"] = list(_REASONING_INCLUDE)

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """内部 Message 列表 → Responses input items。"""
        items: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.content
            role = msg.role

            if isinstance(content, str):
                if content:
                    items.extend(self._text_items(role, content))
                continue
            if not content:
                continue

            if role == "assistant":
                items.extend(self._assistant_items(content))
            else:
                items.extend(self._user_items(content, role))

        return items

    def _text_items(self, role: str, text: str) -> list[dict[str, Any]]:
        """纯文本消息 → 单条 input item。"""
        if role == "assistant":
            return [self._assistant_text_item(text)]
        return [{
            "role": role if role in ("system", "developer") else "user",
            "content": [{"type": "input_text", "text": text}],
        }]

    @staticmethod
    def _assistant_text_item(text: str) -> dict[str, Any]:
        return {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
            "status": "completed",
        }

    def _assistant_items(self, content: list[ContentBlock]) -> list[dict[str, Any]]:
        """assistant 轮：reasoning / text / function_call 按原顺序重放。"""
        items: list[dict[str, Any]] = []
        pending_text: list[str] = []

        def flush_text() -> None:
            if pending_text:
                items.append(self._assistant_text_item("".join(pending_text)))
                pending_text.clear()

        for block in content:
            if isinstance(block, TextBlock):
                pending_text.append(block.text)
                continue

            if isinstance(block, ReasoningBlock):
                item = self._reasoning_item(block)
                if item is not None:
                    flush_text()
                    items.append(item)
                continue

            if isinstance(block, ToolCallBlock):
                flush_text()
                item: dict[str, Any] = {
                    "type": "function_call",
                    "call_id": block.id,
                    "name": block.name,
                    "arguments": block.arguments or "{}",
                }
                # 保留 fc_... item id，官方端点靠它与 reasoning item 配对
                if block.item_id:
                    item["id"] = block.item_id
                items.append(item)

        flush_text()
        return items

    @staticmethod
    def _reasoning_item(block: ReasoningBlock) -> dict[str, Any] | None:
        """把落盘的思考块还原为 Responses reasoning item。

        signature 存的是完整 reasoning item JSON（含 encrypted_content）。
        无有效载荷时跳过——Anthropic/第三方签名不能冒充 Responses item。
        """
        signature = block.signature or ""
        if not signature:
            return None
        try:
            item = json.loads(signature)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            return None
        # 没有 encrypted_content 的 reasoning item 回放价值有限，且部分端点
        # 会因「reasoning 未与后续 item 配对」直接 400；宁可不回放。
        if not item.get("encrypted_content"):
            return None
        # id 必须保留：官方端点靠它与后续 function_call item 配对；status 是
        # 输出侧字段，重放时去掉。
        return {k: v for k, v in item.items() if k != "status"}

    def _user_items(self, content: list[ContentBlock], role: str) -> list[dict[str, Any]]:
        """user/tool 轮：工具结果先出，剩余文本与图片合成一条 user 消息。"""
        items: list[dict[str, Any]] = []
        text_parts: list[str] = []
        images: list[dict[str, Any]] = []

        for block in content:
            block_dict = block_to_dict(block)
            btype = block_dict.get("type", "")

            if btype == "tool_result":
                items.append({
                    "type": "function_call_output",
                    "call_id": block_dict.get("tool_call_id", block_dict.get("tool_use_id", "")),
                    "output": self._tool_output(block_dict.get("content", "")),
                })
            elif btype == "text":
                text_parts.append(block_dict.get("text", ""))
            elif btype == "image":
                image = self._image_part(block_dict.get("source", {}))
                if image is not None:
                    images.append(image)

        text = "\n".join(p for p in text_parts if p)
        if text or images:
            parts: list[dict[str, Any]] = []
            if text:
                parts.append({"type": "input_text", "text": text})
            parts.extend(images)
            items.append({
                "role": role if role in ("system", "developer") else "user",
                "content": parts,
            })
        return items

    @staticmethod
    def _image_part(source: dict[str, Any]) -> dict[str, Any] | None:
        """Anthropic base64 图片块 → Responses input_image。"""
        if source.get("type") != "base64":
            return None
        media_type = source.get("media_type", "image/png")
        return {
            "type": "input_image",
            "image_url": f"data:{media_type};base64,{source.get('data', '')}",
            "detail": "auto",
        }

    def _tool_output(self, content: Any) -> str | list[dict[str, Any]]:
        """工具结果 → function_call_output.output（字符串或 input_text/input_image 列表）。"""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return json.dumps(content, ensure_ascii=False) if content else ""

        parts: list[dict[str, Any]] = []
        for raw in content:
            if not isinstance(raw, dict):
                parts.append({"type": "input_text", "text": str(raw)})
                continue
            rtype = raw.get("type", "")
            if rtype in ("text", "input_text"):
                parts.append({"type": "input_text", "text": raw.get("text", "")})
            elif rtype in ("image", "input_image"):
                source = raw.get("source", {}) if rtype == "image" else raw
                image = self._image_part(source)
                if image is None:
                    url = raw.get("image_url")
                    if isinstance(url, str) and url:
                        image = {
                            "type": "input_image",
                            "image_url": url,
                            "detail": raw.get("detail", "auto"),
                        }
                if image is not None:
                    parts.append(image)
            else:
                parts.append({"type": "input_text", "text": str(raw)})

        if all(p.get("type") == "input_text" for p in parts):
            return "\n".join(p.get("text", "") for p in parts)
        return parts

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
        """Serialize to OpenAI Responses API format."""
        messages = merge_consecutive_same_role(messages)

        from core.prompt_builder import DYNAMIC_BOUNDARY
        if isinstance(system, list):
            instructions = "\n\n".join(s for s in system if s and s != DYNAMIC_BOUNDARY)
        else:
            instructions = system or ""

        body: dict[str, Any] = {
            "model": model,
            "input": self._convert_messages(messages),
            "store": False,
        }
        if instructions:
            body["instructions"] = instructions
        # Responses 对输出上限有下限要求
        body["max_output_tokens"] = max(int(max_tokens or 0), _MIN_OUTPUT_TOKENS)

        self._apply_reasoning(body)

        if tools:
            body["tools"] = self._convert_tools(tools)
            body["tool_choice"] = "auto"

        if stream:
            body["stream"] = True

        # prompt_cache_key 改善网关缓存路由；cache_control="false" 时显式关闭
        if session_id and self.config.cache_control != "false":
            body["prompt_cache_key"] = session_id[:64]

        if self.is_siwc:
            from core.llm.siwc.profile import shape_request
            body = shape_request(body)

        return body

    # ========== 响应解析 ==========

    @staticmethod
    def _reasoning_text(item: dict[str, Any]) -> str:
        """提取 reasoning item 的摘要/正文文本。"""
        parts: list[str] = []

        for summary in item.get("summary") or []:
            if isinstance(summary, dict) and summary.get("text"):
                parts.append(str(summary["text"]))
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("text"):
                    parts.append(str(block["text"]))
        return "\n\n".join(p for p in parts if p)

    def _parse_output_items(
        self, output: list[dict[str, Any]]
    ) -> tuple[list[ContentBlock], bool]:
        """Responses output items → 内部 content blocks。"""
        blocks: list[ContentBlock] = []
        has_tool_call = False

        for item in output or []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")

            if item_type == "message":
                for part in item.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "output_text" and part.get("text"):
                        blocks.append(TextBlock(text=str(part["text"])))
                    elif part.get("type") == "refusal" and part.get("refusal"):
                        blocks.append(TextBlock(text=f"[模型拒绝请求] {part['refusal']}"))

            elif item_type == "reasoning":
                blocks.append(ReasoningBlock(
                    text=self._reasoning_text(item),
                    signature=json.dumps(item, ensure_ascii=False),
                ))

            elif item_type == "function_call":
                has_tool_call = True
                blocks.append(ToolCallBlock(
                    id=str(item.get("call_id", "")),
                    name=str(item.get("name", "")),
                    arguments=self._arguments_text(item.get("arguments")),
                    item_id=str(item.get("id") or ""),
                ))

        return blocks, has_tool_call

    @staticmethod
    def _arguments_text(value: Any) -> str:
        """工具参数统一为 JSON 字符串（个别网关会回 dict）。"""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _stop_reason(
        status: str,
        incomplete_reason: str = "",
        has_tool_call: bool = False,
    ) -> str:
        """Responses status → 中性 stop_reason。"""
        if status == "incomplete":
            if incomplete_reason == "content_filter":
                return "content_filter"
            return "length"
        if status in ("failed", "cancelled"):
            return "error"
        return "tool_use" if has_tool_call else "end_turn"

    def parse_response(self, data: dict[str, Any]) -> tuple[list[ContentBlock], str, UsageData]:
        """Parse non-streaming Responses output."""
        output = data.get("output") or []
        content_blocks, has_tool_call = self._parse_output_items(output)

        incomplete = data.get("incomplete_details") or {}
        incomplete_reason = incomplete.get("reason", "") if isinstance(incomplete, dict) else ""
        stop_reason = self._stop_reason(
            str(data.get("status", "")), incomplete_reason, has_tool_call
        )
        usage = UsageData.from_responses(data.get("usage") or {})
        return content_blocks, stop_reason, usage

    def translate_stream(self, events: Iterable[dict[str, Any]]) -> Iterable[StreamChunk]:
        """Translate Responses SSE events to StreamChunks."""
        next_index = 0
        raw_to_chunk: dict[int, int] = {}
        item_to_chunk: dict[str, int] = {}
        started: dict[int, str] = {}
        text_seen: set[int] = set()
        reasoning_seen: set[int] = set()
        args_seen: set[int] = set()
        header_emitted: set[int] = set()
        saw_tool_call = False
        model = ""
        terminal = False

        def chunk_index(raw_output: Any, item_id: Any) -> int:
            nonlocal next_index
            if isinstance(raw_output, int) and not isinstance(raw_output, bool):
                idx = raw_to_chunk.get(raw_output)
                if idx is not None:
                    return idx
            if isinstance(item_id, str) and item_id in item_to_chunk:
                return item_to_chunk[item_id]
            idx = next_index
            next_index += 1
            if isinstance(raw_output, int) and not isinstance(raw_output, bool):
                raw_to_chunk[raw_output] = idx
            if isinstance(item_id, str) and item_id:
                item_to_chunk[item_id] = idx
            return idx

        def start(idx: int, kind: str) -> StreamChunk | None:
            if idx in started:
                return None
            started[idx] = kind
            return StreamChunk.block_start(idx, kind)

        def emit_item(idx: int, item: dict[str, Any]) -> Iterable[StreamChunk]:
            """补发一个此前未流式输出的完整 output item。"""
            nonlocal saw_tool_call
            item_type = item.get("type", "")

            if item_type == "reasoning":
                chunk = start(idx, "reasoning")
                if chunk is not None:
                    yield chunk
                text = self._reasoning_text(item)
                if text and idx not in reasoning_seen:
                    reasoning_seen.add(idx)
                    yield StreamChunk.reasoning_delta(idx, text)
                yield StreamChunk.signature_delta(idx, json.dumps(item, ensure_ascii=False))

            elif item_type == "message":
                chunk = start(idx, "text")
                if chunk is not None:
                    yield chunk
                text_parts: list[str] = []
                for part in item.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "output_text" and part.get("text"):
                        text_parts.append(str(part["text"]))
                    elif part.get("type") == "refusal" and part.get("refusal"):
                        text_parts.append(f"[模型拒绝请求] {part['refusal']}")
                text = "".join(text_parts)
                if text and idx not in text_seen:
                    text_seen.add(idx)
                    yield StreamChunk.text_delta(idx, text)

            elif item_type == "function_call":
                saw_tool_call = True
                chunk = start(idx, "tool_call")
                if chunk is not None:
                    yield chunk
                if idx not in header_emitted:
                    header_emitted.add(idx)
                    yield StreamChunk.tool_call_delta(
                        idx,
                        id=str(item.get("call_id") or "") or None,
                        name=str(item.get("name") or "") or None,
                        item_id=str(item.get("id") or "") or None,
                    )
                arguments = self._arguments_text(item.get("arguments"))
                if arguments and idx not in args_seen:
                    args_seen.add(idx)
                    yield StreamChunk.tool_call_delta(idx, arguments=arguments)

        for event in events:
            evt_type = event.get("type", "")

            if evt_type == "response.created":
                response = event.get("response") or {}
                if isinstance(response, dict) and response.get("model"):
                    model = str(response["model"])

            elif evt_type == "response.output_item.added":
                item = event.get("item") or {}
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type", "")
                if item_type not in ("reasoning", "message", "function_call"):
                    continue
                idx = chunk_index(event.get("output_index"), item.get("id"))
                if item_type == "function_call":
                    saw_tool_call = True
                    chunk = start(idx, "tool_call")
                    if chunk is not None:
                        yield chunk
                    if idx not in header_emitted:
                        header_emitted.add(idx)
                        yield StreamChunk.tool_call_delta(
                            idx,
                            id=str(item.get("call_id") or "") or None,
                            name=str(item.get("name") or "") or None,
                            item_id=str(item.get("id") or "") or None,
                        )
                    # 个别网关把首段参数直接放在 added item 里
                    arguments = self._arguments_text(item.get("arguments"))
                    if arguments and idx not in args_seen:
                        args_seen.add(idx)
                        yield StreamChunk.tool_call_delta(idx, arguments=arguments)
                else:
                    chunk = start(idx, "reasoning" if item_type == "reasoning" else "text")
                    if chunk is not None:
                        yield chunk

            elif evt_type == "response.output_text.delta":
                delta = event.get("delta") or ""
                if not delta:
                    continue
                idx = chunk_index(event.get("output_index"), event.get("item_id"))
                chunk = start(idx, "text")
                if chunk is not None:
                    yield chunk
                text_seen.add(idx)
                yield StreamChunk.text_delta(idx, delta)

            elif evt_type == "response.refusal.delta":
                delta = event.get("delta") or ""
                if not delta:
                    continue
                idx = chunk_index(event.get("output_index"), event.get("item_id"))
                chunk = start(idx, "text")
                if chunk is not None:
                    yield chunk
                text_seen.add(idx)
                yield StreamChunk.text_delta(idx, delta)

            elif evt_type in (
                "response.reasoning_summary_text.delta",
                "response.reasoning_text.delta",
            ):
                delta = event.get("delta") or ""
                if not delta:
                    continue
                idx = chunk_index(event.get("output_index"), event.get("item_id"))
                chunk = start(idx, "reasoning")
                if chunk is not None:
                    yield chunk
                reasoning_seen.add(idx)
                yield StreamChunk.reasoning_delta(idx, delta)

            elif evt_type == "response.function_call_arguments.delta":
                delta = event.get("delta") or ""
                if not delta:
                    continue
                idx = chunk_index(event.get("output_index"), event.get("item_id"))
                chunk = start(idx, "tool_call")
                if chunk is not None:
                    saw_tool_call = True
                    yield chunk
                args_seen.add(idx)
                yield StreamChunk.tool_call_delta(
                    idx,
                    arguments=delta,
                    item_id=str(event.get("item_id") or "") or None,
                )

            elif evt_type == "response.function_call_arguments.done":
                idx = chunk_index(event.get("output_index"), event.get("item_id"))
                arguments = event.get("arguments") or ""
                chunk = start(idx, "tool_call")
                if chunk is not None:
                    saw_tool_call = True
                    yield chunk
                if arguments and idx not in args_seen:
                    args_seen.add(idx)
                    yield StreamChunk.tool_call_delta(
                        idx,
                        arguments=arguments,
                        item_id=str(event.get("item_id") or "") or None,
                    )

            elif evt_type == "response.output_item.done":
                item = event.get("item") or {}
                if not isinstance(item, dict):
                    continue
                idx = chunk_index(event.get("output_index"), item.get("id"))
                yield from emit_item(idx, item)

            elif evt_type in ("response.completed", "response.incomplete"):
                response = event.get("response") or {}
                if not isinstance(response, dict):
                    response = {}

                # 完整 output 兜底：未流式下发的 item / 未收到的文本在这里补发；
                # 思考签名以终态 output 为准（encrypted_content 可能只在这里下发）。
                # emit_item 内部按 text_seen/args_seen 去重，重复调用不会追加两次。
                output = response.get("output") or []
                for raw_idx, item in enumerate(output):
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "function_call":
                        saw_tool_call = True
                    idx = chunk_index(raw_idx, item.get("id"))
                    yield from emit_item(idx, item)

                if response.get("model"):
                    model = str(response["model"])
                usage_raw = response.get("usage") or {}
                if usage_raw:
                    usage_data: dict[str, Any] = {
                        "usage": UsageData.from_responses(usage_raw)
                    }
                    if model:
                        usage_data["model"] = model
                    yield StreamChunk(type="usage", data=usage_data)

                incomplete = response.get("incomplete_details") or {}
                incomplete_reason = (
                    incomplete.get("reason", "") if isinstance(incomplete, dict) else ""
                )
                stop_reason = self._stop_reason(
                    str(response.get("status") or evt_type.removeprefix("response.")),
                    incomplete_reason,
                    saw_tool_call,
                )
                yield StreamChunk.finish_chunk(stop_reason)
                terminal = True
                break

            elif evt_type == "response.failed":
                response = event.get("response") or {}
                error = response.get("error") if isinstance(response, dict) else None
                detail = error or response or event
                code, message, status = _failed_error_fields(detail)
                suffix = f" ({code})" if code else ""
                raise LLMStreamFailedError(
                    code=code,
                    message=f"OpenAI Responses 请求失败: {message}{suffix}",
                    status=status,
                )

            elif evt_type == "error":
                detail = event.get("error") or event.get("message") or event
                code, message, status = _failed_error_fields(detail)
                suffix = f" ({code})" if code else ""
                raise LLMStreamFailedError(
                    code=code,
                    message=f"OpenAI Responses 错误: {message}{suffix}",
                    status=status,
                )

        if not terminal:
            # 与其它 adapter 一致：流结束关闭仍未关闭的块
            for idx in started:
                yield StreamChunk.block_end(idx)
