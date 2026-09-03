"""浏览器工具测试

注意：这些测试需要 Chrome 浏览器和 Playwright。
如果环境不支持，测试会被跳过。
"""

import pytest


class TestBrowserTool:
    """浏览器工具测试"""

    @pytest.fixture(autouse=True)
    def browser_setup_teardown(self, tools):
        """浏览器测试准备：检查浏览器可用性；测试后清理：关闭所有打开的 tab。"""
        from core.tools import get_tool_by_name

        browser_tool = get_tool_by_name(tools, "browser")

        # 检查浏览器是否可用
        try:
            result = browser_tool.execute(action="navigate", url="about:blank")
            if result.error:
                pytest.skip("Browser not available")
        except Exception as e:
            pytest.skip(f"Browser not available: {e}")

        yield browser_tool

        # 测试结束后关闭所有打开的 tab
        try:
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

    def test_browser_navigate(self, browser_setup_teardown):
        """测试 browser 工具 - 导航"""
        browser_tool = browser_setup_teardown
        result = browser_tool.execute(
            action="navigate",
            url="https://example.com"
        )

        # 可能会因为网络问题失败
        if result.error:
            pytest.skip(f"Network error: {result.output}")

        assert not result.error
        assert "Example" in result.output or "example.com" in result.output.lower()

    def test_browser_get_text(self, browser_setup_teardown):
        """测试 browser 工具 - 获取文本"""
        browser_tool = browser_setup_teardown

        # 先导航
        nav_result = browser_tool.execute(
            action="navigate",
            url="https://example.com"
        )

        if nav_result.error:
            pytest.skip(f"Network error: {nav_result.output}")

        # 获取文本
        result = browser_tool.execute(action="get_text")
        assert not result.error
        assert "Example" in result.output or len(result.output) > 0

    def test_browser_execute_javascript(self, browser_setup_teardown):
        """测试 browser 工具 - 执行 JavaScript"""
        browser_tool = browser_setup_teardown

        # 先导航
        nav_result = browser_tool.execute(
            action="navigate",
            url="https://example.com"
        )

        if nav_result.error:
            pytest.skip(f"Network error: {nav_result.output}")

        # 执行 JavaScript
        result = browser_tool.execute(
            action="execute",
            script="document.title"
        )

        assert not result.error
        # 应该返回页面标题
        assert len(result.output) > 0
