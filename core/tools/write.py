"""Write tool - create or overwrite files."""

from __future__ import annotations

import os

from core.fs_utils import atomic_write_text
from core.tools.base import Tool, ToolResult


class WriteTool(Tool):
    name = "write"
    description = (
        "Create a new file or overwrite an existing file with the given content. "
        "Automatically creates parent directories if they don't exist."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path for the file to write (relative or absolute).",
            },
            "content": {
                "type": "string",
                "description": "The content to write to the file.",
            },
        },
        "required": ["file_path", "content"],
    }

    MAX_RESULT_SIZE_CHARS = 100_000  # 工具结果上限

    def execute(self, file_path: str, content: str) -> ToolResult:
        file_path = self._resolve_path(file_path)

        # Remove surrogate characters that are invalid in UTF-8
        clean_content = self._clean_surrogates(content)

        try:
            # Atomic write（newline="" 禁用换行翻译，内容按原样写入，
            # \n 保持 LF，否则 Windows 文本模式会把 \n 写成 \r\n，破坏 .sh 等脚本）
            atomic_write_text(file_path, clean_content)
            lines = clean_content.count("\n") + (1 if clean_content and not clean_content.endswith("\n") else 0)
            size = os.path.getsize(file_path)
            result_text = f"Successfully wrote {file_path} ({lines} lines, {size} bytes)"
            return ToolResult(self.truncate_result(result_text, self.MAX_RESULT_SIZE_CHARS))
        except Exception as e:
            error_text = f"Error writing file: {e}"
            return ToolResult(self.truncate_result(error_text, self.MAX_RESULT_SIZE_CHARS), error=True)
