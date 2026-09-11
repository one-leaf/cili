"""浏览器 ref 定位（snapshot/find/click/fill/type/press）与诊断（console/requests）测试。

第一部分：_parse_aria_snapshot 纯逻辑解析（不依赖浏览器/网络）。
第二部分：BrowserTool action 分发（mock BrowserService，验证参数转发与缺参校验）。
"""

import re
from unittest.mock import MagicMock, patch

import pytest


class TestParseAriaSnapshot:
    """_parse_aria_snapshot 解析真实 aria_snapshot 输出。"""

    SAMPLE = """- banner:
  - heading "Hello World" [level=1]
- navigation:
  - link "Home":
    - /url: /home
  - link "About":
    - /url: /about
- main:
  - button "Sign in"
  - button "Sign in"
  - textbox "Search..."
  - paragraph: Some welcome text"""

    def _parse(self, yaml_text, **kwargs):
        from core.browser_service import _parse_aria_snapshot
        return _parse_aria_snapshot(yaml_text, **kwargs)

    def test_refs_assigned_in_dfs_order(self):
        lines, ref_map = self._parse(self.SAMPLE)
        ref_lines = [line for _, line in lines if "[ref=" in line]
        # banner/navigation/main 无 name 不分配；heading→r1, link Home→r2, link About→r3,
        # button×2→r4/r5, textbox→r6, paragraph→r7
        assert ["r1", "r2", "r3", "r4", "r5", "r6", "r7"] == [
            re.search(r"\[ref=(\w+)\]", line).group(1) for line in ref_lines
        ]
        assert len(ref_map) == 7

    def test_container_nodes_get_no_ref(self):
        lines, ref_map = self._parse(self.SAMPLE)
        for _, line in lines:
            stripped = line.strip()
            if stripped.startswith("- banner") or stripped.startswith("- navigation") \
                    or stripped.startswith("- main"):
                assert "[ref=" not in line

    def test_duplicate_role_name_increments_index(self):
        lines, ref_map = self._parse(self.SAMPLE)
        buttons = [e for e in ref_map.values() if e["role"] == "button"]
        assert [e["index"] for e in buttons] == [0, 1]
        assert all(e["name"] == "Sign in" for e in buttons)

    def test_text_mode_node(self):
        lines, ref_map = self._parse(self.SAMPLE)
        para = next(e for e in ref_map.values() if e["role"] == "paragraph")
        assert para["mode"] == "text"
        assert para["name"] == "Some welcome text"

    def test_role_mode_node(self):
        lines, ref_map = self._parse(self.SAMPLE)
        heading = next(e for e in ref_map.values() if e["role"] == "heading")
        assert heading["mode"] == "role"
        assert heading["name"] == "Hello World"

    def test_inline_attrs_preserved_in_line(self):
        lines, ref_map = self._parse(self.SAMPLE)
        heading_line = next(l for _, l in lines if 'heading "Hello World"' in l)
        assert "[level=1]" in heading_line
        assert "[ref=" in heading_line

    def test_quoted_name_unescaped(self):
        lines, ref_map = self._parse('- button "Save \\"file\\""')
        entry = next(iter(ref_map.values()))
        assert entry["name"] == 'Save "file"'

    def test_max_refs_caps_allocations(self):
        big = "\n".join(f'- button "Btn {i}"' for i in range(50))
        lines, ref_map = self._parse(big, max_refs=10)
        assert len(ref_map) == 10
        assert len([1 for _, l in lines if "[ref=" in l]) == 10

    def test_property_lines_preserved_without_ref(self):
        lines, ref_map = self._parse(self.SAMPLE)
        joined = "\n".join(l for _, l in lines)
        assert "/url: /home" in joined
        assert "/url" not in ref_map


class TestBrowserToolDispatch:
    """BrowserTool action 转发：mock BrowserService，验证参数映射与缺参报错。"""

    def _service(self):
        svc = MagicMock()
        svc.is_running.return_value = True
        return svc

    def _execute(self, svc, **kwargs):
        from core.tools.browser import BrowserTool
        with patch("core.browser_service.get_service", return_value=svc):
            return BrowserTool().execute(**kwargs)

    def test_snapshot(self):
        svc = self._service()
        self._execute(svc, action="snapshot", tab_index=2)
        svc.snapshot.assert_called_once_with(tab_index=2)

    def test_find(self):
        svc = self._service()
        self._execute(svc, action="find", pattern="Sign in")
        svc.find.assert_called_once_with("Sign in", tab_index=None)

    def test_click(self):
        svc = self._service()
        self._execute(svc, action="click", ref="r4")
        svc.click.assert_called_once_with("r4", tab_index=None)

    def test_fill(self):
        svc = self._service()
        self._execute(svc, action="fill", ref="r6", text="hello")
        svc.fill.assert_called_once_with("r6", "hello", tab_index=None)

    def test_type(self):
        svc = self._service()
        self._execute(svc, action="type", ref="r6", text="hi")
        svc.type.assert_called_once_with("r6", "hi", tab_index=None)

    def test_press_with_ref(self):
        svc = self._service()
        self._execute(svc, action="press", ref="r3", key="Enter")
        svc.press.assert_called_once_with("r3", "Enter", tab_index=None)

    def test_press_page_level(self):
        svc = self._service()
        self._execute(svc, action="press", key="Escape")
        svc.press.assert_called_once_with(None, "Escape", tab_index=None)

    def test_go_back_forward_reload(self):
        svc = self._service()
        self._execute(svc, action="go_back")
        svc.go_back.assert_called_once_with(tab_index=None)
        self._execute(svc, action="go_forward")
        svc.go_forward.assert_called_once_with(tab_index=None)
        self._execute(svc, action="reload")
        svc.reload.assert_called_once_with(tab_index=None)

    def test_console_requests(self):
        svc = self._service()
        self._execute(svc, action="console", clear=True)
        svc.console.assert_called_once_with(clear=True, tab_index=None)
        self._execute(svc, action="requests")
        svc.requests.assert_called_once_with(clear=False, tab_index=None)

    def test_missing_required_args(self):
        svc = self._service()
        assert self._execute(svc, action="click").error
        assert self._execute(svc, action="fill", ref="r1").error  # 缺 text
        assert self._execute(svc, action="fill", text="x").error  # 缺 ref
        assert self._execute(svc, action="type", ref="r1").error
        assert self._execute(svc, action="find").error
        assert self._execute(svc, action="press").error
        assert self._execute(svc, action="navigate").error
        # 缺参时不调用 service
        svc.click.assert_not_called()
        svc.fill.assert_not_called()
