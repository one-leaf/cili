"""Tool registry - re-exports and factory functions."""

from __future__ import annotations

from core.config import Config
from core.tools.base import Tool, ToolResult
from core.tools.registry import TOOL_REGISTRY, create_tools


def get_tool_by_name(tools: list[Tool], name: str) -> Tool | None:
    """Find a tool by name in a role's tool list.

    线性扫描即可：工具数约 26，且仅在工具集重建与每次工具执行时调用一次。
    （此前用 id(tools) 缓存映射，但列表被 GC 后新列表可能复用同一地址，
    导致返回陈旧映射，故移除。）
    """
    for tool in tools:
        if tool.name == name:
            return tool
    return None
