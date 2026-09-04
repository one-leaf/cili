"""Edit tool - precise text replacement in files."""

from __future__ import annotations

import os

from core.tools.shared.base import Tool, ToolResult


class EditTool(Tool):
    name = "edit"
    description = (
        "Make precise edits to a file by replacing exact text matches. "
        "Each edit specifies old_text (to find) and new_text (to replace with). "
        "By default, old_text must be unique in the file. "
        "Use replace_all to replace every occurrence, occurrence to target the Nth match, "
        "or line_hint to disambiguate when old_text appears multiple times."
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
                "description": "The exact text string to find and replace.",
            },
            "new_text": {
                "type": "string",
                "description": "The replacement text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "If true, replace ALL occurrences of old_text. Mutually exclusive with occurrence and line_hint.",
            },
            "occurrence": {
                "type": "integer",
                "description": "Replace only the Nth occurrence (1-indexed). Mutually exclusive with replace_all and line_hint.",
            },
            "line_hint": {
                "type": "integer",
                "description": "1-based line number to disambiguate when old_text appears multiple times. The match covering this line is replaced. Mutually exclusive with replace_all and occurrence.",
            },
        },
        "required": ["file_path", "old_text", "new_text"],
    }

    def execute(
        self,
        file_path: str,
        old_text: str,
        new_text: str,
        replace_all: bool | None = None,
        occurrence: int | None = None,
        line_hint: int | None = None,
    ) -> ToolResult:
        file_path = self._resolve_path(file_path)

        if not old_text:
            return ToolResult("Error: old_text must not be empty", error=True)

        # Check mutual exclusivity
        disambig = [bool(replace_all), occurrence is not None, line_hint is not None]
        if sum(disambig) > 1:
            return ToolResult(
                "Error: replace_all, occurrence, and line_hint are mutually exclusive. Use at most one.",
                error=True,
            )

        clean_old = self._clean_surrogates(old_text)
        clean_new = self._clean_surrogates(new_text)

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            count = content.count(clean_old)
            if count == 0:
                return ToolResult("Error: old_text not found in file.", error=True)

            if replace_all:
                content = content.replace(clean_old, clean_new)
            elif occurrence is not None:
                if occurrence < 1:
                    return ToolResult("Error: occurrence must be a positive integer (1-indexed).", error=True)
                if occurrence > count:
                    return ToolResult(
                        f"Error: occurrence={occurrence} exceeds total matches ({count}).",
                        error=True,
                    )
                content = self._replace_nth(content, clean_old, clean_new, occurrence)
            elif line_hint is not None:
                if line_hint < 1:
                    return ToolResult("Error: line_hint must be a positive integer (1-indexed).", error=True)
                content = self._replace_at_line(content, clean_old, clean_new, line_hint)
            else:
                # Default: require uniqueness
                if count > 1:
                    lines = content.split("\n")
                    match_lines = []
                    for i, line in enumerate(lines, 1):
                        if clean_old in line:
                            match_lines.append(i)
                    return ToolResult(
                        f"Error: old_text found {count} times (must be unique). "
                        f"Match lines: {match_lines}. "
                        f"Use occurrence=N, line_hint=N, or replace_all=true to disambiguate.",
                        error=True,
                    )
                content = content.replace(clean_old, clean_new, 1)

            # Atomic write: write to temp file first, then replace
            temp_path = file_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, file_path)

            result_text = f"Successfully edited {file_path}"
            return ToolResult(self.truncate_result(result_text, 100_000))
        except FileNotFoundError:
            return ToolResult(f"Error: file not found: {file_path}", error=True)
        except Exception as e:
            error_text = f"Error editing file: {e}"
            return ToolResult(self.truncate_result(error_text, 100_000), error=True)

    @staticmethod
    def _replace_nth(content: str, old: str, new: str, n: int) -> str:
        """Replace the Nth occurrence (1-indexed) of old with new in content."""
        idx = -1
        for _ in range(n):
            idx = content.find(old, idx + 1)
            if idx == -1:
                break
        if idx == -1:
            return content  # Should not happen if count was checked
        return content[:idx] + new + content[idx + len(old):]

    @staticmethod
    def _replace_at_line(content: str, old: str, new: str, line_hint: int) -> str:
        """Replace the occurrence of old that covers line_hint (1-indexed)."""
        lines = content.split("\n")
        if line_hint > len(lines):
            raise ValueError(f"line_hint={line_hint} exceeds file length ({len(lines)} lines)")

        # Find which line(s) contain old_text
        # Strategy: rebuild content line-by-line, find the occurrence covering line_hint
        # We need to find the occurrence whose character range includes the start of line_hint
        line_starts = []  # character offset of each line's start
        offset = 0
        for line in lines:
            line_starts.append(offset)
            offset += len(line) + 1  # +1 for the \n

        # Find all occurrences of old in content
        target_line_start = line_starts[line_hint - 1]
        target_line_end = line_starts[line_hint] if line_hint < len(lines) else len(content)

        occurrences = []
        idx = 0
        while True:
            idx = content.find(old, idx)
            if idx == -1:
                break
            occurrences.append(idx)
            idx += 1

        # Find the occurrence that overlaps with the target line
        for occ_start in occurrences:
            occ_end = occ_start + len(old)
            # Occurrence overlaps with line if it starts before line ends and ends after line starts
            if occ_start < target_line_end and occ_end > target_line_start:
                return content[:occ_start] + new + content[occ_start + len(old):]

        # If no overlap found, report the match lines
        match_lines = []
        for occ_start in occurrences:
            for li, ls in enumerate(line_starts):
                le = line_starts[li + 1] if li + 1 < len(line_starts) else len(content)
                if ls <= occ_start < le:
                    match_lines.append(li + 1)
                    break
        raise ValueError(
            f"old_text not found on line {line_hint}. Matches found on lines: {match_lines}"
        )
