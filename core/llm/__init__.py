"""LLM subsystem - unified type system and client.

This package provides:
- Typed content blocks (TextBlock, ReasoningBlock, ToolCallBlock, etc.)
- Provider-neutral message format (Message)
- Token usage tracking (UsageData)
- Unified streaming protocol (StreamChunk)
- LLM client with retry logic (LLMClient)
- Provider adapters (AnthropicAdapter, OpenAIAdapter)

Usage:
    from core.llm import (
        Message, ContentBlock, TextBlock, ToolCallBlock,
        UsageData, StreamChunk, LLMResponse,
        LLMClient, create_llm_client,
    )
"""

from __future__ import annotations

from core.llm.types import (
    # Content block types
    TextBlock,
    ReasoningBlock,
    ImageBlock,
    ToolCallBlock,
    ToolResultBlock,
    ContentBlock,
    # Content block helpers
    block_to_dict,
    block_from_dict,
    blocks_to_dicts,
    blocks_from_dicts,
    extract_text_from_blocks,
    extract_tool_calls_from_blocks,
    has_tool_calls,
    # Message and usage
    Message,
    UsageData,
    # Streaming
    StreamChunk,
    # Response
    LLMResponse,
    # Error formatting
    format_llm_error,
)

from core.llm.assembler import BlockAssembler
from core.llm.client import LLMClient
from core.llm.anthropic import AnthropicAdapter
from core.llm.openai import OpenAIAdapter
from core.llm.transport import _RETRY_STATUS_CODES


# Lazy factory function
def create_llm_client(config):
    """Create an LLMClient from ModelConfig (factory function).

    Auto-detects the provider based on config.interface_type:
    - "anthropic" → AnthropicAdapter
    - "openai"    → OpenAIAdapter
    """
    from core.llm.client import create_llm_client as _create
    return _create(config)

__all__ = [
    # Content block types
    "TextBlock",
    "ReasoningBlock",
    "ImageBlock",
    "ToolCallBlock",
    "ToolResultBlock",
    "ContentBlock",
    # Content block helpers
    "block_to_dict",
    "block_from_dict",
    "blocks_to_dicts",
    "blocks_from_dicts",
    "extract_text_from_blocks",
    "extract_tool_calls_from_blocks",
    "has_tool_calls",
    # Message and usage
    "Message",
    "UsageData",
    # Streaming
    "StreamChunk",
    # Response
    "LLMResponse",
    # Error formatting
    "format_llm_error",
    # Assembler
    "BlockAssembler",
    # Client
    "LLMClient",
    "AnthropicAdapter",
    "OpenAIAdapter",
    "create_llm_client",
]
