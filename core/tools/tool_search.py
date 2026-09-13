"""ToolSearchTool — search and activate deferred tools on demand.

Deferred tools are fully instantiated but their schemas are hidden from the LLM
by default. This tool returns their full schemas when the model queries for them
and triggers activation so the schemas are included in subsequent API calls.
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.tools.base import Tool, ToolResult


class ToolSearchTool(Tool):
    name = "tool_search"
    description = (
        "Search for deferred tools by keyword and load their full schema.\n"
        "Deferred tools are available but not shown in the main tool list to reduce noise.\n"
        "Call this tool before using a deferred tool to get its parameter schema and usage details.\n"
        "Once loaded, the tool becomes active for the rest of the session."
    )

    # Injected by Agent._rebuild_tools after instantiation
    deferred_tools: list[Tool] = []
    on_load: Any = None  # Callable[[list[str]], None] | None

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session_manager=None, **_kwargs):
        super().__init__(cwd, workspace_uuid, session_manager)

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword to search for (matched against tool name and description).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 5).",
                },
            },
            "required": ["query"],
        }

    def execute(self, query: str, max_results: int = 5, **_kwargs) -> ToolResult:
        """Search deferred tools and activate matches."""
        if not query:
            return ToolResult("Error: query is required", error=True)

        # 多词查询按空白/下划线/连字符拆成 token，每个 token 都必须命中 name 或
        # description 之一（AND），得分按 token 累加；单 token 行为与旧版一致。
        tokens = [t for t in re.split(r"[\s_\-/]+", query.lower()) if t]
        if not tokens:
            return ToolResult("Error: query is required", error=True)

        scored: list[tuple[int, Tool]] = []

        for tool in self.deferred_tools:
            name_lower = tool.name.lower()
            name_parts = [p for p in name_lower.replace("_", " ").split()]
            desc = tool.description.lower()
            desc_first_line = tool.description.split("\n")[0].lower()

            token_scores: list[int] = []
            for tok in tokens:
                score = 0
                # Exact name match
                if tok == name_lower:
                    score = 100
                # Name contains token
                elif tok in name_lower:
                    score = 50
                # Token matches any word in name
                elif any(tok in part for part in name_parts):
                    score = 30

                # Description first-line match
                if tok in desc_first_line:
                    score = max(score, 20)
                # Broader description match
                elif tok in desc:
                    score = max(score, 10)
                token_scores.append(score)

            if all(s > 0 for s in token_scores):
                scored.append((sum(token_scores), tool))

        # Sort by score descending
        scored.sort(key=lambda x: -x[0])
        matches = [tool for _, tool in scored[:max_results]]

        if not matches:
            available = [t.name for t in self.deferred_tools]
            return ToolResult(
                f"No deferred tools matched query '{query}'. "
                f"Available deferred tools: {', '.join(available)}"
            )

        # Build result with full schemas
        schemas = []
        activated_names = []
        for tool in matches:
            schemas.append(tool.to_schema())
            activated_names.append(tool.name)

        # Activate matched tools
        if self.on_load:
            self.on_load(activated_names)

        result_lines = [
            f"Found {len(matches)} tool(s) matching '{query}':",
            "",
        ]
        for schema in schemas:
            result_lines.append(f"### {schema['name']}")
            result_lines.append(f"Description: {schema['description']}")
            result_lines.append(f"Parameters: {json.dumps(schema['input_schema'], ensure_ascii=False, indent=2)}")
            result_lines.append("")

        result_lines.append(f"Tool(s) activated: {', '.join(activated_names)}")
        result_lines.append("You can now use these tools directly.")

        return ToolResult("\n".join(result_lines))
