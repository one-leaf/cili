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

        # Detect LiteLLM proxy
        adapter.detect_litellm(transport)

    def chat(
        self,
        messages: list[Message],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        session_id: str = "",
    ) -> LLMResponse:
        """Non-streaming LLM call.

        Args:
            messages: Conversation messages (list[Message])
            system: System prompt
            tools: Tool schemas
            max_tokens: Override default max_tokens
            session_id: Optional session ID for proxy routing

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
            stream=False,
            session_id=session_id,
        )
        headers = self.adapter.build_headers()
        url = self.adapter.api_url

        def do_request():
            status, resp_headers, data = self.transport.post(url, headers, body)
            if status >= 400:
                # Raise for retry logic
                resp = httpx.Response(status_code=status, request=httpx.Request("POST", url))
                resp._content = json.dumps(data).encode("utf-8")
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

        Returns:
            LLMResponse with assembled content blocks
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
            events = self.transport.stream(url, headers, body, stop_check=stop_check)
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

        self.transport.with_retry(
            do_stream,
            stop_check=stop_check,
        )

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

        tool_calls = [b for b in response.content if isinstance(b, ToolCallBlock)]
        if not tool_calls:
            raise ValueError(f"Expected tool call but got stop_reason={response.stop_reason}")

        # Parse arguments (they are raw JSON string)
        try:
            return json.loads(tool_calls[0].arguments) if tool_calls[0].arguments else {}
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse structured output: {e}")

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
