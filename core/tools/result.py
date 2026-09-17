"""ToolResult — 工具执行结果（含新旧接口兼容层）。"""

from __future__ import annotations


class ToolResult:
    """Result of a tool execution.

    Supports both old and new interfaces for backward compatibility:

    New interface (recommended):
        blocks: list[ContentBlock] - typed content blocks
        is_error: bool - whether this is an error result
        meta: dict - optional structured metadata for UI
        completed: bool | None - placeholder lifecycle:
            None (default): normal tool result, no loop pause
            False: placeholder mode, agent loop exits (waiting for external input)
            True: placeholder completed (result written back)

    Old interface (backward compat):
        output: str - plain text output (converted to [TextBlock(text=output)])
        error: bool - alias for is_error
        content: list[dict] - legacy multimodal content (deprecated)
        wait_for_user: bool - deprecated alias for completed=False

    Examples:
        # Normal tool result
        ToolResult(output="some text")
        ToolResult(output="error", error=True)

        # Placeholder mode (ask_user, agent)
        ToolResult(output="执行中...", completed=False, meta={"exec_id": "..."})
    """

    def __init__(
        self,
        output: str = "",
        error: bool = False,
        content: list[dict] | None = None,
        # New interface
        blocks: list | None = None,
        is_error: bool = False,
        meta: dict | None = None,
        completed: bool | None = None,
        # Deprecated alias (backward compat)
        wait_for_user: bool = False,
    ):
        # Normalize error flags
        self.is_error = is_error or error

        # Handle both old (wait_for_user) and new (completed) interface
        # wait_for_user=True → completed=False (not yet done, loop should exit)
        if completed is not None:
            self.completed = completed
        elif wait_for_user:
            self.completed = False
        else:
            self.completed = None

        # Convert old interface to new interface
        if blocks is not None:
            self.blocks = blocks
        elif content:
            from core.llm.types import block_from_dict
            self.blocks = [block_from_dict(b) for b in content if isinstance(b, dict)]
            # If output also provided (old-style multimodal), prepend as text description
            if output:
                from core.llm.types import TextBlock
                self.blocks.insert(0, TextBlock(text=output))
        elif output:
            from core.llm.types import TextBlock
            self.blocks = [TextBlock(text=output)]
        else:
            self.blocks = []

        # Store meta
        self.meta = meta

    @property
    def output(self) -> str:
        """Backward compat: extract text from blocks."""
        from core.llm.types import TextBlock
        return "".join(
            block.text for block in self.blocks
            if isinstance(block, TextBlock)
        )

    @property
    def error(self) -> bool:
        """Backward compat alias for is_error."""
        return self.is_error

    @property
    def wait_for_user(self) -> bool:
        """Backward compat: completed=False means wait_for_user=True."""
        return self.completed is False

    def __repr__(self) -> str:
        return f"ToolResult(blocks={self.blocks!r}, is_error={self.is_error})"
