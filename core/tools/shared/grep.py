"""Grep tool - search file contents."""

from __future__ import annotations

import os

from core.tools.shared.base import Tool, ToolResult


# 文件类型 → 扩展名映射
TYPE_EXTENSIONS: dict[str, list[str]] = {
    "py":     ["*.py", "*.pyi"],
    "js":     ["*.js", "*.jsx", "*.mjs", "*.cjs"],
    "ts":     ["*.ts", "*.tsx", "*.mts", "*.cts"],
    "md":     ["*.md", "*.mdx"],
    "json":   ["*.json"],
    "yaml":   ["*.yaml", "*.yml"],
    "html":   ["*.html", "*.htm"],
    "css":    ["*.css", "*.scss", "*.sass", "*.less"],
    "go":     ["*.go"],
    "rust":   ["*.rs"],
    "java":   ["*.java", "*.kt", "*.scala"],
    "sh":     ["*.sh", "*.bash"],
    "txt":    ["*.txt"],
    "xml":    ["*.xml", "*.svg"],
    "sql":    ["*.sql"],
    "c":      ["*.c", "*.h"],
    "cpp":    ["*.cpp", "*.hpp", "*.cc", "*.hh"],
}


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search file contents using regular expressions. "
        "Supports case-insensitive matching, context lines, glob filters, and file type shortcuts. "
        "Use output_mode to get just file names or match counts. "
        "Use fixed_strings for literal text search (no regex). "
        "Respects common ignore patterns (.git, node_modules, __pycache__, etc.)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression pattern to search for (or literal text when fixed_strings=true).",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search in. Defaults to current working directory.",
            },
            "glob": {
                "type": "string",
                "description": "Glob pattern to filter files (e.g. '*.py', '*.ts'). Superseded by 'type' if both set.",
            },
            "type": {
                "type": "string",
                "description": "File type shortcut: 'py', 'js', 'ts', 'md', 'json', 'yaml', 'html', 'css', 'go', 'rust', 'java', 'sh', 'txt', 'xml', 'sql', 'c', 'cpp'. Overrides glob.",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case-insensitive search (default: false).",
            },
            "fixed_strings": {
                "type": "boolean",
                "description": "Treat pattern as literal text, not regex (default: false).",
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "'content' (default): matching lines with context. 'files_with_matches': just file paths. 'count': match count per file.",
            },
            "context": {
                "type": "integer",
                "description": "Number of context lines before and after each match (default: 0). Only used in 'content' mode.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of matches to return (default: 250).",
            },
        },
        "required": ["pattern"],
    }

    MAX_RESULT_SIZE_CHARS = 20_000  # GrepTool 硬上限（与 Bash 的 30K 不同）
    DEFAULT_HEAD_LIMIT = 250        # 默认最多返回 250 条匹配行
    MAX_FILES = 100                 # 最多检索 100 个文件
    MAX_COLUMNS = 500               # 每行最大字符数（防止长行撑爆上下文）

    def execute(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        type: str | None = None,
        case_insensitive: bool = False,
        fixed_strings: bool = False,
        output_mode: str = "content",
        context: int = 0,
        max_results: int = 250,
    ) -> ToolResult:
        # Resolve to absolute path
        path = self._resolve_path(path) if path else self.cwd

        # Step 1: Get all matching files first
        matching_files = self._find_matching_files(
            pattern=pattern,
            path=path,
            glob=glob,
            type=type,
            case_insensitive=case_insensitive,
            fixed_strings=fixed_strings,
        )

        if not matching_files:
            return ToolResult("No matches found.")

        # Step 2: Sort files by modification time (newest first)
        files_with_mtime = []
        for f in matching_files:
            try:
                mtime = os.path.getmtime(f)
                files_with_mtime.append((f, mtime))
            except OSError:
                continue
        files_with_mtime.sort(key=lambda x: x[1], reverse=True)
        sorted_files = [f for f, _ in files_with_mtime]

        # Limit to MAX_FILES
        if len(sorted_files) > self.MAX_FILES:
            sorted_files = sorted_files[:self.MAX_FILES]

        # Step 3: Generate output based on mode
        if output_mode == "files_with_matches":
            output = '\n'.join(sorted_files[:max_results])
            if len(sorted_files) > max_results:
                output += f"\n\n... ({len(sorted_files)} files total, limited to {max_results})"
            return ToolResult(output)

        elif output_mode == "count":
            return self._count_mode(sorted_files, pattern, case_insensitive, fixed_strings, max_results)

        else:  # content mode
            return self._content_mode(sorted_files, pattern, case_insensitive, fixed_strings, context, max_results)

    def _find_matching_files(
        self,
        pattern: str,
        path: str,
        glob: str | None,
        type: str | None,
        case_insensitive: bool,
        fixed_strings: bool,
    ) -> list[str]:
        """Find all files matching the pattern using grep -l."""
        cmd_parts = ["grep", "-r", "-l"]

        if fixed_strings:
            cmd_parts.append("-F")
        else:
            cmd_parts.append("-E")

        if case_insensitive:
            cmd_parts.append("-i")

        # File filtering
        if type:
            exts = TYPE_EXTENSIONS.get(type.lower())
            if not exts:
                return []
            for ext in exts:
                cmd_parts.extend(["--include", ext])
        elif glob:
            cmd_parts.extend(["--include", glob])

        # Exclude ignored directories
        for d in self.IGNORE_DIRS:
            cmd_parts.extend(["--exclude-dir", d])

        cmd_parts.extend([pattern, path])
        cmd = " ".join(self._shell_escape(p) for p in cmd_parts)
        cmd += " || true"

        result = self._run_bash(cmd, max_chars=50_000)
        if result.error or not result.output or result.output.startswith("[exit code: 1]"):
            return []

        files = [f for f in result.output.strip().split('\n') if f]
        return files

    def _count_mode(
        self,
        sorted_files: list[str],
        pattern: str,
        case_insensitive: bool,
        fixed_strings: bool,
        max_results: int,
    ) -> ToolResult:
        """Generate count output for sorted files."""
        output_lines = []
        for f in sorted_files[:max_results]:
            cmd_parts = ["grep", "-c"]
            if fixed_strings:
                cmd_parts.append("-F")
            else:
                cmd_parts.append("-E")
            if case_insensitive:
                cmd_parts.append("-i")
            cmd_parts.extend([pattern, f])
            cmd = " ".join(self._shell_escape(p) for p in cmd_parts)
            cmd += " || true"
            result = self._run_bash(cmd, max_chars=1000)
            count = result.output.strip() if result.output else "0"
            output_lines.append(f"{f}:{count}")

        output = '\n'.join(output_lines)
        if len(sorted_files) > max_results:
            output += f"\n\n... ({len(sorted_files)} files total, limited to {max_results})"
        return ToolResult(output)

    def _content_mode(
        self,
        sorted_files: list[str],
        pattern: str,
        case_insensitive: bool,
        fixed_strings: bool,
        context: int,
        max_results: int,
    ) -> ToolResult:
        """Generate content output for sorted files."""
        output_lines = []
        total_lines = 0

        for f in sorted_files:
            cmd_parts = ["grep", "-n"]
            if fixed_strings:
                cmd_parts.append("-F")
            else:
                cmd_parts.append("-E")
            if case_insensitive:
                cmd_parts.append("-i")
            if context > 0:
                cmd_parts.extend(["-C", str(context)])
            cmd_parts.extend([pattern, f])
            cmd = " ".join(self._shell_escape(p) for p in cmd_parts)
            cmd += f" | cut -c1-{self.MAX_COLUMNS} || true"

            result = self._run_bash(cmd, max_chars=10_000)
            if result.output and not result.output.startswith("[exit code: 1]"):
                lines = result.output.strip().split('\n')
                for line in lines:
                    if total_lines >= max_results:
                        break
                    # Add filename prefix to each line
                    # grep -n output: linenum:content or linenum-content (with context)
                    # We need: filepath:linenum:content
                    if line:
                        output_lines.append(f"{f}:{line}")
                        total_lines += 1

            if total_lines >= max_results:
                break

        output = '\n'.join(output_lines)
        if total_lines >= max_results:
            output += f"\n\n... (results limited to {max_results} lines)"
        return ToolResult(output)
