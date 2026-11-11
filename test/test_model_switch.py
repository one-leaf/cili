"""会话级模型切换：payload、持久化、runner 运行时换 client。"""

from types import SimpleNamespace

import pytest

from core.config import AgentConfig, Config, ModelSpec, ProviderConfig, SystemConfig
from core.session import SessionStore
from core.session_runner import SessionRunner
from web.routes_chat import _model_payload


def _config() -> Config:
    return Config(
        providers={
            "p": ProviderConfig(
                interface_type="anthropic",
                api_key="test",
                base_url="http://localhost",
                models={
                    "a": ModelSpec(name="Model A", max_output_tokens=1000),
                    "b": ModelSpec(name="Model B", max_output_tokens=2000),
                },
            )
        },
        agents={"master": AgentConfig(model="p/a")},
        system=SystemConfig(),
    )


class TestModelPayload:
    def test_session_override(self):
        data = _model_payload(_config(), SimpleNamespace(metadata={"model_ref": "p/b"}))
        assert data["current_ref"] == "p/b"
        assert data["current_name"] == "Model B"
        assert {m["ref"] for m in data["available"]} == {"p/a", "p/b"}

    def test_defaults_to_master(self):
        data = _model_payload(_config(), SimpleNamespace(metadata={}))
        assert data["current_ref"] == "p/a"
        assert data["current_name"] == "Model A"

    def test_invalid_override_falls_back(self):
        data = _model_payload(_config(), SimpleNamespace(metadata={"model_ref": "gone/model"}))
        assert data["current_ref"] == "p/a"


class TestRunnerSetModel:
    @staticmethod
    def _runner():
        cfg = _config()
        runner = SessionRunner.__new__(SessionRunner)  # 跳过重量级构造
        runner.config = cfg
        runner.role = "master"
        runner.role_cfg = SimpleNamespace(max_tokens=0)
        runner.model = cfg.resolve_model("p/a")
        runner.model_ref = "p/a"
        runner._closed = []
        runner.client = SimpleNamespace(close=lambda: runner._closed.append(True))
        return runner

    def test_switch_swaps_client_and_closes_old(self):
        runner = self._runner()
        runner.set_model("p/b")
        assert runner.model.name == "Model B"
        assert runner.model_ref == "p/b"
        assert runner._closed == [True]

    def test_same_ref_is_noop(self):
        runner = self._runner()
        runner.set_model("p/a")
        assert runner._closed == []

    def test_unknown_ref_rejected(self):
        runner = self._runner()
        with pytest.raises(ValueError):
            runner.set_model("gone/model")


class TestSessionModelRefPersistence:
    def test_roundtrip(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        session = SessionStore.create_new_session(sessions_dir, "Model Ref")
        session.set_model_ref("p/b")
        session.save()

        loaded = SessionStore.load_session(session.session_id, sessions_dir)
        assert loaded is not None
        assert loaded.metadata.get("model_ref") == "p/b"

        loaded.set_model_ref(None)
        loaded.save()
        reloaded = SessionStore.load_session(session.session_id, sessions_dir)
        assert reloaded is not None
        assert "model_ref" not in reloaded.metadata
