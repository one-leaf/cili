"""Tests for deferred tool loading + tool_search 机制。

验证：
- master 角色的 active/deferred 工具分离
- tool_schemas 仅包含 active 工具
- _activate_tools 正确迁移并重建 tool_schemas
- ToolSearchTool.execute() 搜索并激活延迟工具
- _execute_tool override 安全网：直接调用延迟工具自动激活
- worker 有 deferred_tools（非交互工具可延迟加载），lite 无 deferred_tools
- system prompt 包含 Deferred Tools 段
"""

from unittest.mock import MagicMock, patch

import pytest

from core.session_runner import SessionRunner
from core.role_config import load_role
from core.prompt_sections import _build_deferred_tools_section
from core.tools import create_tools, get_tool_by_name
from core.tools.tool_search import ToolSearchTool


def _make_mock_config():
    config = MagicMock()
    config.model = MagicMock()
    config.system.max_iterations = 200
    return config


def _make_master_runner():
    """构造 master Agent，使用真实工具实例化（但 mock LLM client）。"""
    config = _make_mock_config()
    with patch("core.session_runner.create_llm_client") as mock_client:
        mock_client.return_value = MagicMock()
        return SessionRunner(config, role="master")


class TestDeferredToolSplit:
    """master 角色的 active/deferred 工具分离。"""

    def test_master_has_deferred_tools(self):
        """master 角色声明了 deferred_tools。"""
        role_cfg = load_role("master")
        assert len(role_cfg.deferred_tools) == 8
        assert "browser" in role_cfg.deferred_tools
        assert "latex" in role_cfg.deferred_tools
        assert "cron" in role_cfg.deferred_tools

    def test_tool_search_is_core(self):
        """tool_search 在 tools 列表中，不在 deferred_tools 中。"""
        role_cfg = load_role("master")
        assert "tool_search" in role_cfg.tools
        assert "tool_search" not in role_cfg.deferred_tools

    def test_tool_schemas_exclude_deferred(self):
        """tool_schemas 只包含 active 工具，不含 deferred。"""
        runner = _make_master_runner()
        schema_names = {s["name"] for s in runner.tool_schemas}

        # Deferred tools should NOT be in schemas
        for name in runner.role_cfg.deferred_tools:
            # todo 注册键映射到 todo_write 工具名
            tool_obj = get_tool_by_name(runner.tools, name)
            if tool_obj:
                assert tool_obj.name not in schema_names, (
                    f"deferred tool {name} (name={tool_obj.name}) should not be in tool_schemas"
                )

        # Core tools should be in schemas
        assert "tool_search" in schema_names
        assert "bash" in schema_names
        assert "read" in schema_names

    def test_active_tools_count(self):
        """active tools = total tools - deferred tools。"""
        runner = _make_master_runner()
        total = len(runner.tools)
        deferred = len(runner._deferred_tools)
        active = len(runner._active_tools)
        assert active + deferred == total
        assert len(runner.tool_schemas) == active

    def test_all_tools_still_instantiated(self):
        """所有工具（含 deferred）仍然实例化在 self.tools 中。"""
        runner = _make_master_runner()
        tool_names = {t.name for t in runner.tools}
        # Deferred tools should still exist as instances
        for reg_name in runner.role_cfg.deferred_tools:
            tool_obj = get_tool_by_name(runner.tools, reg_name)
            assert tool_obj is not None, f"deferred tool {reg_name} should be instantiated"


class TestActivateTools:
    """_activate_tools 迁移和重建。"""

    def test_activate_moves_tool(self):
        """激活一个 deferred 工具后，它移入 active 和 tool_schemas。"""
        runner = _make_master_runner()
        initial_active = len(runner._active_tools)
        initial_schemas = len(runner.tool_schemas)

        # browser 是 deferred 工具
        assert "browser" in runner._deferred_names

        runner._activate_tools(["browser"])

        assert "browser" not in runner._deferred_names
        assert len(runner._active_tools) == initial_active + 1
        assert len(runner.tool_schemas) == initial_schemas + 1

        schema_names = {s["name"] for s in runner.tool_schemas}
        assert "browser" in schema_names

    def test_activate_nonexistent_is_noop(self):
        """激活已 active 或不存在的工具，不报错。"""
        runner = _make_master_runner()
        initial_schemas = len(runner.tool_schemas)

        runner._activate_tools(["bash"])  # already active
        assert len(runner.tool_schemas) == initial_schemas

        runner._activate_tools(["nonexistent"])  # not a tool at all
        assert len(runner.tool_schemas) == initial_schemas

    def test_activate_updates_tool_search(self):
        """激活后 tool_search.deferred_tools 同步更新。"""
        runner = _make_master_runner()
        ts = get_tool_by_name(runner.tools, "tool_search")
        assert ts is not None
        initial_deferred = len(ts.deferred_tools)

        runner._activate_tools(["browser"])

        assert len(ts.deferred_tools) == initial_deferred - 1


class TestToolSearchExecute:
    """ToolSearchTool.execute() 搜索和返回 schema。"""

    def test_search_by_name(self):
        """按工具名搜索返回完整 schema。"""
        runner = _make_master_runner()
        ts = get_tool_by_name(runner.tools, "tool_search")
        assert isinstance(ts, ToolSearchTool)

        result = ts.execute(query="browser")
        assert not result.error
        assert "browser" in result.output

    def test_search_activates_tool(self):
        """搜索后工具被激活。"""
        runner = _make_master_runner()
        ts = get_tool_by_name(runner.tools, "tool_search")
        assert "browser" in runner._deferred_names

        ts.execute(query="browser")

        assert "browser" not in runner._deferred_names
        schema_names = {s["name"] for s in runner.tool_schemas}
        assert "browser" in schema_names

    def test_search_no_match(self):
        """无匹配时返回可用列表。"""
        runner = _make_master_runner()
        ts = get_tool_by_name(runner.tools, "tool_search")

        result = ts.execute(query="xyznonexistent")
        assert "No deferred tools matched" in result.output

    def test_search_empty_query(self):
        """空 query 返回错误。"""
        runner = _make_master_runner()
        ts = get_tool_by_name(runner.tools, "tool_search")

        result = ts.execute(query="")
        assert result.error

    def test_search_multiword_query(self):
        """多词查询按 token AND 匹配：'cron schedule' 命中 cron（旧版整串子串匹配不到）。"""
        runner = _make_master_runner()
        ts = get_tool_by_name(runner.tools, "tool_search")

        result = ts.execute(query="cron schedule")
        assert not result.error
        assert "cron" in result.output

    def test_search_multiword_name_tokens(self):
        """多词查询每个 token 都命中工具名：'pdf markdown' 命中 pdf2markdown。"""
        runner = _make_master_runner()
        ts = get_tool_by_name(runner.tools, "tool_search")

        result = ts.execute(query="pdf markdown")
        assert not result.error
        assert "pdf2markdown" in result.output

    def test_search_multiword_hyphen_split(self):
        """带连字符的多词查询拆分后匹配：'message-bus' 命中 message_bus。"""
        runner = _make_master_runner()
        ts = get_tool_by_name(runner.tools, "tool_search")

        result = ts.execute(query="message-bus")
        assert not result.error
        assert "message_bus" in result.output

    def test_search_multiword_requires_all_tokens(self):
        """AND 语义：'cron browser' 无工具同时命中两个 token → 无匹配。"""
        runner = _make_master_runner()
        ts = get_tool_by_name(runner.tools, "tool_search")

        result = ts.execute(query="cron browser")
        assert "No deferred tools matched" in result.output


class TestExecuteToolOverride:
    """_execute_tool override 安全网。"""

    def test_direct_call_activates_deferred(self):
        """直接调用延迟工具时自动激活。"""
        runner = _make_master_runner()
        assert "browser" in runner._deferred_names

        # 模拟 _execute_tool 调用延迟工具
        # 不需要真正执行（会调用浏览器），只需确认 _activate_tools 被调用
        with patch.object(runner, "_activate_tools", wraps=runner._activate_tools) as mock_activate:
            try:
                runner._execute_tool("browser", {"action": "list_tabs"}, "test-id")
            except Exception:
                pass  # browser 工具可能因无 Chrome 报错，但 activate 已触发
            mock_activate.assert_called_once_with(["browser"])


class TestDeferredToolsSection:
    """system prompt 中的 Deferred Tools 段。"""

    def test_deferred_section_content(self):
        """_build_deferred_tools_section 包含工具名称和描述。"""
        runner = _make_master_runner()
        section = _build_deferred_tools_section(runner._deferred_tools)

        assert "## Deferred Tools" in section
        assert "tool_search" in section  # 引导模型使用 tool_search
        # 延迟工具名称应出现在段中
        for tool in runner._deferred_tools:
            assert tool.name in section

    def test_system_prompt_has_deferred_section(self):
        """master 的 system prompt 包含 Deferred Tools 段。"""
        runner = _make_master_runner()
        parts = runner._build_system_prompt()
        prompt = "\n\n".join(parts) if isinstance(parts, list) else parts

        assert "## Deferred Tools" in prompt
        assert "tool_search" in prompt


class TestWorkerLiteDeferred:
    """worker 有 deferred_tools（非交互工具可延迟加载），lite 无。"""

    def test_worker_deferred_tools(self):
        """worker 的 deferred_tools 覆盖非交互工具，不含交互三件套。"""
        role_cfg = load_role("worker")
        expected = {"browser", "todo_write", "latex", "message_bus", "temp", "loop", "pdf2markdown"}
        assert set(role_cfg.deferred_tools) == expected

    def test_lite_no_deferred_tools(self):
        """lite 的 deferred_tools 为空。"""
        role_cfg = load_role("lite")
        assert role_cfg.deferred_tools == []
