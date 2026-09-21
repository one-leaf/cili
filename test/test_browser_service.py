"""Tests for core/browser_service.py — pure logic methods (no Playwright needed)."""

import sys
from unittest.mock import patch, MagicMock

import pytest


class TestFindBrowser:
    """_find_browser() platform-specific path lookup."""

    def test_returns_string_or_none(self):
        from core.browser_service import BrowserService
        service = BrowserService.__new__(BrowserService)
        result = service._find_browser()
        # Returns a path string or None
        assert result is None or isinstance(result, str)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_windows_chrome_path_format(self):
        """On Windows, Chrome paths should be .exe files."""
        from core.browser_service import BrowserService
        service = BrowserService.__new__(BrowserService)
        result = service._find_browser()
        if result is not None:
            assert result.endswith(".exe")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_windows_edge_when_configured(self):
        """When browser_path is set to Edge path, should return that path."""
        from core.browser_service import BrowserService
        service = BrowserService.__new__(BrowserService)
        mock_config = MagicMock()
        # Use actual Edge path on Windows
        edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        import os
        if os.path.exists(edge_path):
            mock_config.system.browser_path = edge_path
            with patch("core.config.load_config", return_value=mock_config):
                result = service._find_browser()
                assert result == edge_path


class TestIsPortListening:
    """_is_port_listening() socket check."""

    def test_returns_false_for_unused_port(self):
        from core.browser_service import BrowserService
        service = BrowserService.__new__(BrowserService)
        # Port 1 is almost certainly not listening
        assert service._is_port_listening(1) is False

    def test_returns_bool(self):
        from core.browser_service import BrowserService
        service = BrowserService.__new__(BrowserService)
        result = service._is_port_listening(65535)
        assert isinstance(result, bool)


class TestBrowserServiceInit:
    """BrowserService initialization."""

    def test_default_state(self):
        from core.browser_service import BrowserService
        service = BrowserService()
        assert service._playwright is None
        assert service._browser is None
        assert service._running is False

    def test_is_running_false_by_default(self):
        from core.browser_service import BrowserService
        service = BrowserService()
        assert service.is_running() is False


class TestGetService:
    """get_service() singleton pattern."""

    def test_returns_same_instance(self):
        from core.browser_service import get_service, _service
        # Reset module state for test isolation
        import core.browser_service as bs_module
        original = bs_module._service
        try:
            bs_module._service = None
            s1 = get_service()
            s2 = get_service()
            assert s1 is s2
        finally:
            bs_module._service = original


class TestDisconnectInternal:
    """_disconnect_internal() resets connection state."""

    def test_resets_references(self):
        from core.browser_service import BrowserService
        service = BrowserService()
        # Pretend we have connections
        service._browser = MagicMock()
        service._context = MagicMock()
        mock_page = MagicMock()
        service._page_pool = {1: (mock_page, 1234567890.0)}
        service._active_tab_index = 1

        service._disconnect_internal()

        assert service._context is None
        assert service._browser is None
        assert service._page_pool == {}
        assert service._active_tab_index is None

    def test_handles_close_error(self):
        """browser.close() failure should not raise."""
        from core.browser_service import BrowserService
        service = BrowserService()
        service._browser = MagicMock()
        service._browser.close.side_effect = Exception("close failed")
        # Should not raise
        service._disconnect_internal()
        assert service._browser is None


class TestStealthAvailable:
    """STEALTH_AVAILABLE flag."""

    def test_is_bool(self):
        from core.browser_service import STEALTH_AVAILABLE
        assert isinstance(STEALTH_AVAILABLE, bool)


class TestDefaultCdpPort:
    """DEFAULT_CDP_PORT constant."""

    def test_port_value(self):
        from core.browser_service import DEFAULT_CDP_PORT
        assert DEFAULT_CDP_PORT == 9222


class TestTabIdleTimeout:
    """TAB_IDLE_TIMEOUT constant."""

    def test_timeout_value(self):
        from core.browser_service import TAB_IDLE_TIMEOUT
        assert TAB_IDLE_TIMEOUT == 600  # 10 minutes


class TestPagePool:
    """Page pool initialization."""

    def test_default_pool_empty(self):
        from core.browser_service import BrowserService
        service = BrowserService()
        assert service._page_pool == {}
        assert service._active_tab_index is None

    def test_next_tab_index_starts_at_1(self):
        from core.browser_service import BrowserService
        service = BrowserService()
        assert service._next_tab_index == 1


class TestGetPage:
    """_get_page() with tab_index parameter."""

    def test_get_page_with_valid_tab_index(self):
        from core.browser_service import BrowserService
        service = BrowserService()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        service._page_pool = {1: (mock_page, 1234567890.0)}

        result = service._get_page(1)
        assert result is mock_page

    def test_get_page_with_invalid_tab_index(self):
        from core.browser_service import BrowserService
        service = BrowserService()
        result = service._get_page(999)
        assert result is None

    def test_get_page_with_none_uses_active(self):
        from core.browser_service import BrowserService
        service = BrowserService()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        service._page_pool = {1: (mock_page, 1234567890.0)}
        service._active_tab_index = 1

        result = service._get_page(None)
        assert result is mock_page

    def test_get_page_with_none_no_active(self):
        from core.browser_service import BrowserService
        service = BrowserService()
        result = service._get_page(None)
        assert result is None


class TestClosePageInternal:
    """_close_page_internal() removes from pool and closes page."""

    def test_removes_from_pool(self):
        from core.browser_service import BrowserService
        service = BrowserService()
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        service._page_pool = {1: (mock_page, 1234567890.0)}
        service._active_tab_index = 1

        service._close_page_internal(1, mock_page)

        assert 1 not in service._page_pool
        assert service._active_tab_index is None
        mock_page.close.assert_called_once()


class TestValidateNavigateUrl:
    """_validate_navigate_url() scheme/SSRF 过滤（A30/SEC-17）。"""

    def _validate(self, url):
        from core.browser_service import _validate_navigate_url
        return _validate_navigate_url(url)

    def test_public_http_https_allowed(self):
        assert self._validate("https://example.com/page") is None
        assert self._validate("http://example.com") is None

    def test_file_scheme_rejected(self):
        reason = self._validate("file:///etc/passwd")
        assert reason and "scheme" in reason

    def test_data_scheme_rejected(self):
        reason = self._validate("data:text/html,<script>alert(1)</script>")
        assert reason and "scheme" in reason

    def test_javascript_scheme_rejected(self):
        reason = self._validate("javascript:alert(1)")
        assert reason and "scheme" in reason

    def test_empty_scheme_rejected(self):
        reason = self._validate("example.com")
        assert reason and "scheme" in reason

    def test_loopback_ip_rejected(self):
        reason = self._validate("http://127.0.0.1:9222/")
        assert reason and "SSRF" in reason

    def test_localhost_rejected(self):
        reason = self._validate("http://localhost:8080/")
        assert reason and "SSRF" in reason

    def test_private_ip_rejected(self):
        reason = self._validate("http://192.168.1.5/")
        assert reason and "SSRF" in reason

    def test_link_local_metadata_rejected(self):
        reason = self._validate("http://169.254.169.254/latest/meta-data/")
        assert reason and "SSRF" in reason

    def test_ipv6_loopback_rejected(self):
        reason = self._validate("http://[::1]/")
        assert reason and "SSRF" in reason

    def test_missing_hostname_rejected(self):
        reason = self._validate("https:///path")
        assert reason and "主机名" in reason


class TestCdpPortFallback:
    """W11/W12: CDP 端口占用/冲突处理（不启动真实 Chrome）。"""

    def test_cdp_port_defaults_to_9222(self):
        from core.browser_service import BrowserService, DEFAULT_CDP_PORT
        service = BrowserService()
        assert service._cdp_port == DEFAULT_CDP_PORT

    def test_pick_free_port_returns_unlistening_port(self):
        from core.browser_service import BrowserService
        service = BrowserService()
        with patch.object(service, "_is_port_listening", return_value=False):
            port = service._pick_free_port()
        assert 9300 <= port <= 9900

    def test_find_pid_by_port_parses_netstat(self):
        """从 netstat 输出解析出监听端口的 PID（Windows）。"""
        from core.browser_service import BrowserService
        service = BrowserService()
        fake_output = (
            "  TCP    127.0.0.1:9222    0.0.0.0:0    LISTENING    4321\n"
            "  TCP    127.0.0.1:8080    0.0.0.0:0    LISTENING    1234\n"
        )
        with patch("core.browser_service.sys.platform", "win32"), \
             patch("core.browser_service.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=fake_output, text=True)
            assert service._find_pid_by_port(9222) == 4321
            assert service._find_pid_by_port(9999) is None

    def test_pid_uses_our_profile_true_when_matching(self):
        """进程命令行包含本项目 profile 时判定为自有 Chrome。"""
        from core.browser_service import BrowserService
        service = BrowserService()
        profile = service._chrome_profile_dir.replace("/", "\\").lower()
        cmdline = f'"C:\\fake\\chrome.exe" --user-data-dir={profile}'
        with patch("core.browser_service.sys.platform", "win32"), \
             patch("core.browser_service.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=cmdline, text=True)
            assert service._pid_uses_our_profile(4321) is True

    def test_pid_uses_our_profile_false_when_foreign(self):
        """进程命令行是用户个人 Chrome profile 时判定为外部进程。"""
        from core.browser_service import BrowserService
        service = BrowserService()
        cmdline = (
            '"C:\\Users\\me\\chrome.exe" '
            '--user-data-dir=C:\\Users\\me\\AppData\\Local\\Google\\Chrome\\User Data'
        )
        with patch("core.browser_service.sys.platform", "win32"), \
             patch("core.browser_service.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=cmdline, text=True)
            assert service._pid_uses_our_profile(4321) is False

    def test_start_chrome_foreign_chrome_switches_to_random_port(self, tmp_path):
        """W12: 端口上是非本项目 Chrome（foreign profile）时，切换到随机端口启动自己的 Chrome。"""
        from core.browser_service import BrowserService
        service = BrowserService()
        service._chrome_process = None
        service._cdp_port = 9222
        service._project_root = str(tmp_path)  # 重定向 profile 目录，避免动真实 data/deps/browser

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # 进程存活

        with patch.object(service, "_is_port_listening", return_value=True), \
             patch.object(service, "_try_cdp_connect", return_value=True), \
             patch.object(service, "_find_chrome_process_by_profile", return_value=None), \
             patch.object(service, "_pick_free_port", return_value=9500), \
             patch.object(service, "_find_browser", return_value="C:\\fake\\chrome.exe"), \
             patch("core.browser_service.subprocess.Popen", return_value=mock_proc), \
             patch("core.browser_service.time.sleep"):
            success, _msg = service._start_chrome(9222)

        assert success is True
        assert service._cdp_port == 9500
        assert service._chrome_process is mock_proc

    def test_start_chrome_kills_stale_own_chrome_on_cdp_failure(self):
        """W11: 端口被本项目 profile 的旧 Chrome 占用且 CDP 连不上时，记录 PID 并 kill。"""
        from core.browser_service import BrowserService
        service = BrowserService()
        service._chrome_process = None
        service._cdp_port = 9222

        with patch.object(service, "_is_port_listening", side_effect=[True, False]), \
             patch.object(service, "_try_cdp_connect", return_value=False), \
             patch.object(service, "_find_pid_by_port", return_value=4321), \
             patch.object(service, "_pid_uses_our_profile", return_value=True), \
             patch.object(service, "_find_browser", return_value=None), \
             patch.object(service, "_kill_chrome_internal") as mock_kill:
            service._start_chrome(9222)

        assert mock_kill.call_count == 1
        assert service._chrome_process is not None
        assert service._chrome_process.pid == 4321

    def test_start_chrome_foreign_process_not_killed_switches_port(self):
        """W11: 端口被非本项目进程占用且 CDP 连不上时，不误杀，改用随机端口。"""
        from core.browser_service import BrowserService
        service = BrowserService()
        service._chrome_process = None
        service._cdp_port = 9222

        with patch.object(service, "_is_port_listening", return_value=True), \
             patch.object(service, "_try_cdp_connect", return_value=False), \
             patch.object(service, "_find_pid_by_port", return_value=9999), \
             patch.object(service, "_pid_uses_our_profile", return_value=False), \
             patch.object(service, "_pick_free_port", return_value=9500), \
             patch.object(service, "_find_browser", return_value=None), \
             patch.object(service, "_kill_chrome_internal") as mock_kill:
            service._start_chrome(9222)

        assert mock_kill.call_count == 0
        assert service._cdp_port == 9500


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
        service._project_root = str(tmp_path)
        return service

    @staticmethod
    def _run(service, method, page, **kwargs):
        from core.tools.base import ToolResult  # noqa: F401 (side effect 用)

        def fake_execute(op_name, func, tab_index=None):
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
        page = MagicMock()
        page.evaluate.return_value = ["a", "b"]
        result = self._run(service, "extract", page, selector=".item", attribute="text", limit=5)
        assert not result.error
        assert "Extracted 2 element(s)" in result.output
        assert "1. a" in result.output
        # evaluate 收到 JS 函数 + 三个参数
        args = page.evaluate.call_args.args
        assert args[1:] == (".item", "text", 5)

    def test_extract_no_match(self):
        service = self._service()
        page = MagicMock()
        page.evaluate.return_value = []
        result = self._run(service, "extract", page, selector=".nope")
        assert not result.error
        assert "No elements match" in result.output

    def test_extract_error(self):
        service = self._service()
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("boom")
        result = self._run(service, "extract", page, selector=".item")
        assert result.error

    def test_extract_table(self):
        service = self._service()
        page = MagicMock()
        page.evaluate.return_value = [["Name", "Age"], ["Alice", "30"]]
        result = self._run(service, "extract_table", page, index=0)
        assert not result.error
        assert "| Name | Age |" in result.output
        assert "| Alice | 30 |" in result.output
        assert "1 data rows" in result.output

    def test_extract_table_no_rows(self):
        service = self._service()
        page = MagicMock()
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
        from core.browser_service import _BODY_MAX_CHARS
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

    # ─── navigate SSRF 预检 / skip 参数 ───────────────────────────

    def test_navigate_private_rejects_without_skip(self):
        from core.tools.base import ToolResult
        service = self._service()
        service._run_in_worker = MagicMock(return_value=ToolResult("should not run"))
        result = service.navigate("http://127.0.0.1:8885/tcmp-war/")
        assert result.error
        service._run_in_worker.assert_not_called()

    def test_navigate_public_ok(self):
        from core.tools.base import ToolResult
        service = self._service()
        service._run_in_worker = MagicMock(return_value=ToolResult("navigated"))
        result = service.navigate("https://example.com")
        assert result.output == "navigated"
        service._run_in_worker.assert_called_once()

    def test_navigate_skip_ssrf_bypasses_validation(self):
        from core.tools.base import ToolResult
        service = self._service()
        service._run_in_worker = MagicMock(return_value=ToolResult("navigated"))
        with patch("core.browser_service._validate_navigate_url") as mv:
            mv.return_value = "环回地址"
            result = service.navigate("http://127.0.0.1:8885/tcmp-war/", skip_ssrf=True)
        assert result.output == "navigated"
        mv.assert_not_called()
