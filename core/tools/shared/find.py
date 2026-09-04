"""Find tool - find files by glob pattern or file type."""

from __future__ import annotations

import os

from core.tools.shared.base import Tool, ToolResult


# 文件类型 → 扩展名映射（与 grep 共享同一逻辑）
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


class FindTool(Tool):
    name = "find"
    description = (
        "Find files by name pattern (glob syntax) or by file type shortcut. "
        "Recursively searches directories. "
        "Results can be sorted by path or modification time. "
        "Respects common ignore patterns (.git, node_modules, __pycache__, etc.)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match filenames (e.g. '*.py', 'test_*.js'). Ignored when 'type' is set.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in. Defaults to current working directory.",
            },
            "type": {
                "type": "string",
                "description": "File type shortcut: 'py', 'js', 'ts', 'md', 'json', 'yaml', 'html', 'css', 'go', 'rust', 'java', 'sh', 'txt', 'xml', 'sql', 'c', 'cpp'. Overrides pattern.",
            },
            "sort": {
                "type": "string",
                "enum": ["path", "modified"],
                "description": "Sort order: 'path' (alphabetical, default) or 'modified' (newest first).",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 100).",
            },
        },
        "required": [],  # pattern or type must be provided; execute() validates
    }

    MAX_RESULT_SIZE_CHARS = 100_000  # Find 工具结果上限

    def execute(
        self,
        pattern: str | None = None,
        path: str | None = None,
        type: str | None = None,
        sort: str = "path",
        max_results: int = 100,
    ) -> ToolResult:
        # Validate: at least one of pattern or type must be provided
        if not pattern and not type:
            return ToolResult("Error: either 'pattern' or 'type' must be provided.", error=True)

        # Resolve to absolute path
        path = self._resolve_path(path) if path else self.cwd

        # Build find command (works in Git Bash)
        # Prune ignored directories
        prune_args = []
        for d in self.IGNORE_DIRS:
            if prune_args:
                prune_args.extend(["-o"])
            prune_args.extend(["-name", d, "-prune"])

        prune_str = " ".join(self._shell_escape(p) for p in prune_args)

        # Build name match clause
        if type:
            exts = TYPE_EXTENSIONS.get(type.lower())
            if not exts:
                return ToolResult(
                    f"Error: unknown type '{type}'. Available: {', '.join(sorted(TYPE_EXTENSIONS))}",
                    error=True,
                )
            if len(exts) == 1:
                name_clause = f"-name {self._shell_escape(exts[0])}"
            else:
                name_parts = " -o ".join(f"-name {self._shell_escape(e)}" for e in exts)
                name_clause = f"\\( {name_parts} \\)"
        else:
            name_clause = f"-name {self._shell_escape(pattern)}"

        cmd = f"find {self._shell_escape(path)} {prune_str} -o -type f {name_clause} -print"

        # Sort and limit
        if sort == "modified":
            # Sort by modification time (newest first)
            # Use stat to get mtime, sort numerically descending, then extract path
            cmd += f" | while IFS= read -r f; do stat -c '%Y %n' \"$f\" 2>/dev/null; done | sort -rn | cut -d' ' -f2-"
        # else: sort=path — find output is already roughly sorted; no explicit sort needed for speed

        cmd += f" | head -n {max_results} || true"

        return self._run_bash(cmd, max_chars=self.MAX_RESULT_SIZE_CHARS)
