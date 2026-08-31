"""Configuration management tests"""

import json
import os
import pytest
from pathlib import Path


class TestConfig:
    """Configuration management tests"""

    def test_validate_workspace_name_valid(self):
        """Test valid workspace names"""
        from core.config import validate_workspace_name

        assert validate_workspace_name("test") is None
        assert validate_workspace_name("test-workspace") is None
        assert validate_workspace_name("test_workspace_123") is None
        assert validate_workspace_name("测试工作区") is None

    def test_validate_workspace_name_empty(self):
        """Test empty workspace name"""
        from core.config import validate_workspace_name

        assert validate_workspace_name("") == "Workspace name is required"
        assert validate_workspace_name(None) == "Workspace name is required"

    def test_validate_workspace_name_too_long(self):
        """Test workspace name too long"""
        from core.config import validate_workspace_name

        long_name = "a" * 51
        assert "50 characters" in validate_workspace_name(long_name)

    def test_validate_workspace_name_invalid_chars(self):
        """Test workspace name with invalid characters"""
        from core.config import validate_workspace_name

        assert "invalid characters" in validate_workspace_name("test/workspace")
        assert "invalid characters" in validate_workspace_name("test\\workspace")
        assert "invalid characters" in validate_workspace_name("test\nworkspace")

    def test_get_workspace_data_dir(self):
        """Test workspace data directory path"""
        from core.config import get_workspace_data_dir

        path = get_workspace_data_dir("test-uuid-123")
        assert "agents" in str(path)
        assert "test-uuid-123" in str(path)

    def test_load_global_config_missing(self, tmp_path, monkeypatch):
        """Test loading config when file doesn't exist"""
        from core import config

        # Override DATA_DIR to use temp directory
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "setting.json")

        result = config.load_global_config()
        assert result == {}

    def test_load_global_config_exists(self, tmp_path, monkeypatch):
        """Test loading config when file exists"""
        from core import config

        config_data = {"model": {"name": "test-model"}}
        config_file = tmp_path / "setting.json"
        config_file.write_text(json.dumps(config_data))

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", config_file)

        result = config.load_global_config()
        assert result["model"]["name"] == "test-model"

    def test_load_global_config_invalid_json(self, tmp_path, monkeypatch):
        """Test loading config with invalid JSON"""
        from core import config

        config_file = tmp_path / "setting.json"
        config_file.write_text("invalid json{")

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", config_file)

        result = config.load_global_config()
        assert result == {}

    def test_save_global_config(self, tmp_path, monkeypatch):
        """Test saving global config"""
        from core import config

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "setting.json")

        config_data = {"model": {"name": "saved-model"}}
        result = config.save_global_config(config_data)
        assert result is True

        # Verify file was written
        saved = json.loads((tmp_path / "setting.json").read_text())
        assert saved["model"]["name"] == "saved-model"

    def test_load_config_no_api_key(self, tmp_path, monkeypatch):
        """Test load_config raises error when no API key"""
        from core import config

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "setting.json")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

        with pytest.raises(RuntimeError, match="No API key"):
            config.load_config()

    def test_load_config_with_api_key(self, tmp_path, monkeypatch):
        """Test load_config with API key in config"""
        from core import config

        config_data = {
            "model": {
                "api_key": "test-key",
                "base_url": "https://test.api"
            }
        }
        config_file = tmp_path / "setting.json"
        config_file.write_text(json.dumps(config_data))

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", config_file)
        # Clear all env vars that could override config
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

        result = config.load_config()
        assert result.model.api_key == "test-key"
        assert result.model.base_url == "https://test.api"

    def test_load_config_env_override(self, tmp_path, monkeypatch):
        """Test environment variable overrides config"""
        from core import config

        config_data = {
            "model": {
                "name": "config-model",
                "api_key": "config-key"
            }
        }
        config_file = tmp_path / "setting.json"
        config_file.write_text(json.dumps(config_data))

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", config_file)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")

        result = config.load_config()
        assert result.model.api_key == "env-key"

    def test_config_to_dict(self):
        """Test converting Config to dict"""
        from core.config import Config, ModelConfig

        config = Config(
            model=ModelConfig(name="test", api_key="key"),
        )
        result = config.to_dict()
        assert result["model"]["name"] == "test"

    def test_workspace_config_save_load(self, tmp_path, monkeypatch):
        """Test saving and loading workspace config"""
        from core import config

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)

        workspace_data = {
            "workspace_name": "Test WS",
            "directory": "/test/dir",
            "created_at": "2026-08-22 10:00:00"
        }
        result = config.save_workspace_config("uuid-123", workspace_data)
        assert result is True

        loaded = config.load_workspace_config("uuid-123")
        assert loaded["workspace_name"] == "Test WS"
        assert loaded["directory"] == "/test/dir"
