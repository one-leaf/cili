"""邮件接入端的设置 API：``/api/config`` 的 email 段 + 状态端点。

沿用 ``test_qq_settings_api.py`` 的做法：**直接调端点函数**（TestClient 与
``asyncio.to_thread`` 组合会死锁）。启停副作用统一替身——测试不能真的去连邮箱。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_email_data_dir(tmp_path, monkeypatch):
    """隔离邮件游标/拦截统计：状态端点会读 data/cili/tools/email/accounts/*.json。"""
    import interfaces.email.cursor as cursor_mod
    import interfaces.email.state as state_mod

    monkeypatch.setattr(cursor_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(state_mod, "DATA_DIR", tmp_path)


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """把全局配置指向临时文件，避免读写真实 data/cili/setting.json。"""
    import core.config as config_mod
    cfg_path = tmp_path / "setting.json"
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", cfg_path)
    return cfg_path


@pytest.fixture(autouse=True)
def no_interface_restart(monkeypatch):
    """保存配置触发的账号启停换成记录器（不让测试真连邮箱）。"""
    from web import routes_config

    calls: list[tuple] = []
    monkeypatch.setattr(
        routes_config, "_apply_account_enabled_changes",
        lambda interface, before, after: calls.append((interface, before, after)),
    )
    return calls


def _read(cfg_path: Path) -> dict:
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def _write(cfg_path: Path, data: dict) -> None:
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _account(**overrides) -> dict:
    base = {
        "id": "agent@example.com",
        "email_address": "agent@example.com",
        "imap_host": "imap.example.com",
        "smtp_host": "smtp.example.com",
        "username": "agent@example.com",
        "password": "app-password",
        "allowed_senders": ["me@example.com"],
        "enabled": True,
    }
    base.update(overrides)
    return base


# ─── GET：password 掩码 ──────────────────────────────────────────────


class TestGetConfigMasksEmailPassword:
    def test_masks_password(self, isolated_config):
        from web.routes_config import get_config

        _write(isolated_config, {"email": {"accounts": [_account()]}})
        acct = asyncio.run(get_config())["config"]["email"]["accounts"][0]
        assert "password" not in acct               # 明文绝不出网
        assert acct["password_masked"].startswith("app-")
        assert acct["email_address"] == "agent@example.com"

    def test_no_password_no_masked_field(self, isolated_config):
        from web.routes_config import get_config

        _write(isolated_config, {"email": {"accounts": [_account(password="")]}})
        acct = asyncio.run(get_config())["config"]["email"]["accounts"][0]
        assert "password_masked" not in acct


# ─── PUT：全量替换 + 保留语义 ────────────────────────────────────────


class TestPutConfigEmail:
    def test_saves_account(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config

        asyncio.run(update_config(UpdateConfigRequest(email={"accounts": [_account()]})))
        saved = _read(isolated_config)["email"]["accounts"][0]
        assert saved["password"] == "app-password"
        assert saved["allowed_senders"] == ["me@example.com"]
        assert saved["smtp_security"] == "ssl"

    def test_id_normalized_to_email(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config

        asyncio.run(update_config(UpdateConfigRequest(email={"accounts": [
            _account(id="whatever", email_address="Agent@Example.COM"),
        ]})))
        saved = _read(isolated_config)["email"]["accounts"][0]
        assert saved["id"] == "agent@example.com"

    def test_blank_password_preserves_existing(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config

        _write(isolated_config, {"email": {"accounts": [_account()]}})
        asyncio.run(update_config(UpdateConfigRequest(email={"accounts": [
            _account(password=""),          # 表单不回传明文
        ]})))
        assert _read(isolated_config)["email"]["accounts"][0]["password"] == "app-password"

    def test_masked_password_preserves_existing(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config

        _write(isolated_config, {"email": {"accounts": [_account()]}})
        asyncio.run(update_config(UpdateConfigRequest(email={"accounts": [
            _account(password="app-...word"),
        ]})))
        assert _read(isolated_config)["email"]["accounts"][0]["password"] == "app-password"

    def test_duplicate_email_rejected(self, isolated_config):
        from fastapi import HTTPException

        from web.routes_config import UpdateConfigRequest, update_config

        with pytest.raises(HTTPException) as exc:
            asyncio.run(update_config(UpdateConfigRequest(email={"accounts": [
                _account(), _account(label="重复"),
            ]})))
        assert exc.value.status_code == 400

    def test_invalid_sender_rejected(self, isolated_config):
        from fastapi import HTTPException

        from web.routes_config import UpdateConfigRequest, update_config

        with pytest.raises(HTTPException) as exc:
            asyncio.run(update_config(UpdateConfigRequest(email={"accounts": [
                _account(allowed_senders=["not an email"]),
            ]})))
        assert exc.value.status_code == 400

    def test_enable_diff_triggers_interface_change(self, isolated_config, no_interface_restart):
        from web.routes_config import UpdateConfigRequest, update_config

        _write(isolated_config, {"email": {"accounts": [_account(enabled=False)]}})
        asyncio.run(update_config(UpdateConfigRequest(email={"accounts": [_account(enabled=True)]})))
        assert no_interface_restart == [(
            "email",
            {"agent@example.com": False},
            {"agent@example.com": True},
        )]

    def test_no_change_no_restart(self, isolated_config, no_interface_restart):
        from web.routes_config import UpdateConfigRequest, update_config

        _write(isolated_config, {"email": {"accounts": [_account()]}})
        asyncio.run(update_config(UpdateConfigRequest(email={"accounts": [_account()]})))
        assert no_interface_restart == []

    def test_other_sections_untouched(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config

        _write(isolated_config, {
            "providers": {"p": {"api_key": "k", "models": {"m": {"name": "keep-me"}}}},
            "system": {"pip_mirror": "https://mirror"},
        })
        asyncio.run(update_config(UpdateConfigRequest(email={"accounts": [_account()]})))
        data = _read(isolated_config)
        assert data["providers"]["p"]["models"]["m"]["name"] == "keep-me"
        assert data["system"]["pip_mirror"] == "https://mirror"


# ─── test-email + status ─────────────────────────────────────────────


class TestEmailEndpoints:
    def test_test_email_requires_config(self, isolated_config):
        from web.routes_config import EmailTestRequest, test_email

        result = asyncio.run(test_email(EmailTestRequest(account={"email_address": "a@x.com"})))
        assert result["ok"] is False
        assert result["error"]

    def test_status_lists_accounts_without_credentials(self, isolated_config):
        from web.routes_email import email_status

        _write(isolated_config, {"email": {"accounts": [_account()]}})
        data = asyncio.run(email_status())
        acct = data["accounts"][0]
        assert acct["id"] == "agent@example.com"
        assert acct["configured"] is True
        assert "password" not in acct
        assert acct["ignored_24h"]["total"] == 0
