"""Tests for core/browser_service.py — pure logic methods (no Playwright needed)."""

import sys
from unittest.mock import MagicMock, patch

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
        # Reset module state for test isolation
        import core.browser_service as bs_module
        from core.browser_service import get_service
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
    """check_navigate_url() scheme/SSRF 过滤（A30/SEC-17）。"""

    def _validate(self, url):
        from core.browser_url_policy import check_navigate_url
        return check_navigate_url(url)

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

    # ─── 批准（allow_non_public）只解锁私网判定 ───────────────────

    def test_private_allowed_when_approved(self):
        from core.browser_url_policy import check_navigate_url
        for url in ("http://127.0.0.1:8080/", "http://192.168.1.5/", "http://localhost:9000/"):
            assert check_navigate_url(url, allow_non_public=True) is None

    def test_scheme_still_rejected_when_approved(self):
        from core.browser_url_policy import check_navigate_url
        for url in ("file:///etc/passwd", "data:text/html,x", "javascript:alert(1)"):
            reason = check_navigate_url(url, allow_non_public=True)
            assert reason and "scheme" in reason

    def test_metadata_endpoints_rejected_even_when_approved(self):
        from core.browser_url_policy import check_navigate_url
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://metadata.google.internal/",
            "http://100.100.100.200/latest/meta-data/",
        ):
            reason = check_navigate_url(url, allow_non_public=True)
            assert reason and "硬拒绝" in reason


class TestCdpPortFallback:
    """W11/W12: CDP 端口占用/冲突处理（不启动真实 Chrome）。"""

    def test_cdp_port_defaults_to_9222(self):
        from core.browser_service import DEFAULT_CDP_PORT, BrowserService
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
        with patch("core.browser_chrome.sys.platform", "win32"), \
             patch("core.browser_chrome.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=fake_output, text=True)
            assert service._find_pid_by_port(9222) == 4321
            assert service._find_pid_by_port(9999) is None

    def test_pid_uses_our_profile_true_when_matching(self):
        """进程命令行包含本项目 profile 时判定为自有 Chrome。"""
        from core.browser_service import BrowserService
        service = BrowserService()
        profile = service._chrome_profile_dir.replace("/", "\\").lower()
        cmdline = f'"C:\\fake\\chrome.exe" --user-data-dir={profile}'
        with patch("core.browser_chrome.sys.platform", "win32"), \
             patch("core.browser_chrome.subprocess.run") as mock_run:
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
        with patch("core.browser_chrome.sys.platform", "win32"), \
             patch("core.browser_chrome.subprocess.run") as mock_run:
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


class TestWaitForTimeoutAlignment:
    """wait_for 的等待必须小于 worker 预算，否则结果被丢弃、线程继续被占住。"""

    def test_clamps_model_timeout_to_worker_budget(self):
        from core.browser_service import _WAIT_FOR_MAX_TIMEOUT_MS, BrowserService

        service = BrowserService.__new__(BrowserService)
        captured = {}

        def fake_execute(name, func, tab_index=None, timeout=None):
            captured["worker_timeout"] = timeout
            return MagicMock()

        service._execute_operation = fake_execute
        service.wait_for("#x", timeout=600000)  # 模型传 10 分钟

        assert captured["worker_timeout"] is not None
        # worker 等待必须严格大于操作自身等待，否则结果必然被丢弃
        assert captured["worker_timeout"] > _WAIT_FOR_MAX_TIMEOUT_MS / 1000
        assert captured["worker_timeout"] < 120

    def test_short_timeout_kept(self):
        from core.browser_service import BrowserService

        service = BrowserService.__new__(BrowserService)
        captured = {}

        def fake_execute(name, func, tab_index=None, timeout=None):
            captured["worker_timeout"] = timeout
            return MagicMock()

        service._execute_operation = fake_execute
        service.wait_for("#x", timeout=5000)
        assert captured["worker_timeout"] == pytest.approx(20.0)


class TestWorkerTimeoutMessaging:
    """worker 超时返回可操作错误（弹窗提示 + reset 指引）。"""

    def test_operation_timeout_returns_actionable_error(self):
        from core.browser_service import BrowserService

        service = BrowserService()
        with patch.object(service, "_run_in_worker",
                          side_effect=TimeoutError("Task execution timeout (60s)")):
            result = service._execute_operation("snapshot", lambda page: None)
        assert result.error
        assert "reset" in result.output
        assert "native dialog" in result.output

    def test_navigate_timeout_returns_actionable_error(self):
        from core.browser_service import BrowserService

        service = BrowserService()
        with patch.object(service, "_run_in_worker",
                          side_effect=TimeoutError("Task execution timeout (80s)")):
            result = service.navigate("https://example.com")
        assert result.error
        assert "reset" in result.output


class TestWorkerRecovery:
    """超时自愈：杀 Chrome 释放卡住的调用；线程不退出则换代重建。"""

    def test_timeout_recovers_and_restarts_wedged_worker(self):
        import threading as _threading
        import time

        import core.browser_service as mod
        from core.browser_service import BrowserService

        service = BrowserService()
        block = _threading.Event()

        def stuck():
            block.wait(10)  # 模拟被原生弹窗/渲染器挡住的 sync 调用
            return "late"

        with patch.object(service, "_kill_chrome_internal") as kill, \
             patch.object(mod, "_RECOVERY_WORKER_GRACE_S", 0.1):
            with pytest.raises(TimeoutError):
                service._run_in_worker(stuck, timeout=0.2)

            deadline = time.time() + 5
            while service._recovering and time.time() < deadline:
                time.sleep(0.01)

        assert kill.called, "超时应触发杀 Chrome 以释放卡住的调用"
        assert service._worker_generation >= 2, "卡死的线程应被换代重建"
        assert service._worker_thread is not None and service._worker_thread.is_alive()

        block.set()
        # 新 worker 能继续执行任务（旧队列/旧实例已被替代）
        assert service._run_in_worker(lambda: "ok", timeout=5) == "ok"


class TestResetAction:
    """browser(action='reset')：手动恢复入口。"""

    def test_reset_invalidates_and_returns_ok(self):
        from core.browser_service import BrowserService

        service = BrowserService()
        with patch.object(service, "_kill_chrome_internal") as kill, \
             patch.object(service, "_run_in_worker", return_value=None):
            result = service.reset()
        assert not result.error
        assert "reset" in result.output.lower()
        kill.assert_called_once()

    def test_reset_while_recovering_is_rejected(self):
        from core.browser_service import BrowserService

        service = BrowserService()
        service._recovering = True
        result = service.reset()
        assert result.error
        assert "in progress" in result.output.lower()


class TestKillChromeSafety:
    """_kill_chrome_internal 的 PID 复用防护：只杀确认属于本项目的 Chrome。

    Windows 会复用 PID：服务运行久了，记录的旧 PID 可能已属于用户自己的浏览器
    或其他程序。杀前必须重新校验（存活 + 命令行含本项目 profile），
    拿不到身份就跳过，绝不能误杀。
    """

    @staticmethod
    def _service_with_process(pid=4321, alive=True):
        from core.browser_service import BrowserService

        service = BrowserService()
        proc = MagicMock()
        proc.pid = pid
        proc.poll.return_value = None if alive else 0
        service._chrome_process = proc
        return service

    def test_skips_when_profile_unverifiable(self):
        service = self._service_with_process()
        with patch.object(service, "_pid_uses_our_profile", return_value=False), \
             patch.object(service, "_remove_chrome_locks") as locks, \
             patch("core.browser_chrome.subprocess.run") as run:
            service._kill_chrome_internal()

        run.assert_not_called()  # 没有 taskkill / os.kill
        locks.assert_not_called()  # 无法确认已死，不碰 profile 锁文件
        assert service._chrome_process is None

    def test_skips_when_process_already_exited(self):
        service = self._service_with_process(alive=False)
        with patch.object(service, "_pid_uses_our_profile") as profile, \
             patch.object(service, "_remove_chrome_locks"), \
             patch("core.browser_chrome.subprocess.run") as run:
            service._kill_chrome_internal()

        profile.assert_not_called()  # 已退出：不再按旧 PID 探测/杀
        run.assert_not_called()
        assert service._chrome_process is None

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill")
    def test_kills_only_verified_profile(self):
        service = self._service_with_process(pid=777)
        with patch.object(service, "_pid_uses_our_profile", return_value=True), \
             patch.object(service, "_is_port_listening", return_value=False), \
             patch.object(service, "_remove_chrome_locks"), \
             patch("core.browser_chrome.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            service._kill_chrome_internal()

        argv = run.call_args[0][0]
        assert argv[0] == "taskkill"
        assert "/T" in argv
        assert argv[-1] == "777"
        assert service._chrome_process is None
