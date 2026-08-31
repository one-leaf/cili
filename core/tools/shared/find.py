"""Find tool - find files by glob pattern."""

from __future__ import annotations

import os

from core.tools.shared.base import Tool, ToolResult


class FindTool(Tool):
    name = "find"
    description = (
        "Find files by name pattern (glob syntax). "
        "Recursively searches directories. "
        "Respects common ignore patterns (.git, node_modules, __pycache__, etc.)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match filenames (e.g. '*.py', 'test_*.js').",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in. Defaults to current working directory.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 100).",
            },
        },
        "required": ["pattern"],
    }

    MAX_RESULT_SIZE_CHARS = 100_000  # Find 工具结果上限

    def execute(
        self,
        pattern: str,
        path: str | None = None,
        max_results: int = 100,
    ) -> ToolResult:
        # Resolve to absolute path
        path = self._resolve_path(path) if path else self.cwd

        # Build find command (works in Git Bash)
        # Prune ignored directories
        prune_args = []
        for d in self.IGNORE_DIRS:
            if prune_args:
                prune_args.extend(["-o"])
            prune_args.extend(["-name", d, "-prune"])

        # -name DIR -prune -o -type f -name pattern -print
        cmd = f"find {self._shell_escape(path)} {' '.join(self._shell_escape(p) for p in prune_args)} -o -type f -name {self._shell_escape(pattern)} -print"

        # Pipe to head to limit results
        # Use || true to handle SIGPIPE (exit code 141) when head closes early
        cmd += f" | head -n {max_results} || true"

        return self._run_bash(cmd, max_chars=self.MAX_RESULT_SIZE_CHARS)
