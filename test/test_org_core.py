"""组织托管：本地会话、配置物化/恢复与 HTTP 客户端。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import core.config as config_mod
from core.org import store
from core.org.apply import apply_config, restore_config
from core.org.client import OrgAuthError, OrgClient, OrgNetworkError, OrgRateLimited
from core.org.service import OrgService, mask_phone, normalize_server_url


@pytest.fixture
def org_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = data_dir / "setting.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "personal": {
                        "protocol": "openai-compatible",
                        "api_key": "personal-key",
                        "base_url": "http://personal/v1",
                        "models": {"main": {"name": "personal-main"}},
                    }
                },
                "agents": {
                    "master": {"model": "personal/main"},
                    "worker": {"model": "personal/main"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", config_path)
    yield data_dir
    store.clear_all()


def _payload(**overrides):
    model = {
        "label": "Acme 模型",
        "provider_key": "org-acme",
        "protocol": "openai-compatible",
        "base_url": "http://llm/v1",
        "api_key": "employee-key",
        "models": {
            "main": {"name": "main-model", "max_context_tokens": 1000},
            "fast": {"name": "fast-model"},
        },
        "roles": {"master": "main", "worker": "main", "lite": "fast"},
    }
    model.update(overrides)
    return {
        "version": "v1",
        "org": {"id": "acme", "name": "Acme"},
        "user": {"id": "u1", "name": "张三", "phone": "13800000000"},
        "model": model,
    }


def test_session_and_state_files(org_env):
    store.save_session({"server_url": "https://x", "device_token": "dev_secret"})
    store.save_state({"provider_key": "org-acme"})
    assert store.load_session()["device_token"] == "dev_secret"
    assert store.load_state()["provider_key"] == "org-acme"
    assert "dev_secret" not in json.dumps(store.load_state())

    store.clear_session()
    store.clear_state()
    assert store.load_session() == {}
    assert store.load_state() == {}


def test_apply_and_restore_roles(org_env):
    state = apply_config(
        _payload(), bind_roles=["master", "worker", "lite"]
    )
    assert state["provider_key"] == "org-acme"
    assert state["roles_applied"] == {
        "master": "org-acme/main",
        "worker": "org-acme/main",
        "lite": "org-acme/fast",
    }
    assert state["roles_before"] == {
        "master": "personal/main",
        "worker": "personal/main",
        "lite": "",
    }
    assert state["model_keys"] == ["fast", "main"]

    saved = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["providers"]["org-acme"]["api_key"] == "employee-key"
    assert saved["agents"]["master"]["model"] == "org-acme/main"
    assert saved["agents"]["worker"]["model"] == "org-acme/main"
    assert saved["agents"]["lite"]["model"] == "org-acme/fast"

    restore_config(state)
    restored = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert "org-acme" not in restored["providers"]
    assert restored["agents"]["master"]["model"] == "personal/main"
    assert restored["agents"]["worker"]["model"] == "personal/main"
    assert "model" not in restored["agents"].get("lite", {})


def test_apply_without_binding_only_adds_provider(org_env):
    """组织模型只加入 provider，不改写角色模型。"""
    state = apply_config(_payload(), bind_roles=[])
    assert state["provider_key"] == "org-acme"
    assert state["roles_applied"] == {}
    saved = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["providers"]["org-acme"]["api_key"] == "employee-key"
    assert saved["agents"]["master"]["model"] == "personal/main"
    assert saved["agents"]["worker"]["model"] == "personal/main"


def test_sync_does_not_override_user_changed_role(org_env):
    state = apply_config(_payload(), bind_roles=["master", "worker"])
    config = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    config["agents"]["master"]["model"] = "personal/main"
    config_mod.save_global_config(config)

    updated = _payload(roles={"master": "fast", "worker": "fast"})
    updated["version"] = "v2"
    state2 = apply_config(updated, bind_roles=["master", "worker"], existing_state=state)

    saved = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["agents"]["master"]["model"] == "personal/main"
    assert saved["agents"]["worker"]["model"] == "org-acme/fast"
    assert "master" not in state2["roles_applied"]
    assert state2["roles_skipped"] == ["master"]


def test_removed_role_is_restored(org_env):
    state = apply_config(
        _payload(), bind_roles=["master", "worker", "lite"]
    )
    state2 = apply_config(
        _payload(), bind_roles=["worker"], existing_state=state
    )
    saved = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["agents"]["master"]["model"] == "personal/main"
    assert saved["agents"]["lite"].get("model") is None
    assert saved["agents"]["worker"]["model"] == "org-acme/main"
    assert state2["roles_applied"] == {"worker": "org-acme/main"}
    assert "master" not in state2["roles_before"]


def test_server_cannot_override_personal_provider_key(org_env):
    state = apply_config(
        _payload(provider_key="personal"), bind_roles=["master"]
    )
    saved = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert state["provider_key"] == "org-acme"
    assert saved["providers"]["personal"]["api_key"] == "personal-key"
    assert "org-acme" in saved["providers"]


def test_apply_reports_unknown_model_key_instead_of_aborting(org_env):
    state = apply_config(_payload(roles={"master": "missing"}))
    assert state["roles_applied"] == {}
    assert state["roles_skipped"] == ["master"]
    saved = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["agents"]["master"]["model"] == "personal/main"


def test_client_contract_with_mock_transport():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.headers))
        if request.url.path.endswith("/public/orgs"):
            return httpx.Response(200, json={"orgs": [{"id": "acme", "name": "Acme"}]})
        if request.url.path.endswith("/auth/sms/send"):
            return httpx.Response(200, json={"ok": True, "cooldown_seconds": 60})
        if request.url.path.endswith("/auth/sms/verify"):
            return httpx.Response(200, json={"token": "dev_x", "org": {"id": "acme"}})
        if request.url.path.endswith("/auth/me"):
            return httpx.Response(200, json={"user": {"id": "u1"}})
        if request.url.path.endswith("/config"):
            if request.headers.get("if-none-match") == '"v1"':
                return httpx.Response(304)
            return httpx.Response(200, json={"version": "v1"})
        if request.url.path.endswith("/auth/logout"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "missing"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    org = OrgClient("https://org.example", client=client)
    assert org.discover()[0]["id"] == "acme"
    assert org.send_sms("acme", "13800000000")["cooldown_seconds"] == 60
    assert org.verify(org_id="acme", phone="13800000000", code="123456")["token"] == "dev_x"
    assert org.me("dev_x")["user"]["id"] == "u1"
    assert org.fetch_config("dev_x", '"v1"') == (True, {})
    assert org.fetch_config("dev_x", "")[1]["version"] == "v1"
    org.logout("dev_x")
    assert any(headers.get("authorization") == "Bearer dev_x" for _, _, headers in seen)
    client.close()


def test_client_error_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/me"):
            return httpx.Response(401, json={"error": "token_invalid"})
        return httpx.Response(429, json={"error": "rate_limited", "retry_after": 12})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    org = OrgClient("https://org.example", client=client)
    with pytest.raises(OrgAuthError):
        org.me("bad")
    with pytest.raises(OrgRateLimited) as exc:
        org.send_sms("acme", "13800000000")
    assert exc.value.retry_after == 12
    client.close()


def test_server_url_and_mask():
    assert normalize_server_url("https://x.example/") == "https://x.example"
    assert normalize_server_url("http://localhost:8000") == "http://localhost:8000"
    with pytest.raises(ValueError):
        normalize_server_url("http://example.com")
    assert mask_phone("13800000000") == "138****0000"


class FakeClient:
    auth_fail = False
    config_payload = None
    not_modified = False

    def __init__(self, url):
        self.url = url

    def close(self):
        pass

    def verify(self, **_kwargs):
        return {
            "token": "dev_fake",
            "expires_at": "2026-12-27 10:00:00",
            "org": {"id": "acme", "name": "Acme"},
            "user": {"id": "u1", "name": "张三", "phone": "13800000000"},
            "config_version": "v1",
        }

    def me(self, token):
        if self.auth_fail:
            raise OrgAuthError(401, "token_invalid")
        return {
            "user": {
                "id": "u1",
                "name": "张三",
                "phone": "13800000000",
                "groups": ["研发部"],
            },
            "org": {"id": "acme"},
        }

    def fetch_config(self, token, etag=""):
        if self.not_modified:
            return True, {}
        return False, self.config_payload

    def logout(self, token):
        pass

    def discover(self):
        return [{"id": "acme", "name": "Acme"}]

    def send_sms(self, org_id, phone):
        return {"cooldown_seconds": 60}


def test_lost_state_never_snapshots_org_reference(org_env):
    apply_config(_payload(), bind_roles=["master"])
    # 模拟 state.json 损坏丢失，但 setting.json 已是组织引用
    state = apply_config(_payload(), bind_roles=["master"], existing_state={})
    assert state["roles_before"]["master"] == ""
    restore_config(state)
    saved = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert "org-acme" not in saved["providers"]
    assert "model" not in saved["agents"]["master"]


def test_state_lost_does_not_override_personal_model(org_env):
    apply_config(_payload(), bind_roles=["master", "worker"])
    config = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    config["agents"]["master"]["model"] = "personal/main"
    config_mod.save_global_config(config)

    # state 丢失；provider 和 worker 的组织引用仍在
    state = apply_config(
        _payload(), bind_roles=["master", "worker"], existing_state={}
    )
    saved = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["agents"]["master"]["model"] == "personal/main"
    assert saved["agents"]["worker"]["model"] == "org-acme/main"
    assert state["roles_skipped"] == ["master"]


def test_non_auth_403_keeps_configuration(org_env):
    FakeClient.config_payload = _payload()
    FakeClient.not_modified = False
    FakeClient.auth_fail = False
    service = OrgService(client_factory=FakeClient)
    service.login(
        server_url="https://org.example",
        org_id="acme",
        phone="13800000000",
        code="123456",
    )

    class ForbiddenClient(FakeClient):
        def me(self, token):
            raise OrgAuthError(403, "model_not_allowed")

    service._client_factory = ForbiddenClient
    status = service.sync(force=True)
    assert status["session_invalid"] is False
    assert "model_not_allowed" in status["last_error"]
    saved = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert "org-acme" in saved["providers"]


def test_invalid_then_relogin_keeps_personal_models(org_env):
    FakeClient.config_payload = _payload()
    FakeClient.not_modified = False
    FakeClient.auth_fail = False
    service = OrgService(client_factory=FakeClient)
    service.login(
        server_url="https://org.example",
        org_id="acme",
        phone="13800000000",
        code="123456",
    )
    FakeClient.auth_fail = True
    invalid = service.sync(force=True)
    assert invalid["session_invalid"] is True
    assert invalid["roles"] == {}

    FakeClient.auth_fail = False
    status = service.login(
        server_url="https://org.example",
        org_id="acme",
        phone="13800000000",
        code="123456",
    )
    # 组织模型只进 provider，不覆盖个人模型
    assert status["roles"] == {}
    saved = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["agents"]["master"]["model"] == "personal/main"


def test_startup_background_sync(org_env, monkeypatch):
    import core.org.service as service_mod

    store.save_session({"server_url": "https://org.example", "device_token": "t"})
    service = OrgService(client_factory=FakeClient)
    calls = []
    service.sync = lambda **_kwargs: calls.append(True) or service.status()
    monkeypatch.setattr(service_mod, "STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(service_mod, "SYNC_INTERVAL_SECONDS", 0.05)
    service.startup()
    try:
        import time
        deadline = time.time() + 1
        while not calls and time.time() < deadline:
            time.sleep(0.01)
    finally:
        service.shutdown()
    assert calls


def test_service_login_sync_and_invalid(org_env):
    FakeClient.config_payload = _payload()
    FakeClient.not_modified = False
    FakeClient.auth_fail = False
    service = OrgService(client_factory=FakeClient)

    status = service.login(
        server_url="https://org.example",
        org_id="acme",
        phone="13800000000",
        code="123456",
        device_name="测试机",
    )
    assert status["joined"] is True
    assert status["session_invalid"] is False
    assert status["provider_key"] == "org-acme"
    assert status["roles"] == {}
    assert status["user"]["phone_masked"] == "138****0000"
    assert "phone" not in status["user"]
    assert "dev_fake" not in json.dumps(status)

    saved = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["providers"]["org-acme"]["api_key"] == "employee-key"
    assert saved["agents"]["master"]["model"] == "personal/main"

    FakeClient.not_modified = True
    assert service.sync()["joined"] is True

    FakeClient.auth_fail = True
    invalid = service.sync(force=True)
    assert invalid["session_invalid"] is True
    saved = json.loads(config_mod.GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
    assert "org-acme" not in saved["providers"]

    logged_out = service.logout()
    assert logged_out["joined"] is False
    assert store.load_session() == {}
