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
