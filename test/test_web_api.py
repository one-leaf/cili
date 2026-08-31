"""Tests for web_api.py — access control middleware, API key masking, helpers."""

import json
from unittest.mock import patch, MagicMock

import pytest


class TestMaskSingleModel:
    """_mask_single_model() API key masking."""

    def test_long_key_masked(self):
        from web.web_api import _mask_single_model
        model = {"api_key": "sk-ant-1234567890abcdef", "name": "claude"}
        result = _mask_single_model(model)
        assert "api_key" not in result
        assert result["api_key_masked"] == "sk-a...cdef"
        assert result["name"] == "claude"

    def test_short_key_masked(self):
        from web.web_api import _mask_single_model
        model = {"api_key": "shortkey", "name": "test"}
        result = _mask_single_model(model)
        assert "api_key" not in result
        assert result["api_key_masked"] == "***"

    def test_empty_key_no_masked_field(self):
        from web.web_api import _mask_single_model
        model = {"api_key": "", "name": "test"}
        result = _mask_single_model(model)
        assert "api_key" not in result
        assert "api_key_masked" not in result

    def test_no_api_key_field(self):
        from web.web_api import _mask_single_model
        model = {"name": "test"}
        result = _mask_single_model(model)
        assert "api_key" not in result
        assert "api_key_masked" not in result


class TestMaskApiKey:
    """_mask_api_key() config-level masking."""

    def test_masks_both_models(self):
        from web.web_api import _mask_api_key
        config = {
            "model": {"api_key": "sk-ant-1234567890", "name": "claude"},
            "llm_model": {"api_key": "sk-other-key-12345", "name": "haiku"},
        }
        result = _mask_api_key(config)
        assert "api_key" not in result["model"]
        assert "api_key" not in result["llm_model"]
        assert "api_key_masked" in result["model"]
        assert "api_key_masked" in result["llm_model"]

    def test_no_model_field(self):
        from web.web_api import _mask_api_key
        config = {"system": {"pip_mirror": "https://..."}}
        result = _mask_api_key(config)
        assert result["system"]["pip_mirror"] == "https://..."


class TestLocalhostIPs:
    """Access control localhost IP set."""

    def test_contains_standard_ips(self):
        from web.web_api import _LOCALHOST_IPS
        assert "127.0.0.1" in _LOCALHOST_IPS
        assert "::1" in _LOCALHOST_IPS


class TestRequireWorkspace:
    """_require_workspace() dependency validation."""

    def test_nonexistent_workspace_raises_404(self):
        from fastapi import HTTPException
        from web.web_api import _require_workspace
        with pytest.raises(HTTPException) as exc_info:
            # _require_workspace is an async dependency
            import asyncio
            asyncio.run(_require_workspace("nonexistent-uuid"))
        assert exc_info.value.status_code == 404


class TestListAllWorkspaces:
    """_list_all_workspaces() scans workspace directory."""

    def test_returns_empty_when_no_workspaces(self, tmp_path):
        """Empty directory returns empty list."""
        import web.web_api as api_module
        original = api_module.WORKSPACE_DATA_DIR
        try:
            api_module.WORKSPACE_DATA_DIR = tmp_path
            result = api_module._list_all_workspaces()
            assert result == []
        finally:
            api_module.WORKSPACE_DATA_DIR = original

    def test_skips_hidden_directories(self, tmp_path):
        """Directories starting with '.' are skipped."""
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "visible").mkdir()
        import web.web_api as api_module
        original = api_module.WORKSPACE_DATA_DIR
        try:
            api_module.WORKSPACE_DATA_DIR = tmp_path
            result = api_module._list_all_workspaces()
            # .hidden should not appear (visible has no config, so also skipped)
            assert all(w["uuid"] != ".hidden" for w in result)
        finally:
            api_module.WORKSPACE_DATA_DIR = original


class TestEvictIdleAgent:
    """_evict_idle_agent() LRU eviction logic."""

    def test_no_eviction_when_under_limit(self):
        from web.web_api import _evict_idle_agent, agents, _agent_access, _MAX_AGENTS
        original_agents = dict(agents)
        original_access = dict(_agent_access)
        try:
            agents.clear()
            _agent_access.clear()
            _evict_idle_agent()  # Should not raise
        finally:
            agents.clear()
            agents.update(original_agents)
            _agent_access.clear()
            _agent_access.update(original_access)

    def test_evicts_oldest_idle(self):
        from web.web_api import _evict_idle_agent, agents, _agent_access, _MAX_AGENTS
        original_agents = dict(agents)
        original_access = dict(_agent_access)
        try:
            agents.clear()
            _agent_access.clear()

            mock_agents = {}
            for i in range(_MAX_AGENTS + 3):
                agent = MagicMock()
                agent.is_running.return_value = False
                agent.cleanup.return_value = None
                key = f"test-{i}"
                agents[key] = agent
                _agent_access[key] = 1000 + i

            _evict_idle_agent()

            assert "test-0" not in agents
            assert len(agents) == _MAX_AGENTS + 2
        finally:
            agents.clear()
            agents.update(original_agents)
            _agent_access.clear()
            _agent_access.update(original_access)

    def test_does_not_evict_running_agent(self):
        from web.web_api import _evict_idle_agent, agents, _agent_access, _MAX_AGENTS
        original_agents = dict(agents)
        original_access = dict(_agent_access)
        try:
            agents.clear()
            _agent_access.clear()

            for i in range(_MAX_AGENTS + 2):
                agent = MagicMock()
                agent.is_running.return_value = True
                key = f"test-{i}"
                agents[key] = agent
                _agent_access[key] = 1000 + i

            _evict_idle_agent()

            assert len(agents) == _MAX_AGENTS + 2
        finally:
            agents.clear()
            agents.update(original_agents)
            _agent_access.clear()
            _agent_access.update(original_access)
