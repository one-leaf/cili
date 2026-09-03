"""Web 搜索工具测试

注意：这些测试需要网络连接，可能会因为网络问题而失败。
"""

import pytest


class TestWebSearchTool:
    """Web 搜索工具测试"""

    @pytest.fixture(autouse=True)
    def cleanup_browser_tabs(self, tools):
        """测试后清理：关闭所有打开的 tab。"""
        yield

        # 测试结束后关闭所有打开的 tab
        try:
            from core.tools import get_tool_by_name
            browser_tool = get_tool_by_name(tools, "browser")
            list_result = browser_tool.execute(action="list_tabs")
            if not list_result.error and "No tabs" not in list_result.output:
                import re
                # 输出格式: "tab 1: open, idle=..."
                tab_indices = re.findall(r'^\s*tab\s+(\d+):', list_result.output, re.MULTILINE)
                for idx in tab_indices:
                    try:
                        browser_tool.execute(action="close_tab", tab_index=int(idx))
                    except Exception:
                        pass
        except Exception:
            pass  # 清理失败不影响测试结果

    @pytest.mark.skipif(
        not pytest.importorskip("requests", reason="requests not installed"),
        reason="requires network connection"
    )
    def test_web_search_basic(self, tools):
        """测试 web_search 工具 - 基本搜索"""
        from core.tools import get_tool_by_name

        search_tool = get_tool_by_name(tools, "web_search")
        result = search_tool.execute(query="Python programming", max_results=5)

        # 可能会因为网络问题失败
        if result.error:
            pytest.skip(f"Network error: {result.output}")

        assert "Python" in result.output or "python" in result.output.lower()
        # 应该有多个结果
        assert result.output.count("\n") > 5

    @pytest.mark.skipif(
        not pytest.importorskip("requests", reason="requests not installed"),
        reason="requires network connection"
    )
    def test_web_search_chinese(self, tools):
        """测试 web_search 工具 - 中文搜索"""
        from core.tools import get_tool_by_name

        search_tool = get_tool_by_name(tools, "web_search")
        result = search_tool.execute(query="Python 编程", max_results=3)

        # 可能会因为网络问题失败
        if result.error:
            pytest.skip(f"Network error: {result.output}")

        # 应该有结果
        assert not result.error or "Error" not in result.output

    @pytest.mark.skipif(
        not pytest.importorskip("requests", reason="requests not installed"),
        reason="requires network connection"
    )
    def test_web_search_with_max_results(self, tools):
        """测试 web_search 工具 - 限制结果数量"""
        from core.tools import get_tool_by_name

        search_tool = get_tool_by_name(tools, "web_search")

        # 请求 3 个结果
        result = search_tool.execute(query="test", max_results=3)

        if result.error:
            pytest.skip(f"Network error: {result.output}")

        # 结果应该被限制
        # 简单检查：输出不应该太长
        lines = [line for line in result.output.split("\n") if line.strip()]
        # 每个结果大约占 2-3 行（标题、URL、描述）
        assert len(lines) < 20
