"""BlockAssembler - accumulates StreamChunks into ContentBlocks.

The assembler receives a stream of neutral StreamChunk events (produced by
the adapter's translate_stream()) and builds up a list of ContentBlock objects.

Usage:
    assembler = BlockAssembler()
    for chunk in adapter.translate_stream(events):
        assembler.push(chunk)
    blocks = assembler.blocks  # list[ContentBlock]
    stop_reason = assembler.stop_reason
    usage = assembler.usage
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.llm.types import (
    ContentBlock,
    ImageBlock,
    ReasoningBlock,
    StreamChunk,
    TextBlock,
    ToolCallBlock,
    UsageData,
)


@dataclass
class BlockAssembler:
    """Accumulates StreamChunks into ContentBlocks.

    Thread-unsafe — designed for single-threaded use within a single
    LLM call cycle.
    """

    _blocks: list[ContentBlock] = field(default_factory=list)
    _block_map: dict[int, ContentBlock] = field(default_factory=dict)
    _stop_reason: str = ""
    _usage: UsageData = field(default_factory=UsageData)
    _finished: bool = False

    def push(self, chunk: StreamChunk) -> None:
        """Process a single StreamChunk.

        Args:
            chunk: StreamChunk from adapter.translate_stream()
        """
        ctype = chunk.type

        if ctype == "block_start":
            block_type = chunk.data.get("block_type", "")
            new_block = _create_block(block_type)
            if new_block is not None:
                self._blocks.append(new_block)
                self._block_map[chunk.index] = new_block

        elif ctype == "block_end":
            # Block is complete — nothing to do, block stays in list
            pass

        elif ctype == "text_delta":
            block = self._block_map.get(chunk.index)
            if isinstance(block, TextBlock):
                block.text += chunk.data.get("text", "")

        elif ctype == "reasoning_delta":
            block = self._block_map.get(chunk.index)
            if isinstance(block, ReasoningBlock):
                block.text += chunk.data.get("text", "")

        elif ctype == "signature_delta":
            block = self._block_map.get(chunk.index)
            if isinstance(block, ReasoningBlock):
                block.signature = chunk.data.get("signature", "")

        elif ctype == "tool_call_delta":
            block = self._block_map.get(chunk.index)
            if isinstance(block, ToolCallBlock):
                if "id" in chunk.data and chunk.data["id"]:
                    block.id += chunk.data["id"]
                if "name" in chunk.data and chunk.data["name"]:
                    block.name += chunk.data["name"]
                if "arguments" in chunk.data and chunk.data["arguments"]:
                    block.arguments += chunk.data["arguments"]

        elif ctype == "usage":
            u = chunk.data.get("usage", UsageData())
            if isinstance(u, dict):
                u = UsageData.from_dict(u)
            self._usage = u

        elif ctype == "finish":
            self._stop_reason = chunk.data.get("stop_reason", "")
            self._finished = True

    @property
    def blocks(self) -> list[ContentBlock]:
        """Return accumulated content blocks."""
        return list(self._blocks)

    @property
    def stop_reason(self) -> str:
        """Return the stop reason (end_turn, tool_use, max_tokens, etc.)."""
        return self._stop_reason

    @property
    def usage(self) -> UsageData:
        """Return accumulated usage data."""
        return self._usage

    @property
    def finished(self) -> bool:
        """Return whether a finish chunk was received."""
        return self._finished

    def get_text(self) -> str:
        """Return concatenated text from all TextBlocks."""
        parts = []
        for block in self._blocks:
            if isinstance(block, TextBlock):
                parts.append(block.text)
        return "".join(parts)

    def get_tool_calls(self) -> list[ToolCallBlock]:
        """Return all ToolCallBlocks."""
        return [b for b in self._blocks if isinstance(b, ToolCallBlock)]

    def reset(self) -> None:
        """Reset assembler state for reuse."""
        self._blocks.clear()
        self._block_map.clear()
        self._stop_reason = ""
        self._usage = UsageData()
        self._finished = False


def _create_block(block_type: str) -> ContentBlock | None:
    """Create an empty ContentBlock from a block type string."""
    if block_type == "text":
        return TextBlock()
    elif block_type == "reasoning":
        return ReasoningBlock()
    elif block_type == "tool_call":
        return ToolCallBlock()
    elif block_type == "image":
        return ImageBlock()
    return None
