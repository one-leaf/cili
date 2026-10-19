"""Configuration management tests"""

import json

import pytest


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

    def test_get_workspace_data_dir(self, tmp_path, monkeypatch):
        """Test workspace data directory path ({directory}/.cili)"""
        from core import config

        monkeypatch.setattr(config, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        config.upsert_workspace_entry({
            "uuid": "test-uuid-123",
            "workspace_name": "Test",
            "directory": str(tmp_path / "ws"),
        })

        path = config.get_workspace_data_dir("test-uuid-123")
        assert path == tmp_path / "ws" / ".cili"

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
        """空 providers + 未配置 agents.master → master_ref 兜底失败"""
        from core import config

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "setting.json")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

        with pytest.raises(RuntimeError, match="agents.master.model"):
            config.load_config()

    def test_load_config_new_schema(self, tmp_path, monkeypatch):
        """新 schema：providers + agents 两级引用"""
        from core import config

        config_data = {
            "providers": {
                "bg": {"api_key": "test-key", "base_url": "https://test.api",
                       "models": {"main": {"name": "ac/main-model"}}}
            },
            "agents": {"master": {"model": "bg/main"}}
        }
        config_file = tmp_path / "setting.json"
        config_file.write_text(json.dumps(config_data))

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", config_file)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

        result = config.load_config()
        assert result.model_for_role("master").api_key == "test-key"
        assert result.model_for_role("master").base_url == "https://test.api"
        assert result.model_for_role("master").name == "ac/main-model"

    def test_load_config_env_override(self, tmp_path, monkeypatch):
        """ANTHROPIC_API_KEY/MODEL 只覆盖 master 的接入层与模型 ID"""
        from core import config

        config_data = {
            "providers": {
                "bg": {"api_key": "config-key", "models": {"main": {"name": "config-model"}}}
            },
            "agents": {"master": {"model": "bg/main"}}
        }
        config_file = tmp_path / "setting.json"
        config_file.write_text(json.dumps(config_data))

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", config_file)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        monkeypatch.setenv("ANTHROPIC_MODEL", "env-model")

        result = config.load_config()
        master = result.model_for_role("master")
        assert master.api_key == "env-key"
        assert master.name == "env-model"

    def test_config_to_dict(self):
        """to_dict 输出 providers/agents 结构"""
        from core.config import AgentConfig, Config, ModelSpec, ProviderConfig

        cfg = Config(
            providers={"bg": ProviderConfig(api_key="key", models={"main": ModelSpec()})},
            agents={"master": AgentConfig(model="bg/main")},
        )
        result = cfg.to_dict()
        assert result["providers"]["bg"]["models"]["main"]["model_type"] == "reasoning"
        assert result["agents"]["master"]["model"] == "bg/main"

    def test_migrate_legacy_config(self, tmp_path, monkeypatch):
        """旧格式（model 为对象）→ 备份 + 转换 + 原子写回 + backend 默认 litellm"""
        from core import config

        legacy = {
            "model": {
                "name": "ac/old-model", "api_key": "old-key", "base_url": "http://old/llm",
                "max_tokens": 36000, "multimodal": True, "temperature": 0.2, "reasoning_effort": "",
            }
        }
        config_file = tmp_path / "setting.json"
        config_file.write_text(json.dumps(legacy), encoding="utf-8")
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", config_file)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

        result = config.load_config()
        migrated = json.loads(config_file.read_text(encoding="utf-8"))
        assert "model" not in migrated and "providers" in migrated and "agents" in migrated
        provider = migrated["providers"]["default"]
        assert provider["backend"] == "litellm"
        assert migrated["agents"]["master"]["model"] == "default/main"
        backup = config_file.with_suffix(".json.bak")
        assert backup.exists()
        assert json.loads(backup.read_text(encoding="utf-8")) == legacy
        master = result.model_for_role("master")
        assert master.name == "ac/old-model"
        assert master.input == ["text", "image"]
        assert master.reasoning == "default"

    def test_migrate_idempotent(self, tmp_path, monkeypatch):
        """新格式二次加载不再触发迁移（.bak 不存在）"""
        from core import config

        migrated = {
            "providers": {"p": {"api_key": "k", "models": {"m": {}}}},
            "agents": {"master": {"model": "p/m"}},
        }
        config_file = tmp_path / "setting.json"
        config_file.write_text(json.dumps(migrated), encoding="utf-8")
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", config_file)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

        config.load_config()
        config.load_config()
        assert not config_file.with_suffix(".json.bak").exists()

    def test_migrate_shared_credentials_merge(self):
        """多个旧角色模型凭据相同 → 合入同一 default provider；不同 → 分 default_2

        用纯转换函数 convert_legacy_config（无写盘副作用，不碰真实配置文件）。
        """
        from core.config import convert_legacy_config

        cfg = {
            "model": {"name": "m1", "api_key": "k1", "base_url": "http://a"},
            "worker_model": {"name": "m2", "api_key": "k1", "base_url": "http://a"},
            "lite_model": {"name": "m3", "api_key": "k2", "base_url": "http://b"},
        }
        out = convert_legacy_config(cfg)
        assert out is not None
        assert set(out["providers"]) == {"default", "default_2"}
        assert out["providers"]["default"]["backend"] == "litellm"
        assert out["providers"]["default"]["models"]["main"]["name"] == "m1"
        assert out["providers"]["default"]["models"]["worker"]["name"] == "m2"
        assert out["agents"]["master"]["model"] == "default/main"
        assert out["agents"]["worker"]["model"] == "default/worker"
        assert out["agents"]["lite"]["model"] == "default_2/lite"
        assert "model" in cfg  # 纯函数不改输入

    def test_convert_non_legacy_returns_none(self):
        """新格式（无 model 对象）→ None（调用方原样返回）"""
        from core.config import convert_legacy_config

        assert convert_legacy_config({"providers": {}, "agents": {}}) is None


class TestSystemConfig:
    """SystemConfig：max_concurrent_agents 默认值与范围钳制。"""

    def test_default_max_concurrent_agents(self):
        """默认并发上限为 2。"""
        from core.config import SystemConfig

        assert SystemConfig().max_concurrent_agents == 2

    def test_max_concurrent_agents_clamped_to_bounds(self):
        """并发上限钳制在 [1, 10]。"""
        from core.config import SystemConfig

        assert SystemConfig.from_dict({"max_concurrent_agents": 0}).max_concurrent_agents == 1
        assert SystemConfig.from_dict({"max_concurrent_agents": -5}).max_concurrent_agents == 1
        assert SystemConfig.from_dict({"max_concurrent_agents": 99}).max_concurrent_agents == 10
        assert SystemConfig.from_dict({"max_concurrent_agents": 3}).max_concurrent_agents == 3

    def test_max_concurrent_agents_to_dict(self):
        """to_dict 输出 max_concurrent_agents。"""
        from core.config import SystemConfig

        cfg = SystemConfig.from_dict({"max_concurrent_agents": 7})
        assert cfg.to_dict()["max_concurrent_agents"] == 7


class TestModelResolution:
    """引用解析、角色继承、fail-fast 校验"""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        """隔离环境变量覆盖（用户环境可能设了 ANTHROPIC_MODEL 等）"""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

    def _make_config(self, providers=None, agents=None):
        from core.config import Config
        return Config.from_global_config({
            "providers": providers if providers is not None else {
                "bg": {"api_key": "k", "base_url": "http://x", "models": {
                    "main": {"name": "bg/main-model"}, "cheap": {"name": "bg/cheap"},
                }},
                "dgx": {"api_key": "k2", "models": {"big": {"name": "ds/v4"}}},
            },
            "agents": agents if agents is not None else {"master": {"model": "bg/main"}},
        })

    def test_resolve_model_basic(self):
        cfg = self._make_config()
        m = cfg.resolve_model("bg/main")
        assert m.name == "bg/main-model" and m.api_key == "k" and m.base_url == "http://x"

    def test_resolve_model_key_with_slash(self):
        """模型键内可含 /（按第一个 / 拆分）"""
        cfg = self._make_config(
            providers={"gw": {"api_key": "k", "models": {"vendor/model-x": {"max_output_tokens": 4096}}}},
            agents={"master": {"model": "gw/vendor/model-x"}},
        )
        m = cfg.resolve_model("gw/vendor/model-x")
        assert m.name == "vendor/model-x" and m.max_output_tokens == 4096

    def test_resolve_missing_ref_fail_fast(self):
        cfg = self._make_config()
        with pytest.raises(RuntimeError, match="可用"):
            cfg.resolve_model("bg/missing")
        with pytest.raises(RuntimeError, match="可用"):
            cfg.resolve_model("nope/main")

    def test_role_inheritance(self):
        """worker/lite 未配置时继承 master"""
        cfg = self._make_config(agents={"master": {"model": "bg/main"}, "lite": {"model": "bg/cheap"}})
        assert cfg.model_for_role("worker").name == "bg/main-model"
        assert cfg.model_for_role("lite").name == "bg/cheap"

    def test_unknown_role_falls_back_master(self):
        cfg = self._make_config()
        assert cfg.model_for_role("typo_role").name == "bg/main-model"

    def test_agents_omitted_single_model_fallback(self):
        """agents 省略 + 唯一可用模型 → 三角色共用"""
        cfg = self._make_config(providers={"only": {"api_key": "k", "models": {"solo": {}}}}, agents={})
        assert cfg.model_for_role("master").name == "solo"
        assert cfg.model_for_role("lite").name == "solo"

    def test_agents_omitted_multiple_models_fails(self):
        with pytest.raises(RuntimeError, match="agents.master.model"):
            self._make_config(agents={})

    def test_env_interpolation(self, monkeypatch):
        """api_key 的 ${ENV_VAR} 整值插值"""
        monkeypatch.setenv("MY_TEST_KEY", "sk-from-env")
        cfg = self._make_config(providers={"a": {"api_key": "${MY_TEST_KEY}", "models": {"m": {}}}},
                                agents={"master": {"model": "a/m"}})
        assert cfg.model_for_role("master").api_key == "sk-from-env"

    def test_env_interpolation_missing_var_fails(self, monkeypatch):
        monkeypatch.delenv("MY_MISSING_KEY", raising=False)
        with pytest.raises(RuntimeError, match="MY_MISSING_KEY"):
            self._make_config(providers={"a": {"api_key": "${MY_MISSING_KEY}", "models": {"m": {}}}},
                              agents={"master": {"model": "a/m"}})

    def test_input_must_contain_text(self):
        """旧配置 multimodal=false → input=['text']"""
        from core.config import _convert_legacy_model_spec
        spec = _convert_legacy_model_spec({"name": "x", "multimodal": False})
        assert spec["input"] == ["text"]


class TestEnsureDefaultWorkspace:
    """ensure_default_workspace() 默认开启版本管理与记忆。"""

    def test_new_default_enables_both(self, tmp_path, monkeypatch):
        """全新创建：Default 默认 git_enabled/memory_enabled=True，并触发 git 初始化"""
        from core import config

        monkeypatch.setattr(config, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        inited = []
        monkeypatch.setattr(config, "_init_workspace_git_quietly", lambda uuid: inited.append(uuid))

        uuid = config.ensure_default_workspace()
        assert uuid
        entry = config.load_workspace_config(uuid)
        assert entry["git_enabled"] is True
        assert entry["memory_enabled"] is True
        assert inited == [uuid]

    def test_existing_default_backfills_and_renames(self, tmp_path, monkeypatch):
        """旧条目（缺省字段）：一次性补默认开启；legacy default 顺手改名"""
        from core import config

        monkeypatch.setattr(config, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        config.upsert_workspace_entry({
            "uuid": "old-ws",
            "workspace_name": "default",
            "directory": str(tmp_path / "ws"),
        })
        inited = []
        monkeypatch.setattr(config, "_init_workspace_git_quietly", lambda uuid: inited.append(uuid))

        uuid = config.ensure_default_workspace()
        assert uuid == "old-ws"
        entry = config.load_workspace_config("old-ws")
        assert entry["workspace_name"] == "Default"
        assert entry["git_enabled"] is True
        assert entry["memory_enabled"] is True
        assert inited == ["old-ws"]

    def test_explicit_off_not_overridden(self, tmp_path, monkeypatch):
        """显式关闭过的字段不回填，也不触发 git 初始化"""
        from core import config

        monkeypatch.setattr(config, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        config.upsert_workspace_entry({
            "uuid": "off-ws",
            "workspace_name": "Default",
            "directory": str(tmp_path / "ws"),
            "git_enabled": False,
            "memory_enabled": False,
        })
        inited = []
        monkeypatch.setattr(config, "_init_workspace_git_quietly", lambda uuid: inited.append(uuid))

        uuid = config.ensure_default_workspace()
        assert uuid == "off-ws"
        entry = config.load_workspace_config("off-ws")
        assert entry["git_enabled"] is False
        assert entry["memory_enabled"] is False
        assert inited == []

    def test_backfill_runs_only_once(self, tmp_path, monkeypatch):
        """字段补齐后再次调用：幂等，不再触发 git 初始化"""
        from core import config

        monkeypatch.setattr(config, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        config.upsert_workspace_entry({
            "uuid": "once-ws",
            "workspace_name": "Default",
            "directory": str(tmp_path / "ws"),
        })
        inited = []
        monkeypatch.setattr(config, "_init_workspace_git_quietly", lambda uuid: inited.append(uuid))

        assert config.ensure_default_workspace() == "once-ws"
        assert config.ensure_default_workspace() == "once-ws"
        assert inited == ["once-ws"]
