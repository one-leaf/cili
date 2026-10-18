"""微信接入端的设置 API 测试：配置白名单 + ``/api/config`` 的 wechat 段。

沿用 ``test_qq_settings_api.py`` 的做法：**直接调端点函数**，不用 TestClient——
它和 ``asyncio.to_thread`` 组合会死锁，且 client IP 不在 localhost 白名单会 403。

多账号后（设计 §7.2）：wechat 段是 ``{"accounts": [...]}``，只有身份与开关字段；
调参字段全部下沉为固定默认值（§3.2.1），不再随配置往返。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """把全局配置指向临时文件，避免读写真实 data/cili/setting.json。"""
    import core.config as config_mod
    cfg_path = tmp_path / "setting.json"
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", cfg_path)
    return cfg_path


def _read(cfg_path: Path) -> dict:
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def _write(cfg_path: Path, data: dict) -> None:
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated_account_dir(tmp_path, monkeypatch):
    """微信凭证目录（含旧单文件）指到临时目录——兼容读会去读它。"""
    import interfaces.wechat.account as account_mod
    monkeypatch.setattr(account_mod, "DATA_DIR", tmp_path)


# ─── 配置对象：白名单字段进文件，调参字段固定默认（设计 §3.2.1） ──────


class TestWechatAccountsConfig:
    def test_from_dict_reads_whitelist_only(self):
        """调参键与协议常量不再从配置读取——统一默认值。"""
        from interfaces.wechat.config import WechatConfig
        cfg = WechatConfig.from_dict({
            "id": "a1",
            "enabled": True,
            "text_chunk_limit": 99,      # 旧调参键 → 忽略
            "typing_enabled": False,     # 旧调参键 → 忽略
            "base_url": "https://evil",  # 协议常量 → 忽略
        })
        assert cfg.enabled is True
        assert cfg.text_chunk_limit == 1800
        assert cfg.typing_enabled is True
        assert cfg.base_url == "https://ilinkai.weixin.qq.com"

    def test_roundtrip_keeps_persist_fields_only(self):
        from interfaces.wechat.config import WechatConfig
        cfg = WechatConfig(id="a1", enabled=True, default_workspace_uuid="w1")
        assert WechatConfig.from_dict(cfg.to_dict()) == cfg
        assert set(cfg.to_dict()) == {
            "id", "label", "enabled", "default_workspace_uuid", "workspace_locked",
        }

    def test_bad_bool_type_falls_back(self):
        from interfaces.wechat.config import WechatConfig
        cfg = WechatConfig.from_dict({"workspace_locked": "yes", "enabled": 1})
        assert cfg.workspace_locked is True
        assert cfg.enabled is True

    def test_load_wechat_accounts_compat_flat_section(self, isolated_config):
        """旧扁平段按「整段即一个账号」兼容读，id 取旧凭证的 ilink_bot_id（设计 §3.1）。"""
        import interfaces.wechat.account as account_mod
        from core.fs_utils import atomic_write_json
        from interfaces.wechat.config import load_wechat_accounts

        atomic_write_json(account_mod.legacy_account_path(), {"ilink_bot_id": "b1"})
        _write(isolated_config, {"wechat": {
            "enabled": True, "text_chunk_limit": 1800, "typing_enabled": True,
        }})

        accounts = load_wechat_accounts()
        assert len(accounts) == 1
        assert accounts[0].id == "b1"
        assert accounts[0].enabled is True
        assert accounts[0].text_chunk_limit == 1800    # 固定默认值，不是配置里的

    def test_load_wechat_accounts_new_format(self, isolated_config):
        from interfaces.wechat.config import load_wechat_accounts
        _write(isolated_config, {"wechat": {"accounts": [
            {"id": "w1", "enabled": True},
            {"id": "w2", "enabled": False},
        ]}})
        assert [a.id for a in load_wechat_accounts()] == ["w1", "w2"]


# ─── /api/config 的 wechat 段 ─────────────────────────────────────────


class TestPutConfigWechat:
    def test_saves_accounts(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config
        asyncio.run(update_config(UpdateConfigRequest(wechat={"accounts": [
            {"id": "w1", "label": "主号", "enabled": True},
        ]})))
        saved = _read(isolated_config)["wechat"]["accounts"]
        assert saved[0] == {
            "id": "w1", "label": "主号", "enabled": True,
            "default_workspace_uuid": "", "workspace_locked": False,
        }

    def test_assigns_missing_id(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config
        asyncio.run(update_config(UpdateConfigRequest(wechat={"accounts": [{}]})))
        assert _read(isolated_config)["wechat"]["accounts"][0]["id"]

    def test_preserves_unsubmitted_sections(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config
        _write(isolated_config, {
            "qq": {"accounts": [{"id": "a1", "app_id": "1"}]},
            "wechat": {"accounts": [{"id": "w1", "enabled": False}]},
        })

        asyncio.run(update_config(UpdateConfigRequest(wechat={"accounts": [
            {"id": "w1", "enabled": True},
        ]})))

        saved = _read(isolated_config)
        assert saved["wechat"]["accounts"][0]["enabled"] is True
        assert saved["qq"]["accounts"][0]["app_id"] == "1"   # 其它段不动

    def test_toggle_triggers_apply(self, isolated_config, monkeypatch):
        import web.routes_config as rc
        calls: list = []
        monkeypatch.setattr(
            rc, "_apply_account_enabled_changes",
            lambda iface, b, a: calls.append((iface, dict(b), dict(a))),
        )
        _write(isolated_config, {"wechat": {"accounts": [{"id": "w1", "enabled": False}]}})

        asyncio.run(rc.update_config(rc.UpdateConfigRequest(wechat={"accounts": [
            {"id": "w1", "enabled": True},
        ]})))

        assert calls == [("wechat", {"w1": False}, {"w1": True})]

    def test_no_toggle_when_value_unchanged(self, isolated_config, monkeypatch):
        """开关没变就不该重启接入端（重启要停/起宿主线程，代价不小）。"""
        import web.routes_config as rc
        isolated_config.write_text(
            json.dumps({"wechat": {"accounts": [{"id": "w1", "enabled": True}]}}),
            encoding="utf-8",
        )
        calls: list = []
        monkeypatch.setattr(
            rc, "_apply_account_enabled_changes",
            lambda iface, b, a: calls.append((iface, b, a)),
        )

        asyncio.run(rc.update_config(rc.UpdateConfigRequest(wechat={"accounts": [
            {"id": "w1", "enabled": True},
        ]})))

        assert calls == []

    def test_no_wechat_key_means_no_toggle(self, isolated_config, monkeypatch):
        """请求里没带 wechat 段 → 完全不动接入端。"""
        import web.routes_config as rc
        calls: list = []
        monkeypatch.setattr(
            rc, "_apply_account_enabled_changes",
            lambda iface, b, a: calls.append((iface, b, a)),
        )

        asyncio.run(rc.update_config(rc.UpdateConfigRequest(system={"pip_mirror": ""})))

        assert calls == []

    def test_get_config_exposes_wechat_section(self, isolated_config):
        """前端要读 cfg.wechat.accounts；凭证不在配置里，所以无需掩码。"""
        from web.routes_config import get_config
        _write(isolated_config, {"wechat": {"accounts": [{"id": "w1", "enabled": True}]}})

        data = asyncio.run(get_config())

        assert data["config"]["wechat"]["accounts"][0]["enabled"] is True
