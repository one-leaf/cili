"""LLMClient - provider-agnostic LLM API client.

Ties together Adapter (protocol translation), HttpTransport (HTTP), and
BlockAssembler (stream accumulation). Provides the public API:
- chat()          — non-streaming
- chat_stream()   — streaming with callbacks
- chat_structured() — forced tool use for structured output
- test_connection() — connectivity test
- close()         — resource cleanup

Usage:
    from core.llm import LLMClient, create_llm_client
    from core.config import ModelConfig

    config = ModelConfig(name="claude-sonnet-4-6", api_key="...", ...)
    client = create_llm_client(config)
    response = client.chat(messages, system="You are helpful")
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

import httpx

from core.config import ModelConfig
from core.llm.adapter import Adapter
from core.llm.assembler import BlockAssembler
from core.llm.transport import HttpTransport
from core.llm.types import (
    ContentBlock,
    Message,
    StreamChunk,
    TextBlock,
    ToolCallBlock,
    UsageData,
    LLMResponse,
    format_llm_error,
)

logger = logging.getLogger(__name__)


def _extract_json_dict(text: str) -> dict | None:
    """尽力从模型正文中解析 JSON 对象。

    end_turn 兜底：模型未调用工具但直接在正文输出 JSON（小模型/工具调用异常时
    常见）。依次尝试整段解析、去掉 markdown 围栏后解析、取最外层 {...} 子串解析。
    """
    candidates = [text.strip()]
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*\n?", "", stripped)
        stripped = re.sub(r"\n?\s*```$", "", stripped)
        candidates.append(stripped.strip())
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    if m:
        repaired = _repair_truncated_json(m.group(0))
        if repaired is not None:
            return repaired
    return None


def _balance_close(text: str) -> str | None:
    """补齐未闭合的 {} 与 []；若截断点非法（闭合多于开启）返回 None。"""
    stack: list[str] = []
    in_string = False
    i = 0
    n = len(text)
    pairs = {"}": "{", "]": "["}
    while i < n:
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
        elif c in "{[":
            stack.append(c)
        elif c in "}]":
            if not stack or stack[-1] != pairs[c]:
                return None
            stack.pop()
        i += 1
    for opener in reversed(stack):
        text += "}" if opener == "{" else "]"
    return text


def _cut_positions(text: str, cap: int = 40) -> list[int]:
    """可作为安全截断点的位置（从后往前）：每个完整 } 或 ] 之后。"""
    positions: list[int] = []
    in_string = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
        elif c in "}]":
            positions.append(i + 1)
        i += 1
    positions.reverse()
    return positions[:cap]


def _repair_truncated_json(text: str) -> dict | None:
    """修复被 token 截断的结构化输出 JSON。

    截断点可能在字符串中间、元素中间或元素结束后缺闭合括号。策略：从后往前
    尝试候选截断点（最近的完整元素边界），补齐闭合括号后解析，取第一个能解析
    的 dict。截断只发生在尾部，因此最多丢弃末尾不完整的元素，前面已完成的
    元素全部保留。
    """
    for cut in [len(text)] + _cut_positions(text):
        repaired = _balance_close(text[:cut])
        if repaired is None:
            continue
        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


class LLMClient:
    """Provider-agnostic LLM client.

    Delegates wire-format handling to the Adapter and HTTP transport to
    HttpTransport. Accumulates streaming responses via BlockAssembler.
    """

    def __init__(
        self,
        adapter: Adapter,
        transport: HttpTransport,
        config: ModelConfig,
    ):
        """Initialize client.

        Args:
            adapter: Provider adapter (AnthropicAdapter or OpenAIAdapter)
            transport: HTTP transport layer
            config: Model configuration
        """
        self.adapter = adapter
        self.transport = transport
        self.config = config
        self.model_name = config.name
        self.max_tokens = config.max_tokens
        self.temperature = config.temperature

        # Expose base_url for error formatting (backward compat)
        self.base_url = config.base_url

        # Detect LiteLLM proxy（官方域名跳过，避免启动时多余请求）
        if adapter.should_detect_litellm():
            adapter.detect_litellm(transport)

    def chat(
        self,
        messages: list[Message],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        session_id: str = "",
        timeout: httpx.Timeout | None = None,
    ) -> LLMResponse:
        """Non-streaming LLM call.

        Args:
            messages: Conversation messages (list[Message])
            system: System prompt
            tools: Tool schemas
            max_tokens: Override default max_tokens
            session_id: Optional session ID for proxy routing
            timeout: Optional per-phase timeout override for this call

        Returns:
            LLMResponse with content blocks, stop_reason, usage

        Raises:
            httpx.HTTPStatusError: On API error
            httpx.TransportError: On network error
        """
        max_tokens = max_tokens or self.max_tokens
        body = self.adapter.serialize(
            messages=messages,
            system=system,
            tools=tools,
            model=self.model_name,
            max_tokens=max_tokens,
            temperature=self.temperature,
            stream=False,
            session_id=session_id,
        )
        headers = self.adapter.build_headers()
        url = self.adapter.api_url

        def do_request():
            status, resp_headers, data = self.transport.post(url, headers, body, timeout=timeout)
            if status >= 400:
                # Raise for retry logic
                resp = httpx.Response(status_code=status, request=httpx.Request("POST", url))
                resp._content = json.dumps(data, ensure_ascii=False).encode("utf-8")
                resp.headers.update(resp_headers)
                raise httpx.HTTPStatusError(
                    f"HTTP {status}", request=resp.request, response=resp
                )
            return data

        data = self.transport.with_retry(do_request)
        content_blocks, stop_reason, usage = self.adapter.parse_response(data)

        return LLMResponse(
            content=content_blocks,
            stop_reason=stop_reason,
            usage=usage,
        )

    def chat_stream(
        self,
        messages: list[Message],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        on_chunk: Callable[[StreamChunk], None] | None = None,
        on_text: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_tool_call: Callable[[ToolCallBlock], None] | None = None,
        stop_check: Callable[[], bool] | None = None,
        session_id: str = "",
        timeout: httpx.Timeout | None = None,
    ) -> LLMResponse:
        """Streaming LLM call with callbacks.

        The stream is translated by the adapter into neutral StreamChunks,
        accumulated by the BlockAssembler, and dispatched to callbacks.

        Args:
            messages: Conversation messages (list[Message])
            system: System prompt
            tools: Tool schemas
            max_tokens: Override default max_tokens
            on_chunk: Called for every StreamChunk (low-level)
            on_text: Called for text deltas (high-level)
            on_thinking: Called for reasoning deltas (high-level)
            on_tool_call: Called when a new tool call block starts
            stop_check: If returns True, stream is interrupted
            session_id: Optional session ID for proxy routing
            timeout: Optional per-phase timeout override for this call

        Returns:
            LLMResponse with assembled content blocks

        Raises:
            RuntimeError: 流结束后未收到 finish chunk 且无任何内容（视为失败，供上层重试）
        """
        max_tokens = max_tokens or self.max_tokens
        body = self.adapter.serialize(
            messages=messages,
            system=system,
            tools=tools,
            model=self.model_name,
            max_tokens=max_tokens,
            temperature=self.temperature,
            stream=True,
            session_id=session_id,
        )
        headers = self.adapter.build_headers()
        url = self.adapter.api_url
        assembler = BlockAssembler()

        def do_stream():
            """Execute streaming request and process chunks."""
            assembler.reset()  # Reset on retry to avoid duplicate data
            events = self.transport.stream(url, headers, body, stop_check=stop_check, timeout=timeout)
            # Pass entire event iterator to translate_stream so it maintains
            # state (tool_call_indices, etc.) across events.
            for chunk in self.adapter.translate_stream(events):
                # Dispatch to low-level callback
                if on_chunk:
                    on_chunk(chunk)

                # Dispatch to high-level callbacks
                if chunk.type == "text_delta" and on_text:
                    on_text(chunk.data.get("text", ""))
                elif chunk.type == "reasoning_delta" and on_thinking:
                    on_thinking(chunk.data.get("text", ""))

                # Accumulate
                assembler.push(chunk)

            return assembler

        # 流式重试由 base_agent 统一管理（max_retries=0），避免双层重试
        self.transport.with_retry(
            do_stream,
            stop_check=stop_check,
            max_retries=0,
        )

        # 流结束未收到 finish chunk 且无任何内容 = 连接被静默截断，视为失败供上层重试。
        # （有内容但缺 finish chunk 的兼容服务器不误判。）
        if not assembler.finished and not assembler.blocks:
            raise RuntimeError("LLM 流结束但未收到完成信号（可能被截断），请重试")

        # Notify about completed tool calls
        if on_tool_call:
            for block in assembler.get_tool_calls():
                on_tool_call(block)

        return LLMResponse(
            content=assembler.blocks,
            stop_reason=assembler.stop_reason,
            usage=assembler.usage,
        )

    def chat_structured(
        self,
        messages: list[Message],
        system: str = "",
        output_schema: dict[str, Any] | None = None,
        tool_name: str = "output",
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Force structured output via tool use.

        Sends a single tool with the output schema and parses the tool
        call arguments as the structured result.

        Args:
            messages: Conversation messages
            system: System prompt
            output_schema: JSON Schema for expected output
            tool_name: Name of the synthetic tool
            max_tokens: Override default max_tokens

        Returns:
            Parsed arguments dict from the tool call

        Raises:
            ValueError: If no tool call is returned
        """
        if not output_schema:
            raise ValueError("output_schema is required for structured output")

        tools = [{
            "name": tool_name,
            "description": "Output structured result",
            "input_schema": output_schema,
        }]

        response = self.chat(
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
        )
        try:
            return self._parse_tool_args(response)
        except ValueError as e:
            # LLM 结构化输出偶发坏 JSON/截断：盲重试一次（瞬态失败通常可恢复），仍失败则抛出
            logger.warning("structured output parse failed, retrying once: %s", e)
            response = self.chat(
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
            )
            return self._parse_tool_args(response)

    def _parse_tool_args(self, response: LLMResponse) -> dict[str, Any]:
        """从响应中提取工具调用参数并解析 JSON（失败时附带出错位置片段便于诊断）。

        end_turn 兜底：模型未调用工具但正文含 JSON 时（如直接输出
        '{"ops": [...]}'），尽力解析正文，避免提取/整合整段失败退化。
        """
        tool_calls = [b for b in response.content if isinstance(b, ToolCallBlock)]
        if tool_calls:
            args = tool_calls[0].arguments or ""
            if not args:
                return {}
            try:
                return json.loads(args)
            except json.JSONDecodeError as e:
                repaired = _repair_truncated_json(args)
                if repaired is not None:
                    logger.warning("truncated structured output repaired near %r", args[max(0, e.pos - 60):e.pos + 60])
                    return repaired
                snippet = args[max(0, e.pos - 60):e.pos + 60]
                raise ValueError(f"Failed to parse structured output: {e} near {snippet!r}")

        text = "\n".join(b.text for b in response.content if isinstance(b, TextBlock))
        parsed = _extract_json_dict(text) if text.strip() else None
        if parsed is not None:
            return parsed
        raise ValueError(f"Expected tool call but got stop_reason={response.stop_reason}")

    def test_connection(self) -> tuple[bool, str]:
        """Test API connection with a minimal request.

        Returns:
            (success, message) tuple
        """
        try:
            response = self.chat(
                messages=[Message(role="user", content="Hi")],
                max_tokens=10,
            )
            return True, f"连接成功，模型: {self.model_name}"
        except Exception as e:
            error_msg = format_llm_error(e, self.config.base_url)
            return False, error_msg

    def close(self) -> None:
        """Close the client and release resources."""
        self.transport.close()


def create_llm_client(config: ModelConfig) -> LLMClient:
    """Factory function to create an LLMClient from ModelConfig.

    Auto-detects the provider based on interface_type:
    - "anthropic" → AnthropicAdapter
    - "openai"    → OpenAIAdapter

    Args:
        config: Model configuration

    Returns:
        Configured LLMClient

    Raises:
        ValueError: If interface_type is not supported
    """
    from core.llm.anthropic import AnthropicAdapter
    from core.llm.openai import OpenAIAdapter

    interface_type = config.interface_type.lower()

    if interface_type == "anthropic":
        adapter = AnthropicAdapter(config)
    elif interface_type == "openai":
        adapter = OpenAIAdapter(config)
    else:
        raise ValueError(f"Unsupported interface_type: {interface_type}")

    transport = HttpTransport()
    return LLMClient(adapter=adapter, transport=transport, config=config)
