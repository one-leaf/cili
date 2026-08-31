"""Tool registry - re-exports and factory functions."""

from __future__ import annotations

from core.config import Config
from core.tools.shared.base import Tool, ToolResult
from core.tools.shared import create_shared_tools
from core.tools.root import create_root_tools
from core.tools.sub import create_sub_tools


def create_tools(
    cwd: str = ".", workspace_uuid: str = "", session_manager=None, config: Config | None = None
) -> list[Tool]:
    """Create all tools (root agent = shared + root)."""
    return (
        create_shared_tools(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager, config=config)
        + create_root_tools(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager)
    )


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
