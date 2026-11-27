"""Write tool - create or overwrite files."""

from __future__ import annotations

import os

from core.fs_utils import UTF8_BOM_CHAR, atomic_write_text, is_powershell_script
from core.security.path_target import OP_WRITE, PathTarget
from core.text_sanitize import clean_surrogates
from core.tool_limits import WRITE_RESULT_MAX_CHARS
from core.tools.base import Tool, ToolResult


class WriteTool(Tool):
    mutates_workspace = True
    name = "write"
    prompt_guidelines = (
        "Use write only for new files or complete rewrites; prefer edit for existing files.",
    )
    description = (
        "Create a new file or overwrite an existing file with the given content. "
        "Automatically creates parent directories if they don't exist. "
        "Keep each call's content moderate in size (roughly under 8000 characters). "
        "For longer content, write the first part, then append the rest with the "
        "edit tool — a single oversized call risks being cut off mid-generation. "
        "PowerShell script files (.ps1/.psm1/.psd1) are written as UTF-8 with BOM "
        "so Windows PowerShell 5.1 parses non-ASCII content correctly."
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

    MAX_RESULT_SIZE_CHARS = WRITE_RESULT_MAX_CHARS  # 工具结果上限

    def execute(self, file_path: str, content: str) -> ToolResult:
        escape = self._relative_escape_guard(file_path)
        if escape is not None:
            return escape
        policy = self._path_policy()
        resolved = policy.resolve(file_path)
        gate = self._path_gate([PathTarget(OP_WRITE, file_path, "write", resolved=resolved)])
        if gate:
            return gate
        file_path = resolved

        workflow_guard = self._workflow_definition_guard(file_path)
        if workflow_guard:
            return workflow_guard

        # Remove surrogate characters that are invalid in UTF-8
        clean_content = clean_surrogates(content)

        # Windows PowerShell 5.1 只有看到 UTF-8 BOM 才按 UTF-8 解析脚本。
        # 去掉内容自带的前导 BOM，避免 utf-8-sig 再写一次造成双 BOM。
        encoding = "utf-8"
        if is_powershell_script(file_path):
            encoding = "utf-8-sig"
            clean_content = clean_content.lstrip(UTF8_BOM_CHAR)

        try:
            # Atomic write（newline="" 禁用换行翻译，内容按原样写入，
            # \n 保持 LF，否则 Windows 文本模式会把 \n 写成 \r\n，破坏 .sh 等脚本）
            atomic_write_text(file_path, clean_content, encoding=encoding)
            lines = clean_content.count("\n") + (1 if clean_content and not clean_content.endswith("\n") else 0)
            size = os.path.getsize(file_path)
            result_text = f"Successfully wrote {file_path} ({lines} lines, {size} bytes)"
            return ToolResult(self.truncate_result(result_text, self.MAX_RESULT_SIZE_CHARS))
        except Exception as e:
            error_text = f"Error writing file: {e}"
            return ToolResult(self.truncate_result(error_text, self.MAX_RESULT_SIZE_CHARS), error=True)
