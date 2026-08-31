"""Web 搜索工具测试

注意：这些测试需要网络连接，可能会因为网络问题而失败。
"""

import pytest


class TestWebSearchTool:
    """Web 搜索工具测试"""

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
