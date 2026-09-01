"""Type definitions for the LLM subsystem.

This module defines the core types used throughout the LLM layer:
- ContentBlock types: atomic units of message content
- Message: unified message format for storage and API
- UsageData: token usage tracking
- StreamChunk: unified streaming protocol
- LLMResponse: response from LLM calls

Design inspired by pi-main and DeepSeek Harness:
- Provider-neutral message format (Message)
- Typed content blocks (ContentBlock)
- Unified streaming protocol (StreamChunk)
- Adapter handles provider-specific serialization
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ========== Content Block Types ==========


@dataclass
class TextBlock:
    """Plain text visible to the end user."""

    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for serialization."""
        return {"type": "text", "text": self.text}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TextBlock:
        """Create from dict."""
        return cls(text=data.get("text", ""))


@dataclass
class ReasoningBlock:
    """Model reasoning/thinking content, distinct from visible text."""

    text: str = ""
    # Optional: opaque provider-specific replay metadata (e.g., Anthropic signature)
    signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for serialization."""
        result: dict[str, Any] = {"type": "reasoning", "text": self.text}
        if self.signature:
            result["signature"] = self.signature
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReasoningBlock:
        """Create from dict."""
        return cls(
            text=data.get("text", ""),
            signature=data.get("signature"),
        )


@dataclass
class ImageBlock:
    """Image content block.

    Supports inline base64 data for simplicity.
    Future: could add ImageRef for attachment store integration.
    """

    data: str = ""  # base64 encoded
    mime_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for serialization (Anthropic format)."""
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.mime_type,
                "data": self.data,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImageBlock:
        """Create from dict."""
        source = data.get("source", {})
        return cls(
            data=source.get("data", ""),
            mime_type=source.get("media_type", ""),
        )


@dataclass
class ToolCallBlock:
    """A tool invocation requested by the model.

    IMPORTANT: arguments is RAW JSON STRING, not parsed dict.
    Parsing happens at tool execution layer via parse_arguments().
    This preserves fidelity and avoids parse errors during streaming.
    """

    id: str = ""
    name: str = ""
    arguments: str = ""  # Raw JSON string

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for serialization."""
        return {
            "type": "tool_call",
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCallBlock:
        """Create from dict.

        Supports both new format (arguments: str) and stored format (input: dict).
        """
        # New format: arguments is a string
        arguments = data.get("arguments", "")
        # Stored format: input is a dict (Anthropic API format)
        if not arguments and "input" in data:
            inp = data["input"]
            if isinstance(inp, dict):
                arguments = json.dumps(inp, ensure_ascii=False)
            elif isinstance(inp, str):
                arguments = inp
            else:
                arguments = json.dumps(inp) if inp else ""
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False) if arguments else ""

        return cls(
            id=data.get("id", "") or data.get("tool_use_id", ""),
            name=data.get("name", ""),
            arguments=arguments,
        )

    def parse_arguments(self) -> dict[str, Any]:
        """Parse arguments JSON string to dict.

        Call this at tool execution time, not during streaming.
        """
        if not self.arguments:
            return {}
        try:
            return json.loads(self.arguments)
        except json.JSONDecodeError:
            return {"_raw": self.arguments, "_parse_error": True}


@dataclass
class ToolResultBlock:
    """The result of a tool invocation.

    This is both a content block AND the content of a tool result message.

    Extended fields for SubAgent tracking:
    - exec_id: SubAgent execution ID
    - iterations: number of iterations
    - message_count: number of messages
    - duration_seconds: execution duration
    """

    tool_call_id: str = ""
    content: str | list[dict] = ""  # str for plain text, list[dict] for multimodal (text + image blocks)
    is_error: bool = False

    # SubAgent extension fields (optional)
    exec_id: str = ""
    iterations: int = 0
    message_count: int = 0
    duration_seconds: float = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for serialization."""
        result: dict[str, Any] = {
            "type": "tool_result",
            "tool_call_id": self.tool_call_id,
            "content": self.content,
            "is_error": self.is_error,
        }
        # Include SubAgent fields if present
        if self.exec_id:
            result["exec_id"] = self.exec_id
        if self.iterations:
            result["iterations"] = self.iterations
        if self.message_count:
            result["message_count"] = self.message_count
        if self.duration_seconds:
            result["duration_seconds"] = self.duration_seconds
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolResultBlock:
        """Create from dict."""
        content = data.get("content", "")
        # Keep list content as-is for multimodal support (text + image blocks)
        # Adapter layer handles conversion to provider-specific format

        return cls(
            tool_call_id=data.get("tool_call_id", data.get("tool_use_id", "")),
            content=content,
            is_error=data.get("is_error", False),
            exec_id=data.get("exec_id", ""),
            iterations=data.get("iterations", 0),
            message_count=data.get("message_count", 0),
            duration_seconds=data.get("duration_seconds", 0),
        )


# ========== Content Block Union ==========


# Type alias for any content block
ContentBlock = TextBlock | ReasoningBlock | ImageBlock | ToolCallBlock | ToolResultBlock


def block_to_dict(block: ContentBlock) -> dict[str, Any]:
    """Convert any content block to dict for serialization."""
    return block.to_dict()


def block_from_dict(data: dict[str, Any]) -> ContentBlock:
    """Create content block from dict.

    Dispatches based on 'type' field.
    Supports both new format (tool_call) and legacy (tool_use).
    """
    block_type = data.get("type", "")
    if block_type == "text":
        return TextBlock.from_dict(data)
    elif block_type == "reasoning":
        return ReasoningBlock.from_dict(data)
    elif block_type == "image":
        return ImageBlock.from_dict(data)
    elif block_type in ("tool_use", "tool_call"):
        return ToolCallBlock.from_dict(data)
    elif block_type == "tool_result":
        return ToolResultBlock.from_dict(data)
    else:
        # Unknown type - wrap as TextBlock
        return TextBlock(text=f"[unknown block type: {block_type}]")


def blocks_to_dicts(blocks: list[ContentBlock]) -> list[dict[str, Any]]:
    """Convert list of content blocks to list of dicts."""
    return [block.to_dict() for block in blocks]


def blocks_from_dicts(data_list: list[dict[str, Any]]) -> list[ContentBlock]:
    """Create list of content blocks from list of dicts."""
    return [block_from_dict(data) for data in data_list]


# ========== Usage Data ==========


@dataclass
class UsageData:
    """Token usage for one LLM call.

    Normalized across providers (Anthropic/OpenAI use different field names).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        """Convert to dict for serialization."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageData:
        """Create from dict.

        Supports both Anthropic and OpenAI field names.
        """
        return cls(
            input_tokens=data.get("input_tokens", data.get("prompt_tokens", 0)),
            output_tokens=data.get("output_tokens", data.get("completion_tokens", 0)),
            cache_read_tokens=data.get(
                "cache_read_tokens",
                data.get("cache_read_input_tokens", 0)
            ),
            cache_write_tokens=data.get(
                "cache_write_tokens",
                data.get("cache_creation_input_tokens", 0)
            ),
        )

    @classmethod
    def from_anthropic(cls, data: dict[str, Any]) -> UsageData:
        """Create from Anthropic usage format."""
        return cls(
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cache_read_tokens=data.get("cache_read_input_tokens", 0),
            cache_write_tokens=data.get("cache_creation_input_tokens", 0),
        )

    @classmethod
    def from_openai(cls, data: dict[str, Any]) -> UsageData:
        """Create from OpenAI usage format."""
        return cls(
            input_tokens=data.get("prompt_tokens", 0),
            output_tokens=data.get("completion_tokens", 0),
            cache_read_tokens=data.get("prompt_tokens_details", {}).get("cached_tokens", 0) if data.get("prompt_tokens_details") else 0,
            cache_write_tokens=0,  # OpenAI doesn't report cache write
        )


# ========== Message ==========


@dataclass
class Message:
    """Provider-neutral message format.

    This is the unified message type used for:
    - Session storage
    - API communication (Adapter handles serialization)
    - Agent message history

    Role values:
    - "user": human input
    - "assistant": model output
    - "tool": tool execution result (separate from user message)

    Compression fields (message-level, aligned with pi-main/harness):
    - compacted: tool results compressed, send placeholder to LLM
    - invalidated: message invalid, exclude from filtering
    """

    role: str  # "user" | "assistant" | "tool"
    content: str | list[ContentBlock]

    # Optional metadata (assistant messages)
    provider: str = ""
    model: str = ""
    usage: UsageData | None = None
    stop_reason: str = ""

    # Compression fields (message-level)
    compacted: bool = False  # Tool results compressed, send placeholder to LLM
    invalidated: bool = False  # Message invalid, exclude from filtering

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for serialization."""
        result: dict[str, Any] = {
            "role": self.role,
        }

        # Content: string or list of blocks
        if isinstance(self.content, str):
            result["content"] = self.content
        else:
            result["content"] = blocks_to_dicts(self.content)

        # Optional metadata
        if self.provider:
            result["provider"] = self.provider
        if self.model:
            result["model"] = self.model
        if self.usage:
            result["usage"] = self.usage.to_dict()
        if self.stop_reason:
            result["stop_reason"] = self.stop_reason

        # Compression fields (only if True, to save space)
        if self.compacted:
            result["compacted"] = True
        if self.invalidated:
            result["invalidated"] = True

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        """Create from dict."""
        content = data.get("content", "")

        # Parse content: string or list of blocks
        if isinstance(content, list):
            content = blocks_from_dicts(content)

        # Parse usage
        usage = None
        if "usage" in data and data["usage"]:
            usage = UsageData.from_dict(data["usage"])

        return cls(
            role=data.get("role", "user"),
            content=content,
            provider=data.get("provider", ""),
            model=data.get("model", ""),
            usage=usage,
            stop_reason=data.get("stop_reason", ""),
            compacted=data.get("compacted", False),
            invalidated=data.get("invalidated", False),
        )


# ========== Stream Chunk ==========


@dataclass
class StreamChunk:
    """Unified streaming protocol.

    Adapters translate provider-specific SSE events into neutral StreamChunks.
    BlockAssembler accumulates chunks into ContentBlocks.

    Chunk types:
    - block_start: new content block begins (index, block_type)
    - text_delta: text content delta (index, text)
    - reasoning_delta: reasoning content delta (index, text)
    - tool_call_delta: tool call delta (index, id?, name?, arguments?)
    - block_end: content block ends (index)
    - usage: token usage update (usage)
    - finish: stream finished (stop_reason)
    """

    type: str  # chunk type
    index: int = 0  # block index
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def block_start(cls, index: int, block_type: str) -> StreamChunk:
        """Create block_start chunk."""
        return cls(type="block_start", index=index, data={"block_type": block_type})

    @classmethod
    def text_delta(cls, index: int, text: str) -> StreamChunk:
        """Create text_delta chunk."""
        return cls(type="text_delta", index=index, data={"text": text})

    @classmethod
    def reasoning_delta(cls, index: int, text: str) -> StreamChunk:
        """Create reasoning_delta chunk."""
        return cls(type="reasoning_delta", index=index, data={"text": text})

    @classmethod
    def tool_call_delta(
        cls,
        index: int,
        id: str | None = None,
        name: str | None = None,
        arguments: str | None = None,
    ) -> StreamChunk:
        """Create tool_call_delta chunk."""
        data: dict[str, Any] = {}
        if id is not None:
            data["id"] = id
        if name is not None:
            data["name"] = name
        if arguments is not None:
            data["arguments"] = arguments
        return cls(type="tool_call_delta", index=index, data=data)

    @classmethod
    def block_end(cls, index: int) -> StreamChunk:
        """Create block_end chunk."""
        return cls(type="block_end", index=index)

    @classmethod
    def usage_chunk(cls, usage: UsageData) -> StreamChunk:
        """Create usage chunk."""
        return cls(type="usage", data={"usage": usage})

    @classmethod
    def finish_chunk(cls, stop_reason: str) -> StreamChunk:
        """Create finish chunk."""
        return cls(type="finish", data={"stop_reason": stop_reason})


# ========== LLM Response ==========


@dataclass
class LLMResponse:
    """Normalized response from LLM.

    Attributes:
        content: List of typed content blocks
        stop_reason: Why the model stopped (end_turn, tool_use, length, etc.)
        usage: Token usage
        headers: Response headers (for proxy metadata)
    """

    content: list[ContentBlock]
    stop_reason: str = ""
    usage: UsageData = field(default_factory=UsageData)
    headers: dict[str, str] = field(default_factory=dict)

    def get_text(self) -> str:
        """Extract all text content from blocks."""
        parts = []
        for block in self.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
        return "".join(parts)

    def get_tool_calls(self) -> list[ToolCallBlock]:
        """Extract all tool call blocks."""
        return [b for b in self.content if isinstance(b, ToolCallBlock)]

    def has_tool_calls(self) -> bool:
        """Check if response contains any tool calls."""
        return any(isinstance(b, ToolCallBlock) for b in self.content)

    def content_as_dicts(self) -> list[dict[str, Any]]:
        """Convert content blocks to dicts for serialization."""
        return blocks_to_dicts(self.content)


# ========== Helper Functions ==========


def extract_text_from_blocks(blocks: list[ContentBlock]) -> str:
    """Extract all text content from blocks."""
    parts = []
    for block in blocks:
        if isinstance(block, TextBlock):
            parts.append(block.text)
    return "".join(parts)


def extract_tool_calls_from_blocks(blocks: list[ContentBlock]) -> list[ToolCallBlock]:
    """Extract all tool call blocks from content."""
    return [block for block in blocks if isinstance(block, ToolCallBlock)]


def has_tool_calls(blocks: list[ContentBlock]) -> bool:
    """Check if content contains any tool calls."""
    return any(isinstance(block, ToolCallBlock) for block in blocks)


def format_llm_error(e: Exception, base_url: str = "") -> str:
    """Format an LLM API exception into a short, actionable Chinese message.

    Shared by root_agent.py and sub_agent.py to avoid duplicating the
    isinstance chain.
    """
    import httpx

    if isinstance(e, httpx.ConnectTimeout):
        return f"LLM 连接超时，请检查网络或 API 地址是否正确 ({base_url})"
    if isinstance(e, httpx.ReadTimeout):
        return f"LLM 读取超时，服务器响应过慢 ({base_url})"
    if isinstance(e, httpx.ConnectError):
        return f"LLM 连接失败: {e}，请检查 API 地址和网络"
    if isinstance(e, httpx.NetworkError):
        return f"LLM 网络错误: {e}"
    if isinstance(e, httpx.TimeoutException):
        return f"LLM 请求超时，请检查网络连接 ({base_url})"
    if isinstance(e, httpx.HTTPStatusError):
        return f"LLM 错误 {e.response.status_code}: {e.response.text[:200]}"
    return f"LLM 请求失败: {e}"
