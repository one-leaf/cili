"""Edit tool - precise text replacement in files."""

from __future__ import annotations

import os

from core.tools.shared.base import Tool, ToolResult


class EditTool(Tool):
    name = "edit"
    description = (
        "Make precise edits to a file by replacing exact text matches. "
        "Each edit specifies old_text (to find) and new_text (to replace with). "
        "old_text must be unique in the file. "
        "For multiple disjoint edits, pass parallel arrays of old_texts and new_texts."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to edit (relative or absolute).",
            },
            "old_text": {
                "type": "string",
                "description": "The exact text string to find and replace. Must be unique in the file.",
            },
            "new_text": {
                "type": "string",
                "description": "The replacement text.",
            },
        },
        "required": ["file_path", "old_text", "new_text"],
    }

    def execute(self, file_path: str, old_text: str, new_text: str) -> ToolResult:
        file_path = self._resolve_path(file_path)

        if not old_text:
            return ToolResult("Error: old_text must not be empty", error=True)

        clean_old = self._clean_surrogates(old_text)
        clean_new = self._clean_surrogates(new_text)

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            count = content.count(clean_old)
            if count == 0:
                return ToolResult("Error: old_text not found in file.", error=True)
            if count > 1:
                return ToolResult(f"Error: old_text found {count} times (must be unique).", error=True)

            content = content.replace(clean_old, clean_new, 1)

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            result_text = f"Successfully edited {file_path}"
            return ToolResult(self.truncate_result(result_text, 100_000))
        except FileNotFoundError:
            return ToolResult(f"Error: file not found: {file_path}", error=True)
        except Exception as e:
            error_text = f"Error editing file: {e}"
            return ToolResult(self.truncate_result(error_text, 100_000), error=True)
