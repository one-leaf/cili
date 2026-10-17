"""QQ 接入端的设置 API 测试：GET/PUT /api/config 的 qq 段 + 测试连接端点。

沿用 test_web_api.py 的做法：**直接调端点函数**（不用 TestClient——它和
asyncio.to_thread 组合会死锁，且 client IP 不在 localhost 白名单会 403）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

# 注册副作用：QQ 能力段（interface_caps）断言依赖档案已注册
import interfaces.qq.profile  # noqa: F401


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """把全局配置指向临时文件，避免读写真实 data/cili/setting.json。"""
    import core.config as config_mod
    cfg_path = tmp_path / "setting.json"
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", cfg_path)
    return cfg_path


def _read(cfg_path: Path) -> dict:
    return json.loads(cfg_path.read_text(encoding="utf-8"))


class TestGetConfigMasksQQSecret:
    def test_masks_secret_and_hides_plaintext(self, isolated_config):
        from web.routes_config import get_config
        isolated_config.write_text(json.dumps({
            "qq": {"enabled": True, "app_id": "123", "secret": "supersecretvalue"},
        }), encoding="utf-8")

        data = asyncio.run(get_config())
        qq = data["config"]["qq"]
        assert "secret" not in qq                    # 明文绝不出网
        assert qq["secret_masked"].startswith("supe")
        assert qq["app_id"] == "123"

    def test_no_secret_no_masked_field(self, isolated_config):
        from web.routes_config import get_config
        isolated_config.write_text(json.dumps({"qq": {"enabled": False}}), encoding="utf-8")
        qq = asyncio.run(get_config())["config"]["qq"]
        assert "secret_masked" not in qq


class TestPutConfigQQ:
    def test_saves_qq_section(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config
        req = UpdateConfigRequest(qq={
            "enabled": True, "app_id": "999", "secret": "s3cr3t",
            "use_sandbox": False, "msg_format": "plain",
        })
        asyncio.run(update_config(req))
        qq = _read(isolated_config)["qq"]
        assert qq["app_id"] == "999"
        assert qq["secret"] == "s3cr3t"
        assert qq["use_sandbox"] is False

    def test_blank_secret_preserves_existing(self, isolated_config):
        """前端只回显掩码，不回传明文 → 留空必须保留原 secret，不能清掉。"""
        from web.routes_config import UpdateConfigRequest, update_config
        isolated_config.write_text(json.dumps({
            "qq": {"enabled": False, "app_id": "1", "secret": "original-secret"},
        }), encoding="utf-8")

        asyncio.run(update_config(UpdateConfigRequest(qq={
            "enabled": False, "app_id": "2",          # 不带 secret
        })))
        qq = _read(isolated_config)["qq"]
        assert qq["secret"] == "original-secret"      # 保留
        assert qq["app_id"] == "2"                    # 其它字段更新

    def test_other_sections_untouched(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config
        isolated_config.write_text(json.dumps({
            "providers": {"p": {"api_key": "k", "models": {"m": {"name": "keep-me"}}}},
            "agents": {"master": {"model": "p/m"}},
            "qq": {"enabled": False},
        }), encoding="utf-8")
        asyncio.run(update_config(UpdateConfigRequest(qq={"enabled": True})))
        after = _read(isolated_config)
        assert after["providers"]["p"]["models"]["m"]["name"] == "keep-me"
        assert after["agents"]["master"]["model"] == "p/m"

    def test_toggle_starts_interface(self, isolated_config, monkeypatch):
        """enabled 从关到开 → 就地启动接入端（不重启 Cili）。"""
        from web.routes_config import UpdateConfigRequest, update_config
        isolated_config.write_text(json.dumps({"qq": {"enabled": False}}), encoding="utf-8")

        calls = []
        import interfaces.qq as qq_pkg
        monkeypatch.setattr(qq_pkg, "start_qq_interface", lambda: calls.append("start") or True)
        monkeypatch.setattr(qq_pkg, "stop_qq_interface", lambda: calls.append("stop"))

        asyncio.run(update_config(UpdateConfigRequest(qq={"enabled": True})))
        assert calls == ["start"]

    def test_toggle_stops_interface(self, isolated_config, monkeypatch):
        from web.routes_config import UpdateConfigRequest, update_config
        isolated_config.write_text(json.dumps({"qq": {"enabled": True}}), encoding="utf-8")

        calls = []
        import interfaces.qq as qq_pkg
        monkeypatch.setattr(qq_pkg, "start_qq_interface", lambda: calls.append("start") or True)
        monkeypatch.setattr(qq_pkg, "stop_qq_interface", lambda: calls.append("stop"))

        asyncio.run(update_config(UpdateConfigRequest(qq={"enabled": False})))
        assert calls == ["stop"]

    def test_no_toggle_when_unchanged(self, isolated_config, monkeypatch):
        """enabled 没变 → 不动接口（避免每次保存都重连）。"""
        from web.routes_config import UpdateConfigRequest, update_config
        isolated_config.write_text(json.dumps({"qq": {"enabled": True}}), encoding="utf-8")

        calls = []
        import interfaces.qq as qq_pkg
        monkeypatch.setattr(qq_pkg, "start_qq_interface", lambda: calls.append("start") or True)
        monkeypatch.setattr(qq_pkg, "stop_qq_interface", lambda: calls.append("stop"))

        asyncio.run(update_config(UpdateConfigRequest(qq={"enabled": True, "app_id": "x"})))
        assert calls == []

    def test_start_failure_does_not_fail_save(self, isolated_config, monkeypatch):
        """启停失败不该让保存请求失败（配置已落盘）。"""
        from web.routes_config import UpdateConfigRequest, update_config
        isolated_config.write_text(json.dumps({"qq": {"enabled": False}}), encoding="utf-8")

        import interfaces.qq as qq_pkg

        def boom():
            raise RuntimeError("SDK 炸了")

        monkeypatch.setattr(qq_pkg, "start_qq_interface", boom)
        asyncio.run(update_config(UpdateConfigRequest(qq={"enabled": True})))
        assert _read(isolated_config)["qq"]["enabled"] is True


class TestTestQQEndpoint:
    def test_reports_missing_sdk_clearly(self, isolated_config, monkeypatch):
        """没装 SDK 时给出可读原因，而不是 500。"""
        import interfaces.qq.gateway as gw
        from web.routes_config import QQTestRequest, test_qq_connection

        monkeypatch.setattr(gw, "SDK_AVAILABLE", False)
        monkeypatch.setattr(gw, "_IMPORT_ERROR", "No module named 'qqbot_agent_sdk'")

        out = asyncio.run(test_qq_connection(QQTestRequest(app_id="a", secret="s")))
        assert out["success"] is False
        assert "qqbot-agent-sdk" in out["message"]

    def test_reports_missing_credentials(self, isolated_config, monkeypatch):
        import interfaces.qq.gateway as gw
        from web.routes_config import QQTestRequest, test_qq_connection

        monkeypatch.setattr(gw, "SDK_AVAILABLE", True)
        out = asyncio.run(test_qq_connection(QQTestRequest(app_id="", secret="")))
        assert out["success"] is False
        assert "未填写" in out["message"]

    def test_falls_back_to_saved_credentials(self, isolated_config, monkeypatch):
        """未传 secret 时用已保存的。"""
        import interfaces.qq.gateway as gw
        from web.routes_config import QQTestRequest, test_qq_connection

        isolated_config.write_text(json.dumps({
            "qq": {"app_id": "saved-app", "secret": "saved-secret", "use_sandbox": True},
        }), encoding="utf-8")

        seen = {}

        async def fake_test(app_id, secret, use_sandbox):
            seen.update(app_id=app_id, secret=secret, use_sandbox=use_sandbox)
            return True, "ok"

        monkeypatch.setattr(gw, "test_connection", fake_test)
        out = asyncio.run(test_qq_connection(QQTestRequest()))
        assert out["success"] is True
        assert seen == {"app_id": "saved-app", "secret": "saved-secret", "use_sandbox": True}


class TestApplyApiBase:
    def test_sets_env_var(self, monkeypatch):
        import os

        import interfaces.qq.gateway as gw
        monkeypatch.setattr(gw, "SDK_AVAILABLE", False)
        gw.apply_api_base(True)
        assert os.environ.get("QQ_API_BASE") == gw.SANDBOX_API_BASE
        gw.apply_api_base(False)
        assert os.environ.get("QQ_API_BASE") == gw.PROD_API_BASE

    def test_patches_both_module_copies(self, monkeypatch):
        """SDK 是 `from .constants import API_BASE` 的按值导入 —— 两处都要打补丁。

        这里用假模块验证补丁逻辑本身（真实 SDK 未必安装）。
        """
        import sys
        import types

        import interfaces.qq.gateway as gw

        fake_pkg = types.ModuleType("qqbot_agent_sdk")
        fake_const = types.ModuleType("qqbot_agent_sdk.constants")
        fake_ac = types.ModuleType("qqbot_agent_sdk.api_client")
        fake_const.API_BASE = gw.PROD_API_BASE
        fake_ac.API_BASE = gw.PROD_API_BASE
        fake_pkg.constants = fake_const
        fake_pkg.api_client = fake_ac
        monkeypatch.setitem(sys.modules, "qqbot_agent_sdk", fake_pkg)
        monkeypatch.setitem(sys.modules, "qqbot_agent_sdk.constants", fake_const)
        monkeypatch.setitem(sys.modules, "qqbot_agent_sdk.api_client", fake_ac)
        monkeypatch.setattr(gw, "SDK_AVAILABLE", True)

        assert gw.apply_api_base(True) == gw.SANDBOX_API_BASE
        assert fake_const.API_BASE == gw.SANDBOX_API_BASE
        assert fake_ac.API_BASE == gw.SANDBOX_API_BASE     # 关键：这份副本也要改
        assert gw.effective_api_base() == gw.SANDBOX_API_BASE


class TestSendFilePromptFollowsEnabledFlag:
    """`send_file` 的工具与提示词都由「是否启用 QQ」间接控制。

    机制：只有 QQ 接入端在运行时才会调 `set_turn_interface("qq")`；关闭后没有任何
    回合带这个接口标记，所以工具与 QQ 能力段（Response Surface）都不会出现。
    这里把这条链验证死。
    """

    def test_disabled_interface_does_not_start(self, isolated_config, monkeypatch):
        """未启用时 start_qq_interface 直接返回 False（不加载 SDK、不起线程）。"""
        import interfaces.qq.service as svc
        monkeypatch.setattr(svc, "_host", None)
        monkeypatch.setattr(svc, "load_qq_config",
                            lambda: __import__("interfaces.qq.config", fromlist=["x"]).QQConfig(enabled=False))
        assert svc.start_qq_interface() is False
        assert svc.qq_interface_running() is False

    def test_undeclared_turn_has_no_qq_tools_or_caps(self, runner):
        """未声明接口的回合：既没有 send_file，也没有 QQ 能力段。"""
        assert "send_file" not in {s["name"] for s in runner.tool_schemas}
        assert "QQ private chat" not in "\n".join(runner._build_system_prompt())

    def test_qq_turn_has_both(self, runner):
        runner.set_turn_interface("qq")
        assert "send_file" in {s["name"] for s in runner.tool_schemas}
        assert "QQ private chat" in "\n".join(runner._build_system_prompt())

    def test_clearing_interface_removes_both(self, runner):
        runner.set_turn_interface("qq")
        runner.set_turn_interface(None)
        assert "send_file" not in {s["name"] for s in runner.tool_schemas}
        assert "QQ private chat" not in "\n".join(runner._build_system_prompt())
