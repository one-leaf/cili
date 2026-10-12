"""微信接入端设置：配置项类型校验 + ``/api/config`` 的 wechat 段。

沿用 ``test_qq_settings_api.py`` 的做法：**直接调端点函数**，不用 TestClient——
它和 ``asyncio.to_thread`` 组合会死锁，且 client IP 不在 localhost 白名单会 403。
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


# ─── 配置项类型校验 ───────────────────────────────────────────────────


class TestWechatConfigCoercion:
    def test_defaults(self):
        from interfaces.wechat.config import WechatConfig
        cfg = WechatConfig.from_dict(None)
        assert cfg.enabled is False
        assert cfg.text_chunk_limit == 1800
        assert cfg.context_message_budget == 8
        assert cfg.typing_enabled is True
        assert cfg.base_url == "https://ilinkai.weixin.qq.com"

    def test_bad_types_fall_back_to_defaults(self):
        from interfaces.wechat.config import WechatConfig
        cfg = WechatConfig.from_dict({
            "enabled": "yes",
            "text_chunk_limit": "abc",          # 非法 → 退回默认
            "context_message_budget": 3,
        })
        assert cfg.enabled is True
        assert cfg.text_chunk_limit == 1800
        assert cfg.context_message_budget == 3

    def test_bool_is_not_a_number(self):
        """True/False 不是合法数字——否则 text_chunk_limit=True 会静默变成 1。"""
        from interfaces.wechat.config import WechatConfig
        cfg = WechatConfig.from_dict({"text_chunk_limit": True})
        assert cfg.text_chunk_limit == 1800

    def test_unknown_keys_ignored(self):
        from interfaces.wechat.config import WechatConfig
        assert WechatConfig.from_dict({"nonsense": 1}) == WechatConfig()

    def test_is_ready_only_checks_base_url(self):
        """凭证来自扫码、不在配置里——所以「就绪」只看 base_url 配没配。"""
        from interfaces.wechat.config import WechatConfig
        assert WechatConfig().is_ready() is True
        assert WechatConfig(base_url="").is_ready() is False


# ─── /api/config 的 wechat 段 ─────────────────────────────────────────


class TestPutConfigWechat:
    def test_saves_wechat_section(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config
        asyncio.run(update_config(UpdateConfigRequest(wechat={
            "enabled": True, "text_chunk_limit": 1500, "typing_enabled": False,
        })))
        wechat = _read(isolated_config)["wechat"]
        assert wechat["enabled"] is True
        assert wechat["text_chunk_limit"] == 1500
        assert wechat["typing_enabled"] is False

    def test_preserves_unsubmitted_keys_and_other_sections(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config
        isolated_config.write_text(json.dumps({
            "qq": {"enabled": True, "app_id": "1"},
            "wechat": {"enabled": False, "text_chunk_limit": 1800},
        }), encoding="utf-8")

        asyncio.run(update_config(UpdateConfigRequest(wechat={"enabled": True})))

        saved = _read(isolated_config)
        assert saved["wechat"]["enabled"] is True
        assert saved["wechat"]["text_chunk_limit"] == 1800    # 未提交的键保留
        assert saved["qq"]["app_id"] == "1"                   # 其它段不动

    def test_toggle_triggers_apply(self, isolated_config, monkeypatch):
        import web.routes_config as rc
        calls: list = []
        monkeypatch.setattr(rc, "_apply_wechat_enabled", lambda enabled: calls.append(enabled))

        asyncio.run(rc.update_config(rc.UpdateConfigRequest(wechat={"enabled": True})))

        assert calls == [True]

    def test_no_toggle_when_value_unchanged(self, isolated_config, monkeypatch):
        """开关没变就不该重启接入端（重启要停/起宿主线程，代价不小）。"""
        import web.routes_config as rc
        isolated_config.write_text(json.dumps({"wechat": {"enabled": True}}), encoding="utf-8")
        calls: list = []
        monkeypatch.setattr(rc, "_apply_wechat_enabled", lambda enabled: calls.append(enabled))

        asyncio.run(rc.update_config(rc.UpdateConfigRequest(wechat={"enabled": True})))

        assert calls == []

    def test_no_wechat_key_means_no_toggle(self, isolated_config, monkeypatch):
        """请求里没带 wechat 段 → 完全不动接入端。"""
        import web.routes_config as rc
        calls: list = []
        monkeypatch.setattr(rc, "_apply_wechat_enabled", lambda enabled: calls.append(enabled))

        asyncio.run(rc.update_config(rc.UpdateConfigRequest(system={"pip_mirror": ""})))

        assert calls == []

    def test_get_config_exposes_wechat_section(self, isolated_config):
        """前端要读 cfg.wechat；凭证不在这个段里，所以无需掩码。"""
        from web.routes_config import get_config
        isolated_config.write_text(json.dumps({
            "wechat": {"enabled": True, "text_chunk_limit": 1800},
        }), encoding="utf-8")

        data = asyncio.run(get_config())

        assert data["config"]["wechat"]["text_chunk_limit"] == 1800
