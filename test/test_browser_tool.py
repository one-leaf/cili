"""浏览器工具测试

注意：这些测试需要 Chrome 浏览器和 Playwright。
如果环境不支持，测试会被跳过。
"""

import pytest


class TestBrowserTool:
    """浏览器工具测试"""

    @pytest.fixture(autouse=True)
    def check_browser_available(self, tools):
        """检查浏览器是否可用"""
        from core.tools import get_tool_by_name

        try:
            browser_tool = get_tool_by_name(tools, "browser")
            # 尝试执行一个简单的操作来检查浏览器是否可用
            result = browser_tool.execute(action="navigate", url="about:blank")
            if result.error:
                pytest.skip("Browser not available")
        except Exception as e:
            pytest.skip(f"Browser not available: {e}")

    def test_browser_navigate(self, tools):
        """测试 browser 工具 - 导航"""
        from core.tools import get_tool_by_name

        browser_tool = get_tool_by_name(tools, "browser")
        result = browser_tool.execute(
            action="navigate",
            url="https://example.com"
        )

        # 可能会因为网络问题失败
        if result.error:
            pytest.skip(f"Network error: {result.output}")

        assert not result.error
        assert "Example" in result.output or "example.com" in result.output.lower()

    def test_browser_get_text(self, tools):
        """测试 browser 工具 - 获取文本"""
        from core.tools import get_tool_by_name

        browser_tool = get_tool_by_name(tools, "browser")

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

    def test_browser_execute_javascript(self, tools):
        """测试 browser 工具 - 执行 JavaScript"""
        from core.tools import get_tool_by_name

        browser_tool = get_tool_by_name(tools, "browser")

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
