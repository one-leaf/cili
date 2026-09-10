"""Tool registry - re-exports and factory functions."""

from __future__ import annotations

from core.config import Config
from core.tools.base import Tool, ToolResult
from core.tools.registry import TOOL_REGISTRY, create_tools


def get_tool_by_name(tools: list[Tool], name: str) -> Tool | None:
    """Find a tool by name (O(1) via dict lookup)."""
    return _tool_map(tools).get(name)


# Cache the tool map per tools list to avoid rebuilding every call.
# Uses id(tools) as key — the list reference is stable within an agent's lifetime.
_tool_map_cache: dict[int, dict[str, Tool]] = {}


def _tool_map(tools: list[Tool]) -> dict[str, Tool]:
    """Build or return cached {name: tool} mapping."""
    key = id(tools)
    m = _tool_map_cache.get(key)
    if m is None:
        m = {t.name: t for t in tools}
        _tool_map_cache[key] = m
    return m
