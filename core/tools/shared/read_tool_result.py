"""Tool for reading compacted tool results from external storage."""

import re

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

    # 只允许安全字符，防止路径遍历
    _SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

    def execute(self, tool_use_id: str, **kwargs) -> ToolResult:
        # 校验 tool_use_id，防止路径遍历
        if not self._SAFE_ID_PATTERN.match(tool_use_id):
            return ToolResult(f"Error: invalid tool_use_id format: {tool_use_id}", error=True)

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
