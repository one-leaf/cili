"""Grep tool - search file contents."""

from __future__ import annotations

import os

from core.tools.shared.base import Tool, ToolResult


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search file contents using regular expressions. "
        "Supports case-insensitive matching, context lines, and glob filters. "
        "Respects common ignore patterns (.git, node_modules, __pycache__, etc.)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search in. Defaults to current working directory.",
            },
            "glob": {
                "type": "string",
                "description": "Glob pattern to filter files (e.g. '*.py', '*.ts').",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case-insensitive search (default: false).",
            },
            "context": {
                "type": "integer",
                "description": "Number of context lines before and after each match (default: 0).",
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
        case_insensitive: bool = False,
        context: int = 0,
        max_results: int = 250,
    ) -> ToolResult:
        # Resolve to absolute path
        path = self._resolve_path(path) if path else self.cwd

        # Build grep command (works in Git Bash)
        cmd_parts = ["grep", "-r", "-n", "-E"]

        if case_insensitive:
            cmd_parts.append("-i")

        if context > 0:
            cmd_parts.extend(["-C", str(context)])

        if glob:
            cmd_parts.extend(["--include", glob])

        # Exclude ignored directories
        for d in self.IGNORE_DIRS:
            cmd_parts.extend(["--exclude-dir", d])

        cmd_parts.extend([pattern, path])

        # Pipe to head to limit results
        # Use || true to handle SIGPIPE (exit code 141) when head closes early
        cmd = " ".join(self._shell_escape(p) for p in cmd_parts)
        # 限制每行长度（防止长行撑爆上下文），然后限制行数
        cmd += f" | cut -c1-{self.MAX_COLUMNS} | head -n {max_results} || true"

        # max_chars 限制在 GrepTool 硬上限内（避免 _run_bash 默认 10K 提前截断）
        result = self._run_bash(cmd, max_chars=self.MAX_RESULT_SIZE_CHARS)

        # grep returns 1 if no matches found (not an error)
        if result.error and result.output.startswith("[exit code: 1]"):
            return ToolResult("No matches found.")

        # 限制最多 100 个文件的结果
        output = result.output
        if output and not output.startswith("No matches"):
            lines = output.split('\n')
            files_seen: set[str] = set()
            file_cutoff_idx = -1
            for idx, line in enumerate(lines):
                # grep 格式: filepath:linenum:content
                # Windows 路径含 C: 前缀，需跳过驱动器字母
                stripped = line.lstrip()
                if len(stripped) > 2 and stripped[1] == ':':
                    # Windows 路径：跳过 X: 后再按 : 分割
                    rest = stripped[2:]
                    colon_pos = rest.find(':')
                    if colon_pos >= 0:
                        filepath = stripped[:2 + colon_pos]
                        files_seen.add(filepath)
                        if len(files_seen) > self.MAX_FILES:
                            file_cutoff_idx = idx
                            break
                elif ':' in stripped:
                    filepath = stripped.split(':', 1)[0]
                    files_seen.add(filepath)
                    if len(files_seen) > self.MAX_FILES:
                        file_cutoff_idx = idx
                        break
            if file_cutoff_idx >= 0:
                kept = '\n'.join(lines[:file_cutoff_idx])
                result = ToolResult(
                    f"{kept}\n\n... (results limited to {self.MAX_FILES} files, "
                    f"more matches in other files omitted)"
                )

        return result
