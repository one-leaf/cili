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


_CJK_ONLY_RE = re.compile(r"^[一-鿿]+$")


def _term_variants(term: str) -> list[str]:
    """中文词组按 2 字切块（新增提单 → 新增/提单），英文/短词保持原样。"""
    if len(term) <= 2 or not _CJK_ONLY_RE.match(term):
        return [term]
    return [term[i:i + 2] for i in range(0, len(term), 2)]


class ToolSearchTool(Tool):
    name = "tool_search"
    description = (
        "Search tools by keyword and return their full schemas.\n"
        "Deferred tools are activated on demand; already-active tools are returned too.\n"
        "Use this to discover a tool before calling it, or to remind yourself of a tool's parameters."
    )

    # Injected by Agent._rebuild_tools after instantiation
    on_load: Any = None  # Callable[[list[str]], None] | None

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session=None, **_kwargs):
        super().__init__(cwd, workspace_uuid, session)
        # 实例级：类级可变默认值会被所有实例共享
        self.deferred_tools: list[Tool] = []
        # 当前接口/模型下所有可见工具（active + deferred）；未注入时回退 deferred
        self.searchable_tools: list[Tool] = []

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword to search for (matched against tool name, search aliases, and description).",
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

        # 多词查询按空白/下划线/连字符拆成 term，每个 term 都必须命中 name /
        # search_keywords / description 之一（AND）；中文长词再按 2 字切块，
        # 例如“新增提单”拆成“新增”+“提单”，避免整串子串匹配不到。
        terms = [t for t in re.split(r"[\s_\-/]+", query.lower()) if t]
        if not terms:
            return ToolResult("Error: query is required", error=True)

        tools = self.searchable_tools or self.deferred_tools
        scored: list[tuple[int, Tool]] = []

        def _score(tok: str, name_lower: str, name_parts: list[str],
                   keywords: str, desc_first_line: str, desc: str) -> int:
            score = 0
            if tok == name_lower:
                score = 100
            elif tok in name_lower:
                score = 50
            elif any(tok in part for part in name_parts):
                score = 30
            elif keywords and tok in keywords:
                score = 25
            if tok in desc_first_line:
                score = max(score, 20)
            elif tok in desc:
                score = max(score, 10)
            return score

        for tool in tools:
            name_lower = tool.name.lower()
            name_parts = [p for p in name_lower.replace("_", " ").split()]
            desc = tool.description.lower()
            desc_first_line = tool.description.split("\n")[0].lower()
            keywords = " ".join(getattr(tool, "search_keywords", ()) or ()).lower()

            term_scores: list[int] = []
            for term in terms:
                score = _score(term, name_lower, name_parts, keywords,
                               desc_first_line, desc)
                if not score:
                    chunks = _term_variants(term)
                    if len(chunks) > 1:
                        chunk_scores = [
                            _score(c, name_lower, name_parts, keywords,
                                   desc_first_line, desc)
                            for c in chunks
                        ]
                        if all(s > 0 for s in chunk_scores):
                            score = sum(chunk_scores) + 5
                if not score:
                    break
                term_scores.append(score)

            if len(term_scores) == len(terms):
                scored.append((sum(term_scores), tool))

        # Sort by score descending
        scored.sort(key=lambda x: -x[0])
        matches = [tool for _, tool in scored[:max_results]]

        if not matches:
            available = [t.name for t in tools]
            return ToolResult(
                f"No tools matched query '{query}'. "
                "该工具可能已激活，可直接调用；否则换一个工具名或中文别名。 "
                f"Available tools: {', '.join(available)}"
            )

        # Build result with full schemas
        schemas = [tool.to_schema() for tool in matches]
        matched_names = [tool.name for tool in matches]
        deferred_names = {tool.name for tool in self.deferred_tools}
        newly = [name for name in matched_names if name in deferred_names]
        already = [name for name in matched_names if name not in deferred_names]

        # Activate only deferred matches; active matches are returned as-is
        if newly and self.on_load:
            self.on_load(newly)

        result_lines = [
            f"Found {len(matches)} tool(s) matching '{query}':",
            "",
        ]
        for schema in schemas:
            result_lines.append(f"### {schema['name']}")
            result_lines.append(f"Description: {schema['description']}")
            result_lines.append(f"Parameters: {json.dumps(schema['input_schema'], ensure_ascii=False, indent=2)}")
            result_lines.append("")

        if newly:
            result_lines.append(f"Tool(s) activated: {', '.join(newly)}")
        if already:
            result_lines.append(f"Already active: {', '.join(already)}")
        result_lines.append("You can now use these tools directly.")

        return ToolResult("\n".join(result_lines))
