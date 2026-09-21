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
        svc.snapshot.assert_called_once_with(tab_index=2, frame=None)

    def test_find(self):
        svc = self._service()
        self._execute(svc, action="find", pattern="Sign in")
        svc.find.assert_called_once_with("Sign in", tab_index=None, frame=None)

    def test_click(self):
        svc = self._service()
        self._execute(svc, action="click", ref="r4")
        svc.click.assert_called_once_with("r4", button="left", tab_index=None, frame=None)

    def test_click_right_button(self):
        svc = self._service()
        self._execute(svc, action="click", ref="r4", button="right")
        svc.click.assert_called_once_with("r4", button="right", tab_index=None, frame=None)

    def test_fill(self):
        svc = self._service()
        self._execute(svc, action="fill", ref="r6", text="hello")
        svc.fill.assert_called_once_with("r6", "hello", tab_index=None, frame=None)

    def test_type(self):
        svc = self._service()
        self._execute(svc, action="type", ref="r6", text="hi")
        svc.type.assert_called_once_with("r6", "hi", tab_index=None, frame=None)

    def test_press_with_ref(self):
        svc = self._service()
        self._execute(svc, action="press", ref="r3", key="Enter")
        svc.press.assert_called_once_with("r3", "Enter", tab_index=None, frame=None)

    def test_press_page_level(self):
        svc = self._service()
        self._execute(svc, action="press", key="Escape")
        svc.press.assert_called_once_with(None, "Escape", tab_index=None, frame=None)

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
        svc.requests.assert_called_once_with(clear=False, details=False, body=False, tab_index=None)

    def test_missing_required_args(self):
        svc = self._service()
        assert self._execute(svc, action="click").error
        assert self._execute(svc, action="fill", ref="r1").error  # 缺 text
        assert self._execute(svc, action="fill", text="x").error  # 缺 ref
        assert self._execute(svc, action="type", ref="r1").error
        assert self._execute(svc, action="find").error
        assert self._execute(svc, action="press").error
        assert self._execute(svc, action="navigate").error
        assert self._execute(svc, action="hover").error
        assert self._execute(svc, action="drag", ref="r1").error
        assert self._execute(svc, action="upload", ref="r1").error
        assert self._execute(svc, action="fill_form").error
        assert self._execute(svc, action="extract").error
        assert self._execute(svc, action="download").error
        assert self._execute(svc, action="set_cookie").error
        # 缺参时不调用 service
        svc.click.assert_not_called()
        svc.fill.assert_not_called()


class TestEnhancedDispatch:
    """新增动作的分发转发：参数映射 + 缺参校验 + navigate 审批门。"""

    def _service(self):
        svc = MagicMock()
        svc.is_running.return_value = True
        return svc

    def _execute(self, svc, **kwargs):
        from core.tools.browser import BrowserTool
        with patch("core.browser_service.get_service", return_value=svc):
            return BrowserTool().execute(**kwargs)

    def test_scroll(self):
        svc = self._service()
        self._execute(svc, action="scroll", direction="down", amount=400)
        svc.scroll.assert_called_once_with(direction="down", amount=400, ref=None, frame=None, tab_index=None)

    def test_scroll_default_direction(self):
        svc = self._service()
        self._execute(svc, action="scroll")
        svc.scroll.assert_called_once_with(direction="down", amount=800, ref=None, frame=None, tab_index=None)

    def test_hover(self):
        svc = self._service()
        self._execute(svc, action="hover", ref="r1")
        svc.hover.assert_called_once_with("r1", frame=None, tab_index=None)

    def test_drag(self):
        svc = self._service()
        self._execute(svc, action="drag", ref="r1", target_ref="r2")
        svc.drag.assert_called_once_with("r1", "r2", frame=None, tab_index=None)

    def test_upload_resolves_relative_path(self):
        import os
        svc = self._service()
        self._execute(svc, action="upload", ref="r1", path="a.txt")
        args = svc.upload.call_args.args
        assert args[0] == "r1"
        assert os.path.isabs(args[1]) and args[1].endswith("a.txt")
        assert svc.upload.call_args.kwargs == {"frame": None, "tab_index": None}

    def test_fill_form(self):
        svc = self._service()
        fields = [{"ref": "r1", "value": "x"}, {"ref": "r2", "value": "y"}]
        self._execute(svc, action="fill_form", fields=fields)
        svc.fill_form.assert_called_once_with(fields, frame=None, tab_index=None)

    def test_extract(self):
        svc = self._service()
        self._execute(svc, action="extract", selector=".item", attribute="href", limit=5)
        svc.extract.assert_called_once_with(".item", attribute="href", limit=5, frame=None, tab_index=None)

    def test_extract_table(self):
        svc = self._service()
        self._execute(svc, action="extract_table", index=1)
        svc.extract_table.assert_called_once_with(index=1, frame=None, tab_index=None)

    def test_download_with_ref(self):
        import os
        svc = self._service()
        self._execute(svc, action="download", ref="r1", save_path="dl.bin")
        assert svc.download.call_args.kwargs["ref"] == "r1"
        assert svc.download.call_args.kwargs["url"] is None
        assert svc.download.call_args.kwargs["save_path"].endswith("dl.bin")
        assert svc.download.call_args.kwargs["tab_index"] is None

    def test_dialogs(self):
        svc = self._service()
        self._execute(svc, action="dialogs", clear=True)
        svc.dialogs.assert_called_once_with(clear=True, tab_index=None)

    def test_get_frames(self):
        svc = self._service()
        self._execute(svc, action="get_frames")
        svc.get_frames.assert_called_once_with(tab_index=None)

    def test_cookies_actions(self):
        svc = self._service()
        self._execute(svc, action="get_cookies", url="https://example.com")
        svc.get_cookies.assert_called_once_with(url="https://example.com", tab_index=None)
        self._execute(svc, action="clear_cookies")
        svc.clear_cookies.assert_called_once_with(tab_index=None)
        cookie = {"name": "a", "value": "b", "domain": "example.com"}
        self._execute(svc, action="set_cookie", cookie=cookie)
        svc.set_cookie.assert_called_once_with(cookie, tab_index=None)

    def test_frame_passed_to_actions(self):
        svc = self._service()
        self._execute(svc, action="click", ref="r1", frame=2)
        svc.click.assert_called_once_with("r1", button="left", tab_index=None, frame=2)
        self._execute(svc, action="snapshot", frame="1")
        svc.snapshot.assert_called_once_with(tab_index=None, frame=1)

    def test_navigate_public_url_no_gate(self):
        svc = self._service()
        self._execute(svc, action="navigate", url="https://example.com")
        svc.navigate.assert_called_once_with("https://example.com", tab_index=None)

    def test_navigate_private_url_placeholder(self):
        from core.tools.approval import META_KEY
        svc = self._service()
        result = self._execute(svc, action="navigate", url="http://127.0.0.1:8885/tcmp-war/")
        assert result.completed is False
        assert not result.error
        assert META_KEY in result.meta
        assert result.meta[META_KEY]["kind"] == "browser:navigate"
        assert result.meta[META_KEY]["command"] == "http://127.0.0.1:8885/tcmp-war/"
        svc.navigate.assert_not_called()

    def test_navigate_private_url_approved_bypasses(self):
        from core.tools.approval import ApprovalStore, approval_decision_id
        from core.tools.browser import BrowserTool
        url = "http://127.0.0.1:8885/tcmp-war/"
        store = ApprovalStore()
        store.approve(approval_decision_id(url), url, kind="browser:navigate")
        svc = self._service()
        with patch("core.browser_service.get_service", return_value=svc):
            BrowserTool(approval_store=store).execute(action="navigate", url=url)
        svc.navigate.assert_called_once_with(url, tab_index=None, skip_ssrf=True)

    def test_navigate_private_url_no_store_hard_placeholder(self):
        """无 approval_store（独立/worker 无共享实例）时仍返回占位而非放行。"""
        svc = self._service()
        result = self._execute(svc, action="navigate", url="http://192.168.1.5/")
        assert result.completed is False
        svc.navigate.assert_not_called()

    def test_create_tools_wires_approval_store_to_browser(self):
        """回归：create_tools 必须把共享 approval_store 注入 BrowserTool。

        曾因 registry 中 browser 工厂缺 needs_approval=True，导致工具侧
        self.approval_store 恒为 None，即使规则已写入 approvals.json，
        navigate 门也永远返回审批占位（用户批准后反复弹卡）。
        """
        from core.agent_config import load_agent_role
        from core.tools.approval import ApprovalStore, approval_decision_id
        from core.tools.registry import create_tools
        url = "http://127.0.0.1:8000/"
        store = ApprovalStore()
        store.approve(approval_decision_id(url), url, kind="browser:navigate")
        tools = create_tools(
            load_agent_role("master"),
            cwd=".", workspace_uuid="d1b45267",
            config=None, approval_store=store,
        )
        bt = next(t for t in tools if t.name == "browser")
        assert bt.approval_store is store
        svc = self._service()
        with patch("core.browser_service.get_service", return_value=svc):
            bt.execute(action="navigate", url=url)
        svc.navigate.assert_called_once_with(url, tab_index=None, skip_ssrf=True)

