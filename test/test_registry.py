"""Tests for core/tools/registry.py — 统一工具注册表（A45：间接覆盖补强）。

验证：角色白名单工具名全部注册、master/worker/lite 集合关系、
"todo" 注册键映射到 name="todo_write" 的工具（T26 有意差异）。
"""

import pytest

from core.agent_config import load_agent_role
from core.tools.base import Tool
from core.tools.registry import TOOL_REGISTRY, create_tools


ROLES = ["master", "worker", "lite"]


class TestRegistryCompleteness:
    @pytest.mark.parametrize("role", ROLES)
    def test_all_whitelist_names_registered(self, role):
        """每个角色的工具白名单都必须能在注册表里解析。"""
        role_cfg = load_agent_role(role)
        missing = [name for name in role_cfg.tools if name not in TOOL_REGISTRY]
        assert not missing, f"角色 {role} 白名单中未注册: {missing}"

    def test_master_has_full_toolset(self):
        master = load_agent_role("master").tools
        for role in ("worker", "lite"):
            subset = load_agent_role(role).tools
            assert set(subset) <= set(master), (
                f"{role} 有 master 之外的工具: {set(subset) - set(master)}"
            )

    def test_no_duplicate_factories(self):
        """不同注册键不指向同一工具类（除有意别名外）。"""
        from core.tools.todo import TodoWriteTool
        todo_tool = TOOL_REGISTRY["todo"](load_agent_role("master"), ".", "", None, None, None)
        assert isinstance(todo_tool, TodoWriteTool)
        assert todo_tool.name == "todo_write"  # T26：注册键 "todo" 与工具名不同，属有意设计


class TestCreateTools:
    def test_instantiates_tool_objects(self, tmp_path):
        tools = create_tools(role="master", cwd=str(tmp_path), workspace_uuid="ws-test")
        assert len(tools) > 0
        assert all(isinstance(t, Tool) for t in tools)

    def test_lite_smallest(self):
        lite = create_tools(role="lite", cwd=".", workspace_uuid="ws-test")
        master = create_tools(role="master", cwd=".", workspace_uuid="ws-test")
        assert len(lite) < len(master)

    def test_role_cfg_fallback(self, tmp_path):
        """不传 role_cfg 时按 role 名回退加载。"""
        tools = create_tools(role="worker", cwd=str(tmp_path), workspace_uuid="ws-test")
        names = {t.name for t in tools}
        assert "bash" in names
        assert "ask_user" not in names  # worker 无 ask_user（交互工具仅 master）

    def test_names_match_registry_keys(self):
        """工具 name 与注册键一致（T26 中的 todo 除外）。"""
        role_cfg = load_agent_role("master")
        for name in role_cfg.tools:
            if name == "todo":
                continue  # 有意差异：注册键 "todo" → 工具名 "todo_write"
            tool = TOOL_REGISTRY[name](role_cfg, ".", "", None, None, None)
            assert tool.name == name, f"注册键 {name} 实例化出工具名 {tool.name}"
