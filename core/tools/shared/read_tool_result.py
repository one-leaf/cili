"""Tool for reading compacted tool results from external storage."""

from core.tools.shared.base import Tool, ToolResult


class ReadToolResultTool(Tool):
    """Retrieve compacted tool output from external storage."""

    name = "read_tool_result"
    description = "Retrieve a compacted tool result by its tool_use_id. Use when you see '[Compacted: use `read_tool_result` tool ...]' in a tool result."

    parameters = {
        "type": "object",
        "properties": {
            "tool_use_id": {
                "type": "string",
                "description": "The tool_use_id from the compacted result (e.g., 'toolu_01ABC123')",
            },
        },
        "required": ["tool_use_id"],
    }

    def execute(self, tool_use_id: str, **kwargs) -> ToolResult:
        if not self.session_manager:
            return ToolResult("Error: session manager not available", error=True)

        session_dir = self.session_manager.session_dir

        # Try main session directory first
        filename = f"{tool_use_id}.txt"
        file_path = session_dir / filename

        if not file_path.exists():
            # Try subagent execution directories
            for exec_dir in session_dir.glob("exec_*"):
                candidate = exec_dir / filename
                if candidate.exists():
                    file_path = candidate
                    break
            else:
                return ToolResult(f"Tool result file not found: {filename}", error=True)

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            if not content:
                content = "(empty output)"
            return ToolResult(content)
        except Exception as e:
            return ToolResult(f"Error reading file: {e}", error=True)
