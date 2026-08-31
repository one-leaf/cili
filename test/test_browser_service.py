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
