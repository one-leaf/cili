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

from core.config import ModelConfig
from core.llm.anthropic import AnthropicAdapter
from core.llm.assembler import BlockAssembler
from core.llm.client import LLMClient
from core.llm.errors import LLMErrorInfo, classify_llm_error
from core.llm.openai import OpenAIAdapter
from core.llm.transport import _RETRY_STATUS_CODES
from core.llm.types import (
    ContentBlock,
    ImageBlock,
    # Response
    LLMResponse,
    # Message and usage
    Message,
    ReasoningBlock,
    # Streaming
    StreamChunk,
    # Content block types
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    UsageData,
    block_from_dict,
    # Content block helpers
    block_to_dict,
    blocks_from_dicts,
    blocks_to_dicts,
    # Error formatting
    format_llm_error,
)


# Lazy factory function
def create_llm_client(config: ModelConfig) -> LLMClient:
    """Create an LLMClient from ModelConfig (factory function).

    Auto-detects the provider based on config.interface_type:
    - "anthropic" → AnthropicAdapter
    - "openai"    → OpenAIAdapter
    """
    from core.llm.client import create_llm_client as _create
    return _create(config)

__all__ = [
    # Content block types
    "_RETRY_STATUS_CODES",
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
    # Message and usage
    "Message",
    "UsageData",
    # Streaming
    "StreamChunk",
    # Response
    "LLMResponse",
    # Error formatting / taxonomy
    "format_llm_error",
    "classify_llm_error",
    "LLMErrorInfo",
    # Assembler
    "BlockAssembler",
    # Client
    "LLMClient",
    "AnthropicAdapter",
    "OpenAIAdapter",
    "create_llm_client",
]
