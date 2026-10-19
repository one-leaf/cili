"""QQ 接入端的设置 API 测试：GET/PUT /api/config 的 qq 段 + 测试连接端点。

沿用 test_web_api.py 的做法：**直接调端点函数**（不用 TestClient——它和
asyncio.to_thread 组合会死锁，且 client IP 不在 localhost 白名单会 403）。

多账号后（设计 §7.1）：qq 段是 ``{"accounts": [...]}``；PUT 全量替换、
secret 留空/掩码 = 保留原值；开关变化按账号 diff 启停。
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


def _write(cfg_path: Path, data: dict) -> None:
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ─── GET：逐账号掩码 secret ───────────────────────────────────────────


class TestGetConfigQQAccounts:
    def test_masks_secret_per_account(self, isolated_config):
        from web.routes_config import get_config
        _write(isolated_config, {"qq": {"accounts": [
            {"id": "a1", "app_id": "123", "secret": "supersecretvalue", "enabled": True},
        ]}})

        data = asyncio.run(get_config())
        acct = data["config"]["qq"]["accounts"][0]
        assert "secret" not in acct                 # 明文绝不出网
        assert acct["secret_masked"].startswith("supe")
        assert acct["app_id"] == "123"
        assert acct["id"] == "a1"

    def test_no_secret_no_masked_field(self, isolated_config):
        from web.routes_config import get_config
        _write(isolated_config, {"qq": {"accounts": [{"id": "a1", "app_id": "1"}]}})
        acct = asyncio.run(get_config())["config"]["qq"]["accounts"][0]
        assert "secret_masked" not in acct

    def test_multiple_accounts_masked_independently(self, isolated_config):
        from web.routes_config import get_config
        _write(isolated_config, {"qq": {"accounts": [
            {"id": "a1", "secret": "first-secret"},
            {"id": "a2"},
        ]}})
        accounts = asyncio.run(get_config())["config"]["qq"]["accounts"]
        assert accounts[0]["secret_masked"].startswith("firs")
        assert "secret_masked" not in accounts[1]


# ─── PUT：账号列表全量替换 + secret 保留 ──────────────────────────────


class TestPutConfigQQ:
    def test_saves_accounts(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config
        req = UpdateConfigRequest(qq={"accounts": [
            {"id": "a1", "app_id": "999", "secret": "s3cr3t",
             "enabled": True, "msg_format": "plain"},
        ]})
        asyncio.run(update_config(req))
        saved = _read(isolated_config)["qq"]["accounts"]
        assert saved[0]["app_id"] == "999"
        assert saved[0]["secret"] == "s3cr3t"
        assert saved[0]["msg_format"] == "plain"

    def test_assigns_missing_id(self, isolated_config):
        """前端新增的卡片可能没带 id（比如手工 curl），后端补一个。"""
        from web.routes_config import UpdateConfigRequest, update_config
        asyncio.run(update_config(UpdateConfigRequest(qq={"accounts": [
            {"app_id": "999", "secret": "s"},
        ]})))
        saved = _read(isolated_config)["qq"]["accounts"]
        assert saved[0]["id"]

    def test_blank_secret_preserves_existing(self, isolated_config):
        """前端只回显掩码，不回传明文 → 留空必须保留原 secret，不能清掉。"""
        from web.routes_config import UpdateConfigRequest, update_config
        _write(isolated_config, {"qq": {"accounts": [
            {"id": "a1", "app_id": "1", "secret": "original-secret"},
        ]}})

        asyncio.run(update_config(UpdateConfigRequest(qq={"accounts": [
            {"id": "a1", "app_id": "2"},          # 不带 secret
        ]})))
        acct = _read(isolated_config)["qq"]["accounts"][0]
        assert acct["secret"] == "original-secret"      # 保留
        assert acct["app_id"] == "2"                    # 其它字段更新

    def test_masked_secret_preserves_existing(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config
        _write(isolated_config, {"qq": {"accounts": [
            {"id": "a1", "app_id": "1", "secret": "original-secret"},
        ]}})
        asyncio.run(update_config(UpdateConfigRequest(qq={"accounts": [
            {"id": "a1", "secret": "orig...cret"},
        ]})))
        assert _read(isolated_config)["qq"]["accounts"][0]["secret"] == "original-secret"

    def test_other_sections_untouched(self, isolated_config):
        from web.routes_config import UpdateConfigRequest, update_config
        _write(isolated_config, {
            "providers": {"p": {"api_key": "k", "models": {"m": {"name": "keep-me"}}}},
            "agents": {"master": {"model": "p/m"}},
            "qq": {"accounts": [{"id": "a1", "enabled": False}]},
        })
        asyncio.run(update_config(UpdateConfigRequest(qq={"accounts": [
            {"id": "a1", "enabled": True},
        ]})))
        after = _read(isolated_config)
        assert after["providers"]["p"]["models"]["m"]["name"] == "keep-me"
        assert after["agents"]["master"]["model"] == "p/m"


# ─── 开关变化：按账号 diff 启停（设计 §7.1） ──────────────────────────


class TestAccountToggleDiff:
    def test_toggle_starts_only_changed_account(self, isolated_config, monkeypatch):
        from web.routes_config import UpdateConfigRequest, update_config
        _write(isolated_config, {"qq": {"accounts": [
            {"id": "a1", "app_id": "1", "enabled": False},
            {"id": "a2", "app_id": "2", "enabled": True},
        ]}})
        import interfaces.qq as qq_pkg
        calls = []
        monkeypatch.setattr(
            qq_pkg, "start_qq_interface",
            lambda account_id=None: calls.append(("start", account_id)) or True,
        )
        monkeypatch.setattr(
            qq_pkg, "stop_qq_interface", lambda account_id=None: calls.append(("stop", account_id))
        )

        asyncio.run(update_config(UpdateConfigRequest(qq={"accounts": [
            {"id": "a1", "app_id": "1", "enabled": True},
            {"id": "a2", "app_id": "2", "enabled": True},     # 未变 → 不动
        ]})))
        assert calls == [("start", "a1")]

    def test_toggle_stops_changed_account(self, isolated_config, monkeypatch):
        from web.routes_config import UpdateConfigRequest, update_config
        _write(isolated_config, {"qq": {"accounts": [{"id": "a1", "enabled": True}]}})
        import interfaces.qq as qq_pkg
        calls = []
        monkeypatch.setattr(
            qq_pkg, "start_qq_interface",
            lambda account_id=None: calls.append(("start", account_id)) or True,
        )
        monkeypatch.setattr(
            qq_pkg, "stop_qq_interface", lambda account_id=None: calls.append(("stop", account_id))
        )

        asyncio.run(update_config(UpdateConfigRequest(qq={"accounts": [
            {"id": "a1", "enabled": False},
        ]})))
        assert calls == [("stop", "a1")]

    def test_removed_account_stops_its_host(self, isolated_config, monkeypatch):
        """前端删号 → 被删账号的宿主也要停掉。"""
        from web.routes_config import UpdateConfigRequest, update_config
        _write(isolated_config, {"qq": {"accounts": [
            {"id": "a1", "app_id": "1", "enabled": True},
        ]}})
        import web.routes_config as rc
        calls = []
        monkeypatch.setattr(
            rc, "_apply_account_enabled_changes",
            lambda iface, b, a: calls.append((iface, dict(b), dict(a))),
        )
        asyncio.run(update_config(UpdateConfigRequest(qq={"accounts": []})))
        assert calls == [("qq", {"a1": True}, {})]

    def test_no_toggle_when_unchanged(self, isolated_config, monkeypatch):
        """enabled 没变 → 不动接口（避免每次保存都重连）。"""
        from web.routes_config import UpdateConfigRequest, update_config
        _write(isolated_config, {"qq": {"accounts": [{"id": "a1", "enabled": True}]}})
        import interfaces.qq as qq_pkg
        calls = []
        monkeypatch.setattr(
            qq_pkg, "start_qq_interface",
            lambda account_id=None: calls.append(("start", account_id)) or True,
        )
        monkeypatch.setattr(
            qq_pkg, "stop_qq_interface", lambda account_id=None: calls.append(("stop", account_id))
        )

        asyncio.run(update_config(UpdateConfigRequest(qq={"accounts": [
            {"id": "a1", "enabled": True, "app_id": "x"},
        ]})))
        assert calls == []

    def test_start_failure_does_not_fail_save(self, isolated_config, monkeypatch):
        """启停失败不该让保存请求失败（配置已落盘）。"""
        from web.routes_config import UpdateConfigRequest, update_config
        _write(isolated_config, {"qq": {"accounts": [{"id": "a1", "enabled": False}]}})

        import interfaces.qq as qq_pkg

        def boom(account_id=None):
            raise RuntimeError("SDK 炸了")

        monkeypatch.setattr(qq_pkg, "start_qq_interface", boom)
        asyncio.run(update_config(UpdateConfigRequest(qq={"accounts": [
            {"id": "a1", "enabled": True},
        ]})))
        saved = _read(isolated_config)["qq"]["accounts"]
        assert saved[0]["enabled"] is True

    def test_wechat_toggle_uses_wechat_interface(self, isolated_config, monkeypatch):
        """微信段的 diff 启停走 interfaces.wechat 的启停函数。"""
        from web.routes_config import UpdateConfigRequest, update_config
        _write(isolated_config, {"wechat": {"accounts": [{"id": "w1", "enabled": False}]}})
        import interfaces.wechat as wechat_pkg
        calls = []
        monkeypatch.setattr(
            wechat_pkg, "start_wechat_interface",
            lambda account_id=None: calls.append(("start", account_id)) or True,
        )
        monkeypatch.setattr(
            wechat_pkg, "stop_wechat_interface",
            lambda account_id=None: calls.append(("stop", account_id)),
        )

        asyncio.run(update_config(UpdateConfigRequest(wechat={"accounts": [
            {"id": "w1", "enabled": True},
        ]})))
        assert calls == [("start", "w1")]


# ─── 测试连接端点（用进程当前域名，不再有 use_sandbox） ───────────────


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
        """未传 secret 时用已保存账号的。"""
        import interfaces.qq.gateway as gw
        from web.routes_config import QQTestRequest, test_qq_connection

        _write(isolated_config, {"qq": {"accounts": [
            {"id": "a1", "app_id": "saved-app", "secret": "saved-secret"},
        ]}})

        seen: dict = {}

        async def fake_test(app_id, secret):
            seen.update(app_id=app_id, secret=secret)
            return True, "ok"

        monkeypatch.setattr(gw, "test_connection", fake_test)
        out = asyncio.run(test_qq_connection(QQTestRequest()))
        assert out["success"] is True
        assert seen == {"app_id": "saved-app", "secret": "saved-secret"}

    def test_prefers_matching_account(self, isolated_config, monkeypatch):
        """带 account_id 时用对应账号的已存凭据，而不是第一份。"""
        import interfaces.qq.gateway as gw
        from web.routes_config import QQTestRequest, test_qq_connection

        _write(isolated_config, {"qq": {"accounts": [
            {"id": "a1", "app_id": "app-1", "secret": "s1"},
            {"id": "a2", "app_id": "app-2", "secret": "s2"},
        ]}})

        seen: dict = {}

        async def fake_test(app_id, secret):
            seen.update(app_id=app_id, secret=secret)
            return True, "ok"

        monkeypatch.setattr(gw, "test_connection", fake_test)
        asyncio.run(test_qq_connection(QQTestRequest(account_id="a2")))
        assert seen == {"app_id": "app-2", "secret": "s2"}


# ─── API 域名：进程级默认 + 环境变量覆盖（设计 §4.2） ─────────────────


class TestApiDomainDefault:
    def test_effective_api_base_reads_env(self, monkeypatch):
        import interfaces.qq.gateway as gw
        monkeypatch.setenv("QQ_API_BASE", gw.PROD_API_BASE)
        assert gw.effective_api_base() == gw.PROD_API_BASE

    def test_effective_api_base_defaults_to_sandbox(self, monkeypatch):
        import interfaces.qq.gateway as gw
        monkeypatch.delenv("QQ_API_BASE", raising=False)
        assert gw.effective_api_base() == gw.SANDBOX_API_BASE

    def test_setdefault_does_not_override_user_env(self, monkeypatch):
        """gateway 导入时 setdefault：用户显式设置的值不被覆盖（正式环境部署常规做法）。"""
        import interfaces.qq.gateway as gw
        monkeypatch.setenv("QQ_API_BASE", gw.PROD_API_BASE)
        gw.os.environ.setdefault("QQ_API_BASE", gw.SANDBOX_API_BASE)
        assert gw.os.environ["QQ_API_BASE"] == gw.PROD_API_BASE


# ─── 配置对象：白名单字段进文件，调参字段固定默认（设计 §3.2.1） ──────


class TestQQAccountsConfig:
    def test_from_dict_reads_whitelist_only(self):
        """调参键（flush/上限）不再从配置读取——统一默认值。"""
        from interfaces.qq.config import QQConfig
        cfg = QQConfig.from_dict({
            "id": "a1", "enabled": True, "app_id": "x",
            "flush_interval_seconds": 99,       # 旧调参键 → 忽略
            "max_outbound_files": 99,           # 旧调参键 → 忽略
        })
        assert cfg.enabled is True
        assert cfg.app_id == "x"
        assert cfg.flush_interval_seconds == 1.5
        assert cfg.max_outbound_files == 3

    def test_roundtrip_keeps_persist_fields_only(self):
        from interfaces.qq.config import QQConfig
        cfg = QQConfig(id="a1", enabled=True, app_id="a", secret="s", msg_format="plain")
        restored = QQConfig.from_dict(cfg.to_dict())
        assert restored == cfg
        assert "flush_interval_seconds" not in cfg.to_dict()

    def test_bad_type_falls_back(self):
        from interfaces.qq.config import QQConfig
        cfg = QQConfig.from_dict({"workspace_locked": "yes", "enabled": 1})
        assert cfg.workspace_locked is True
        assert cfg.enabled is True

    def test_load_qq_accounts_compat_flat_section(self, isolated_config):
        """旧扁平段按「整段即一个账号」兼容读，id 取 app_id（设计 §3.1）。"""
        import interfaces.qq.config as cfg_mod
        from interfaces.qq.config import load_qq_accounts
        _write(isolated_config, {"qq": {
            "enabled": True, "app_id": "102849133", "secret": "s",
            "use_sandbox": False, "msg_format": "markdown",
        }})
        accounts = cfg_mod.load_qq_accounts() or load_qq_accounts()
        assert len(accounts) == 1
        assert accounts[0].id == "102849133"
        assert accounts[0].secret == "s"
        assert accounts[0].enabled is True

    def test_load_qq_accounts_new_format(self, isolated_config):
        from interfaces.qq.config import load_qq_accounts
        _write(isolated_config, {"qq": {"accounts": [
            {"id": "a1", "app_id": "1", "enabled": True},
            {"id": "a2", "app_id": "2", "enabled": False},
        ]}})
        accounts = load_qq_accounts()
        assert [a.id for a in accounts] == ["a1", "a2"]

    def test_load_qq_config_prefers_ready_enabled(self, isolated_config):
        from interfaces.qq.config import QQConfig, load_qq_config, save_qq_accounts
        save_qq_accounts([
            QQConfig(id="a1", enabled=True, app_id=""),
            QQConfig(id="a2", enabled=True, app_id="x", secret="s"),
        ])
        assert load_qq_config().id == "a2"      # a1 凭据不齐 → 取 a2

    def test_save_qq_accounts_writes_new_format(self, isolated_config):
        import json

        from interfaces.qq.config import QQConfig, save_qq_accounts
        assert save_qq_accounts([QQConfig(id="a1", enabled=True, app_id="1", secret="s")])
        data = json.loads(isolated_config.read_text(encoding="utf-8"))
        assert data["qq"] == {"accounts": [
            {"id": "a1", "label": "", "enabled": True, "default_workspace_uuid": "",
             "workspace_locked": False, "app_id": "1", "secret": "s", "msg_format": "markdown"},
        ]}


class TestSendFilePromptFollowsEnabledFlag:
    """`send_user_file` 的工具与提示词都由「是否有 QQ 账号在跑」间接控制。

    机制：只有 QQ 接入端在运行时才会调 `set_turn_interface("qq")`；关闭后没有任何
    回合带这个接口标记，所以工具与 QQ 能力段（Response Surface）都不会出现。
    这里把这条链验证死。
    """

    def test_disabled_interface_does_not_start(self, isolated_config, monkeypatch):
        """未启用时 start_qq_interface 返回 False（不加载 SDK、不起线程）。"""
        import interfaces.qq.service as svc
        from interfaces.qq.config import QQConfig
        monkeypatch.setattr(svc, "_hosts", {})
        monkeypatch.setattr(svc, "load_qq_accounts", lambda: [QQConfig(id="a1", enabled=False)])
        assert svc.start_qq_interface() is False
        assert svc.qq_interface_running() is False

    def test_undeclared_turn_has_no_qq_tools_or_caps(self, runner):
        """未声明接口的回合：既没有 send_user_file，也没有 QQ 能力段。"""
        assert "send_user_file" not in {s["name"] for s in runner.tool_schemas}
        assert "QQ private chat" not in "\n".join(runner._build_system_prompt())

    def test_qq_turn_has_both(self, runner):
        runner.set_turn_interface("qq")
        assert "send_user_file" in {s["name"] for s in runner.tool_schemas}
        assert "QQ private chat" in "\n".join(runner._build_system_prompt())

    def test_clearing_interface_removes_both(self, runner):
        runner.set_turn_interface("qq")
        runner.set_turn_interface(None)
        assert "send_user_file" not in {s["name"] for s in runner.tool_schemas}
        assert "QQ private chat" not in "\n".join(runner._build_system_prompt())
