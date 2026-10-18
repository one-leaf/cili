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

        # 检查浏览器是否可用（about:blank 已被 SSRF 防护拦截，改用公网地址探测）
        try:
            result = browser_tool.execute(action="navigate", url="https://example.com")
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


class TestBrowserToolSchemaAndParsing:
    """无需真实浏览器的回归（上类的 autouse fixture 只作用于该类）。"""

    def test_upload_schema_exposes_path(self):
        """upload 实现要求 path，schema 必须暴露它，否则模型无从传参（曾整条不可用）。"""
        from core.tools.browser import BrowserTool

        assert "path" in BrowserTool.parameters["properties"]

    def test_nameless_interactive_element_gets_ref(self):
        """无可访问名的交互元素也要分配 ref。

        回归：JS 注入的 <div role="button"> 常无文本标签，快照里看得见却寻址不了
        （drag/click 报 Unknown ref）。结构性无名节点仍不应占用 ref 预算。
        """
        from core.browser_service import _parse_aria_snapshot

        yaml_text = "\n".join([
            "- banner:",
            '  - button "Sign in"',
            "  - button",
            "  - generic:",
            "    - paragraph: hello",
        ])
        _lines, ref_map = _parse_aria_snapshot(yaml_text)

        assert len([r for r, e in ref_map.items() if e["name"] == "Sign in"]) == 1
        assert len([r for r, e in ref_map.items()
                    if e["role"] == "button" and not e["name"]]) == 1
        assert not [r for r, e in ref_map.items()
                    if e["role"] == "generic" and not e["name"]]

    def test_extract_passes_single_object_arg(self, monkeypatch):
        """回归：Playwright 的 evaluate 只收 1 个 arg。

        原先 extract 传了 4 个位置参数，任何 selector/attribute 组合都抛
        TypeError（Page.evaluate() takes from 2 to 3 positional arguments but 5
        were given），功能整条不可用。这里用与 Playwright 同签名的假 page 钉住。
        """
        from core import browser_service as bs

        captured = {}

        class _FakePage:
            def evaluate(self, expression, arg=None):
                captured["expression"] = expression
                captured["arg"] = arg
                return ["alpha", "beta"]

        svc = bs.BrowserService.__new__(bs.BrowserService)   # 跳过 __init__，不起浏览器
        monkeypatch.setattr(
            svc, "_execute_operation",
            lambda name, func, tab_index=None: func(_FakePage()),
        )

        r = svc.extract("div.item", attribute="text", limit=5)

        assert not r.error, r.output
        assert "alpha" in r.output and "beta" in r.output
        assert captured["arg"] == {"sel": "div.item", "attr": "text", "limit": 5}

    def test_locator_for_nameless_ref_uses_role_only(self):
        """无名 ref 按 role 定位，不能传 name=""（那是「可访问名为空」的另一种语义）。"""
        from core import browser_service as bs

        calls = []

        class _FakeBase:
            def get_by_role(self, role, name=None, exact=False):
                calls.append({"role": role, "name": name, "exact": exact})
                return self

            def nth(self, index):
                calls.append({"nth": index})
                return self

        svc = bs.BrowserService.__new__(bs.BrowserService)
        svc._locator_for(
            _FakeBase(), {"role": "button", "name": "", "index": 1, "mode": "role"}
        )

        assert calls == [
            {"role": "button", "name": None, "exact": False},
            {"nth": 1},
        ]
