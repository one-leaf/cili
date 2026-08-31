"""Temp tool - 临时文件和目录管理。

在 workspace 的 .cili/tmp/{session_id}/ 下创建临时文件和目录。
用于存放下载内容、中间结果等临时数据。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from core.config import DATA_DIR, get_workspace_data_dir
from core.tools.shared.base import Tool, ToolResult


class TempTool(Tool):
    """临时文件和目录管理工具。"""

    name = "temp"
    description = """Manage temporary files and directories for the current session.

Temporary files are stored in the workspace's .cili/tmp/{session_id}/ directory.
Use this for intermediate results, downloads, or any data that doesn't need to persist.

Available actions:
- create_file: Create a temporary file (returns absolute path)
- create_dir: Create a temporary directory (returns absolute path)
- list: List all temp files/dirs for current session
- cleanup: Delete entire temp directory for current session

Examples:
- {"action": "create_file", "name": "data.json", "content": "{...}"}
- {"action": "create_dir", "name": "downloads"}
- {"action": "list"}
- {"action": "cleanup"}
"""
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create_file", "create_dir", "list", "cleanup"],
                "description": "Action to perform.",
            },
            "name": {
                "type": "string",
                "description": "File or directory name (required for create_file/create_dir).",
            },
            "content": {
                "type": "string",
                "description": "File content (optional for create_file, defaults to empty).",
            },
        },
        "required": ["action"],
    }

    def _get_temp_dir(self) -> Path:
        """获取当前 session 的临时目录。"""
        base = get_workspace_data_dir(self.workspace_uuid)

        session_id = "no-session"
        if self.session_manager and hasattr(self.session_manager, "session_id"):
            session_id = self.session_manager.session_id or "no-session"

        temp_dir = base / ".cili" / "tmp" / session_id
        temp_dir.mkdir(parents=True, exist_ok=True)
        return temp_dir

    def execute(
        self,
        action: str,
        name: str | None = None,
        content: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """执行临时文件/目录操作。"""
        if action == "create_file":
            if not name:
                return ToolResult("Error: 'name' is required for create_file", error=True)
            temp_dir = self._get_temp_dir()
            file_path = temp_dir / name
            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content or "", encoding="utf-8")
                return ToolResult(f"Created temp file: {file_path}")
            except Exception as e:
                return ToolResult(f"Error creating file: {e}", error=True)

        elif action == "create_dir":
            if not name:
                return ToolResult("Error: 'name' is required for create_dir", error=True)
            temp_dir = self._get_temp_dir()
            dir_path = temp_dir / name
            try:
                dir_path.mkdir(parents=True, exist_ok=True)
                return ToolResult(f"Created temp directory: {dir_path}")
            except Exception as e:
                return ToolResult(f"Error creating directory: {e}", error=True)

        elif action == "list":
            temp_dir = self._get_temp_dir()
            try:
                items = list(temp_dir.iterdir())
                if not items:
                    return ToolResult(f"No temp files in {temp_dir}")

                lines = [f"Temp directory: {temp_dir}", ""]
                for item in sorted(items, key=lambda p: p.name):
                    item_type = "DIR" if item.is_dir() else "FILE"
                    size = item.stat().st_size if item.is_file() else "-"
                    lines.append(f"  [{item_type}] {item.name}  ({size} bytes)")
                return ToolResult("\n".join(lines))
            except Exception as e:
                return ToolResult(f"Error listing files: {e}", error=True)

        elif action == "cleanup":
            temp_dir = self._get_temp_dir()
            try:
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)
                    return ToolResult(f"Cleaned up temp directory: {temp_dir}")
                else:
                    return ToolResult("No temp directory to clean up")
            except Exception as e:
                return ToolResult(f"Error cleaning up: {e}", error=True)

        else:
            return ToolResult(f"Error: unknown action '{action}'", error=True)
