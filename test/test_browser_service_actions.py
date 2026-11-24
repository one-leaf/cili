"""浏览器服务动作单元测试：mock page + fake _execute_operation 验证内部逻辑。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.tools.base import ToolResult


def _strict_evaluate_page() -> MagicMock:
    """构造 evaluate 带 Playwright 真实签名的 mock page（多传位置参数会 TypeError）。"""
    page = MagicMock()

    def _evaluate(expression, arg=None):
        return page.evaluate.return_value

    page.evaluate = MagicMock(side_effect=_evaluate)
    return page


class TestEnhancedActions:
    """新增服务动作单元测试：patch _execute_operation 直接执行内部 func(mock page)。

    每个方法经 _execute_operation(name, func, tab_index) 分发；这里用 fake 版本
    直接调用 func(page)，验证内部逻辑与 Playwright 调用参数。
    """

    @staticmethod
    def _service(tmp_path="C:\\proj"):
        from core.browser_service import BrowserService
        service = BrowserService.__new__(BrowserService)
        service._snapshot_refs = {}
        service._page_pool = {}
        service._active_tab_index = 0
        service._dialog_buffers = {}
        service._request_buffers = {}
        service._console_buffers = {}
        service._listener_pages = set()
        service._project_root = str(tmp_path)
        service._free_tab_indices = []
        service._next_tab_index = 1
        return service

    @staticmethod
    def _run(service, method, page, **kwargs):

        def fake_execute(op_name, func, tab_index=None, timeout=None):
            return func(page)

        with patch.object(service, "_execute_operation", side_effect=fake_execute):
            return getattr(service, method)(**kwargs)

    @staticmethod
    def _ref(role="button", name="Go", index=0):
        return {"role": role, "name": name, "index": index, "mode": "role"}

    def _ref_page(self, page, *entries):
        self._snapshot_refs[(0, 0)] = dict(entries)

    # ─── scroll ──────────────────────────────────────────────────

    def test_scroll_down_evaluate(self):
        service = self._service()
        page = MagicMock()
        result = self._run(service, "scroll", page, direction="down", amount=300)
        assert not result.error
        assert "Scrolled down by 300px" in result.output
        page.evaluate.assert_called_once_with("window.scrollBy(0, 300)")

    def test_scroll_up_negative(self):
        service = self._service()
        page = MagicMock()
        self._run(service, "scroll", page, direction="up", amount=500)
        page.evaluate.assert_called_once_with("window.scrollBy(0, -500)")

    def test_scroll_top_bottom(self):
        service = self._service()
        page = MagicMock()
        self._run(service, "scroll", page, direction="top")
        page.evaluate.assert_called_once_with("window.scrollTo(0, 0)")
        page.reset_mock()
        self._run(service, "scroll", page, direction="bottom")
        page.evaluate.assert_called_once_with("window.scrollTo(0, document.body.scrollHeight)")

    def test_scroll_to_element(self):
        service = self._service()
        page = MagicMock()
        service._snapshot_refs[(0, 0)] = {"r1": self._ref()}
        result = self._run(service, "scroll", page, direction="to_element", ref="r1")
        assert not result.error
        assert "into view" in result.output
        page.get_by_role.return_value.nth.return_value.scroll_into_view_if_needed.assert_called_once()

    def test_scroll_to_element_missing_ref(self):
        service = self._service()
        page = MagicMock()
        result = self._run(service, "scroll", page, direction="to_element")
        assert result.error

    def test_scroll_unknown_direction(self):
        service = self._service()
        page = MagicMock()
        result = self._run(service, "scroll", page, direction="sideways")
        assert result.error

    # ─── hover / drag / upload / fill_form ─────────────────────────

    def test_hover(self):
        service = self._service()
        page = MagicMock()
        service._snapshot_refs[(0, 0)] = {"r1": self._ref()}
        result = self._run(service, "hover", page, ref="r1")
        assert not result.error
        assert "Hovered [r1]" in result.output
        page.get_by_role.return_value.nth.return_value.hover.assert_called_once()

    def test_drag(self):
        service = self._service()
        page = MagicMock()
        service._snapshot_refs[(0, 0)] = {"r1": self._ref(index=0), "r2": self._ref(name="Drop", index=1)}
        result = self._run(service, "drag", page, ref="r1", target_ref="r2")
        assert not result.error
        assert "Dragged [r1]" in result.output
        # 源/目标各自解析一次 role locator
        assert page.get_by_role.call_count == 2
        page.get_by_role.return_value.nth.return_value.drag_to.assert_called_once()

    def test_upload(self):
        from core.browser_service import _ACTION_TIMEOUT_MS
        service = self._service()
        page = MagicMock()
        service._snapshot_refs[(0, 0)] = {"r1": self._ref()}
        result = self._run(service, "upload", page, ref="r1", path="C:\\f\\a.txt")
        assert not result.error
        assert "a.txt" in result.output
        page.get_by_role.return_value.nth.return_value.set_input_files.assert_called_once_with(
            "C:\\f\\a.txt", timeout=_ACTION_TIMEOUT_MS
        )

    def test_fill_form(self):
        service = self._service()
        page = MagicMock()
        service._snapshot_refs[(0, 0)] = {
            "r1": self._ref(index=0), "r2": self._ref(name="Name", index=1),
        }
        result = self._run(service, "fill_form", page, fields=[
            {"ref": "r1", "value": "x"}, {"ref": "r2", "value": "y"},
        ])
        assert not result.error
        assert "Fill form (2 fields)" in result.output
        assert page.get_by_role.return_value.nth.return_value.fill.call_count == 2

    def test_fill_form_skips_missing_value(self):
        service = self._service()
        page = MagicMock()
        service._snapshot_refs[(0, 0)] = {"r1": self._ref()}
        result = self._run(service, "fill_form", page, fields=[{"ref": "r1"}, {"value": "no-ref"}])
        assert "skipped" in result.output
        page.get_by_role.return_value.nth.return_value.fill.assert_not_called()

    # ─── extract / extract_table ──────────────────────────────────

    def test_extract(self):
        service = self._service()
        page = _strict_evaluate_page()
        page.evaluate.return_value = ["a", "b"]
        result = self._run(service, "extract", page, selector=".item", attribute="text", limit=5)
        assert not result.error
        assert "Extracted 2 element(s)" in result.output
        assert "1. a" in result.output
        # evaluate 只收 1 个 arg：JS 函数 + 打包好的参数对象
        assert page.evaluate.call_args.args[1] == {"sel": ".item", "attr": "text", "limit": 5}

    def test_extract_no_match(self):
        service = self._service()
        page = _strict_evaluate_page()
        page.evaluate.return_value = []
        result = self._run(service, "extract", page, selector=".nope")
        assert not result.error
        assert "No elements match" in result.output

    def test_extract_error(self):
        service = self._service()
        page = _strict_evaluate_page()
        page.evaluate.side_effect = RuntimeError("boom")
        result = self._run(service, "extract", page, selector=".item")
        assert result.error

    def test_extract_table(self):
        service = self._service()
        page = _strict_evaluate_page()
        page.evaluate.return_value = [["Name", "Age"], ["Alice", "30"]]
        result = self._run(service, "extract_table", page, index=0)
        assert not result.error
        assert "| Name | Age |" in result.output
        assert "| Alice | 30 |" in result.output
        assert "1 data rows" in result.output

    def test_extract_table_no_rows(self):
        service = self._service()
        page = _strict_evaluate_page()
        page.evaluate.return_value = []
        result = self._run(service, "extract_table", page)
        assert not result.error
        assert "No <table> elements" in result.output

    # ─── dialogs ──────────────────────────────────────────────────

    def test_dialogs_list_and_clear(self):
        from collections import deque
        service = self._service()
        page = MagicMock()
        service._dialog_buffers[0] = deque([("alert", "hello"), ("confirm", "go?")])
        result = self._run(service, "dialogs", page)
        assert "[alert] hello" in result.output
        assert len(service._dialog_buffers[0]) == 2  # 未 clear 不清空
        self._run(service, "dialogs", page, clear=True)
        assert len(service._dialog_buffers[0]) == 0

    def test_dialogs_none(self):
        service = self._service()
        page = MagicMock()
        result = self._run(service, "dialogs", page)
        assert "No dialogs captured yet." in result.output

    def test_handle_dialog_dismisses(self):
        from collections import deque
        service = self._service()
        service._dialog_buffers[0] = deque()
        dlg = MagicMock()
        dlg.type = "alert"
        dlg.message = "Hi"
        service._handle_dialog(0, dlg)
        assert service._dialog_buffers[0][0] == ("alert", "Hi")
        dlg.dismiss.assert_called_once()
        dlg.accept.assert_not_called()

    # ─── download ─────────────────────────────────────────────────

    def _download_cm(self, filename="file.bin"):
        dl = MagicMock()
        dl.suggested_filename = filename
        cm = MagicMock()
        cm.__enter__.return_value.value = dl
        return cm, dl

    def test_download_by_ref(self, tmp_path):
        service = self._service(tmp_path)
        page = MagicMock()
        cm, dl = self._download_cm()
        page.expect_download.return_value = cm
        service._snapshot_refs[(0, 0)] = {"r1": self._ref()}
        result = self._run(service, "download", page, ref="r1", save_path="out.bin")
        assert not result.error
        expected = str(tmp_path / "out.bin")
        dl.save_as.assert_called_once_with(expected)
        assert "Downloaded 'file.bin'" in result.output
        # 点击触发下载
        page.get_by_role.return_value.nth.return_value.click.assert_called_once()

    def test_download_by_url(self, tmp_path):
        service = self._service(tmp_path)
        page = MagicMock()
        cm, dl = self._download_cm("x.zip")
        page.expect_download.return_value = cm
        result = self._run(service, "download", page, url="https://x/y.zip", save_path="dl")
        assert not result.error
        page.goto.assert_called_once()
        assert str(tmp_path / "dl") in result.output

    def test_download_default_path_uses_suggested(self, tmp_path):
        service = self._service(tmp_path)
        page = MagicMock()
        cm, dl = self._download_cm("sug.pdf")
        page.expect_download.return_value = cm
        result = self._run(service, "download", page, url="https://x/sug.pdf")
        assert not result.error
        assert str(tmp_path / "sug.pdf") in result.output

    def test_download_requires_ref_or_url(self):
        service = self._service()
        page = MagicMock()
        result = self._run(service, "download", page)
        assert result.error

    # ─── frames / cookies ─────────────────────────────────────────

    def test_get_frames(self):
        service = self._service()
        page = MagicMock()
        f0 = MagicMock()
        f0.name = ""
        f0.url = "https://main/"
        f1 = MagicMock()
        f1.name = "widget"
        f1.url = "https://main/w.html"
        page.frames = [f0, f1]
        result = self._run(service, "get_frames", page)
        assert not result.error
        assert "frame 0" in result.output and "[MAIN]" in result.output
        assert "frame 1: widget" in result.output

    def test_get_cookies(self):
        service = self._service()
        page = MagicMock()
        page.context.cookies.return_value = [
            {"name": "sid", "value": "abc", "domain": ".ex.com", "path": "/",
             "secure": True, "httpOnly": True, "sameSite": "Lax"}
        ]
        result = self._run(service, "get_cookies", page)
        assert "sid = abc" in result.output
        page.context.cookies.assert_called_once_with()
        self._run(service, "get_cookies", page, url="https://ex.com/")
        page.context.cookies.assert_called_with(url="https://ex.com/")

    def test_clear_cookies(self):
        service = self._service()
        page = MagicMock()
        result = self._run(service, "clear_cookies", page)
        assert "All cookies cleared" in result.output
        page.context.clear_cookies.assert_called_once()

    def test_set_cookie(self):
        service = self._service()
        page = MagicMock()
        cookie = {"name": "a", "value": "b", "domain": "ex.com"}
        result = self._run(service, "set_cookie", page, cookie=cookie)
        assert "Cookie set: a" in result.output
        page.context.add_cookies.assert_called_once_with([cookie])

    # ─── requests dict 格式 ───────────────────────────────────────

    def test_requests_summary(self):
        from collections import deque
        service = self._service()
        page = MagicMock()
        service._request_buffers[0] = deque([
            {"method": "GET", "url": "https://x/a", "status": 200},
        ])
        result = self._run(service, "requests", page)
        assert "GET https://x/a 200" in result.output

    def test_requests_details_and_body(self):
        from collections import deque
        service = self._service()
        page = MagicMock()
        service._request_buffers[0] = deque([{
            "method": "POST", "url": "https://x/b", "status": 201,
            "req_headers": {"user-agent": "u"},
            "resp_headers": {"content-type": "text/plain"},
            "body_text": "hello body",
        }])
        result = self._run(service, "requests", page, details=True, body=True)
        assert "POST https://x/b 201" in result.output
        assert "req headers" in result.output and "user-agent" in result.output
        assert "resp headers" in result.output
        assert "body: hello body" in result.output

    def test_requests_clear_empties_buffer(self):
        from collections import deque
        service = self._service()
        page = MagicMock()
        service._request_buffers[0] = deque([{"method": "GET", "url": "u", "status": 200}])
        self._run(service, "requests", page, clear=True)
        assert len(service._request_buffers[0]) == 0

    def test_capture_response_text_body(self):
        from collections import deque
        service = self._service()
        buf = deque()
        resp = MagicMock()
        resp.request.method = "POST"
        resp.url = "https://x/a"
        resp.status = 200
        resp.request.headers = {"user-agent": "u"}
        resp.headers = {"content-type": "text/html; charset=utf-8", "content-length": "100"}
        resp.body.return_value = b"<p>hi</p>"
        service._capture_response(buf, resp)
        entry = buf[0]
        assert entry["method"] == "POST"
        assert entry["url"] == "https://x/a"
        assert entry["status"] == 200
        assert entry["req_headers"] == {"user-agent": "u"}
        assert entry["body_text"] == "<p>hi</p>"

    def test_capture_response_binary_skips_body(self):
        from collections import deque

        service = self._service()
        buf = deque()
        resp = MagicMock()
        resp.request.method = "GET"
        resp.url = "u"
        resp.status = 200
        resp.request.headers = {}
        resp.headers = {"content-type": "application/octet-stream", "content-length": "100"}
        service._capture_response(buf, resp)
        assert "body_text" not in buf[0]

    def test_capture_response_large_body_skipped(self):
        from collections import deque

        from core.browser_service import _BODY_MAX_CHARS
        service = self._service()
        buf = deque()
        resp = MagicMock()
        resp.request.method = "GET"
        resp.url = "u"
        resp.status = 200
        resp.request.headers = {}
        resp.headers = {"content-type": "text/html", "content-length": str(_BODY_MAX_CHARS * 4 + 1)}
        service._capture_response(buf, resp)
        assert "body_text" not in buf[0]
        resp.body.assert_not_called()

    # ─── frame 解析 / ref 双键 / tab 清理 ─────────────────────────

    def test_resolve_frame(self):
        service = self._service()
        page = MagicMock()
        f0, f1 = MagicMock(), MagicMock()
        page.frames = [f0, f1]
        assert service._resolve_frame(page, None) is page
        assert service._resolve_frame(page, 0) is f0
        assert service._resolve_frame(page, 1) is f1

    def test_resolve_frame_out_of_range(self):
        service = self._service()
        page = MagicMock()
        page.frames = [MagicMock()]
        with pytest.raises(ValueError):
            service._resolve_frame(page, 5)

    def test_ref_key_normalizes_none_to_zero(self):
        service = self._service()
        assert service._ref_key(2, None) == (2, 0)
        assert service._ref_key(2, 0) == (2, 0)
        assert service._ref_key(2, 1) == (2, 1)

    def test_frame_aware_snapshot_resolution(self):
        """ref 映射按 (tab, frame) 双键隔离：同 ref 不同 frame 不串扰。"""
        service = self._service()
        page = MagicMock()
        frame = MagicMock()
        page.frames = [page, frame]  # frame 0 = page 自身（mock 简化）
        # 主 frame snapshot 记录 r1
        base = service._resolve_frame(page, None)
        base.locator("body").aria_snapshot.return_value = '- button "Main"'
        service._build_snapshot(page, 0, None)
        # frame 1 snapshot 记录同名 r1（不同条目）
        frame.locator("body").aria_snapshot.return_value = '- button "InFrame"'
        service._build_snapshot(page, 0, 1)
        assert service._resolve_ref(0, "r1")["name"] == "Main"
        assert service._resolve_ref(0, "r1", frame=1)["name"] == "InFrame"

    def test_close_page_cleans_per_tab_state(self):
        from collections import deque
        service = self._service()
        page = MagicMock()
        page.is_closed.return_value = True
        service._snapshot_refs = {(1, 0): {"r1": self._ref()}, (1, 2): {}, (0, 0): {}}
        service._console_buffers = {1: deque(), 0: deque()}
        service._request_buffers = {1: deque(), 0: deque()}
        service._dialog_buffers = {1: deque(), 0: deque()}
        service._page_pool = {1: (page, 0.0)}
        service._listener_pages = {id(page)}
        service._active_tab_index = 1
        service._close_page_internal(1, page)
        assert all(k[0] != 1 for k in service._snapshot_refs)
        assert 1 not in service._dialog_buffers
        assert 1 not in service._console_buffers
        assert 1 not in service._request_buffers
        assert service._active_tab_index is None

    def test_closed_tab_index_is_reused(self):
        """关闭的 tab 编号归还并复用于新 tab（编号不再只增不减）。"""
        service = self._service()
        page = MagicMock()
        page.is_closed.return_value = True
        service._page_pool = {3: (page, 0.0)}
        service._listener_pages = {id(page)}
        service._active_tab_index = 3
        service._close_page_internal(3, page)
        assert 3 in service._free_tab_indices

        service._context = MagicMock()
        service._context.new_page.return_value = MagicMock()
        service._touch_page = MagicMock()
        assert service._open_new_page() == 3
        assert service._free_tab_indices == []

    # ─── navigate SSRF 预检 / skip 参数 ───────────────────────────

    def test_navigate_private_rejects_without_skip(self):
        service = self._service()
        service._run_in_worker = MagicMock(return_value=ToolResult("should not run"))
        result = service.navigate("http://127.0.0.1:8885/tcmp-war/")
        assert result.error
        service._run_in_worker.assert_not_called()

    def test_navigate_public_ok(self):
        service = self._service()
        service._run_in_worker = MagicMock(return_value=ToolResult("navigated"))
        result = service.navigate("https://example.com")
        assert result.output == "navigated"
        service._run_in_worker.assert_called_once()

    def test_navigate_approved_non_public_allows_private(self):
        """已批准的非公网导航：跳过私网判定并真正执行。"""
        service = self._service()
        service._run_in_worker = MagicMock(return_value=ToolResult("navigated"))
        result = service.navigate("http://127.0.0.1:8885/tcmp-war/", approved_non_public=True)
        assert result.output == "navigated"
        service._run_in_worker.assert_called_once()

    def test_navigate_approved_still_blocks_hard_deny(self):
        """回归：用户批准不得连带绕过 scheme 白名单与云元数据端点。

        此前 skip_ssrf=True 跳过的是整段校验（含 scheme），批准一个
        file:///... 即可让 Playwright 读本地文件。
        """
        service = self._service()
        service._run_in_worker = MagicMock(return_value=ToolResult("navigated"))
        for url in ("file:///etc/passwd", "http://169.254.169.254/latest/meta-data/"):
            result = service.navigate(url, approved_non_public=True)
            assert result.error
        service._run_in_worker.assert_not_called()
