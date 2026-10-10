"""Glob tool - find files by glob pattern or file type.

Uses Python's pathlib.glob for pattern matching, respecting ignore dirs.
Sorted by modification time (newest first), supports pagination with offset/limit.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from core.tools.base import Tool, ToolResult
from core.tools.file_types import TYPE_EXTENSIONS


class GlobTool(Tool):
    name = "glob"
    concurrency_safe = True
    description = (
        "- Fast file pattern matching tool that works with any codebase size\n"
        "- Supports glob patterns like \"**/*.js\" or \"src/**/*.ts\"\n"
        "- Returns matching file paths sorted by modification time\n"
        "- Use this tool when you need to find files by name patterns\n"
        "- When you are doing an open ended search that may require multiple rounds of globbing and grepping, use the Agent tool instead"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The glob pattern to match files against (e.g. '**/*.js', 'src/**/*.ts').",
            },
            "path": {
                "type": "string",
                "description": "The directory to search in. If not specified, the current working directory will be used. IMPORTANT: Omit this field to use the default directory. DO NOT enter \"undefined\" or \"null\" - simply omit it for the default behavior. Must be a valid directory path if provided.",
            },
            "file_type": {
                "type": "string",
                "description": "File type shortcut: 'py', 'js', 'ts', 'md', 'json', 'yaml', 'html', 'css', 'go', 'rust', 'java', 'kotlin', 'scala', 'sh', 'txt', 'xml', 'svg', 'sql', 'c', 'cpp'. Overrides pattern.",
            },
            "head_limit": {
                "type": "integer",
                "description": "Limit output to first N results, equivalent to \"| head -N\". Defaults to 100 when unspecified. Pass 0 for unlimited.",
            },
            "offset": {
                "type": "integer",
                "description": "Skip first N results before applying head_limit, equivalent to \"| tail -n +N | head -N\". Defaults to 0.",
            },
        },
        "required": [],  # pattern or file_type must be provided; execute() validates
    }

    MAX_RESULT_SIZE_CHARS = 100_000
    DEFAULT_LIMIT = 100

    def execute(
        self,
        pattern: str | None = None,
        path: str | None = None,
        file_type: str | None = None,
        head_limit: int | None = None,
        offset: int = 0,
    ) -> ToolResult:
        # Validate: at least one of pattern or file_type must be provided
        if not pattern and not file_type:
            return ToolResult("Error: either 'pattern' or 'file_type' must be provided.", error=True)

        # Resolve to absolute path
        search_dir = Path(self._resolve_path(path, read_only=True)) if path else Path(self.cwd)
        if not search_dir.is_dir():
            return ToolResult(f"Error: path is not a directory: {search_dir}", error=True)

        # Build glob patterns
        if file_type:
            exts = TYPE_EXTENSIONS.get(file_type.lower())
            if not exts:
                return ToolResult(
                    f"Error: unknown file_type '{file_type}'. Available: {', '.join(sorted(TYPE_EXTENSIONS))}",
                    error=True,
                )
            glob_patterns = exts
        else:
            glob_patterns = [pattern]

        # Collect matching files, avoiding duplicates
        seen: set[str] = set()
        matched_files: list[Path] = []
        search_root = search_dir.resolve()

        for gp in glob_patterns:
            try:
                for p in search_dir.rglob(gp):
                    if not p.is_file():
                        continue
                    resolved_path = p.resolve()
                    # 符号链接可能把搜索引出 workspace：解析后必须仍在根目录内
                    if not resolved_path.is_relative_to(search_root):
                        continue
                    resolved = str(resolved_path)
                    if resolved in seen:
                        continue
                    # Check ignore dirs
                    if self._should_ignore(p, search_dir):
                        continue
                    seen.add(resolved)
                    matched_files.append(p)
            except Exception:
                continue

        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                # 断链/无权限文件：排到末尾，而不是让整个 glob 抛错终止
                return 0.0

        # Sort by modification time (newest first)
        matched_files.sort(key=_mtime, reverse=True)

        total_count = len(matched_files)

        # Apply offset
        if offset < 0:
            offset = 0
        if offset > 0:
            matched_files = matched_files[offset:]

        # Apply head_limit（schema 约定：0 = 不限条数；仍受 MAX_RESULT_SIZE_CHARS 兜底）
        if head_limit is None:
            limit = self.DEFAULT_LIMIT
        elif head_limit > 0:
            limit = head_limit
        else:
            limit = None
        truncated = limit is not None and len(matched_files) > limit
        if truncated:
            matched_files = matched_files[:limit]

        # Build output
        if not matched_files:
            return ToolResult("No files found")

        # Convert to relative paths for token savings
        lines = []
        for f in matched_files:
            try:
                rel = f.relative_to(search_dir)
            except ValueError:
                rel = f
            lines.append(str(rel))

        output = "\n".join(lines)

        # 条数不限时仍限总量，避免一次 glob 撑爆上下文
        if len(output) > self.MAX_RESULT_SIZE_CHARS:
            output = output[:self.MAX_RESULT_SIZE_CHARS] + "\n\n... (output truncated)"
        elif truncated:
            output += f"\n\n(Results are truncated: showing first {len(matched_files)} of {total_count} results. Consider using a more specific path or pattern.)"
        elif offset > 0:
            output += f"\n\n(Showing results {offset + 1}–{offset + len(lines)} of {total_count}.)"

        return ToolResult(output)

    def _should_ignore(self, file_path: Path, search_dir: Path) -> bool:
        """Check if file is inside an ignored directory."""
        try:
            rel = file_path.relative_to(search_dir)
            parts = rel.parts
            for part in parts[:-1]:  # Check directory parts only (not the file itself)
                if part in self.IGNORE_DIRS or part.endswith(self.IGNORE_DIR_SUFFIXES):
                    return True
        except ValueError:
            pass
        return False
