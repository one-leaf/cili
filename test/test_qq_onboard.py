"""QQ 接入端：扫码绑定机器人（onboard）——store 状态机、凭据落库、路由参数校验。

沿用 test_qq_settings_api.py 的做法：直接调端点函数 / store 方法（不用
TestClient），SDK onboard 的三个内部调用全部 monkeypatch 掉（不碰真实网络）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("qqbot_agent_sdk")

from qqbot_agent_sdk.onboard import BindStatus, _BindPollResult


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """把全局配置指向临时文件，避免读写真实 data/cili/setting.json。"""
    import core.config as config_mod
    cfg_path = tmp_path / "setting.json"
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", cfg_path)
    return cfg_path


@pytest.fixture
def fresh_store():
    """全新 store 实例（不与其它用例共享内存会话）。"""
    from interfaces.qq.onboard import QQOnboardStore
    return QQOnboardStore()


@pytest.fixture
def fake_sdk(monkeypatch):
    """把 SDK onboard 的内部调用换成可编排的假实现。

    返回的 dict 可以往里放假返回值：task / poll_results（按序弹出，空则视为
    一直 pending）/ decrypted（假解密结果）。
    """
    import qqbot_agent_sdk.onboard as sdk_onboard

    state = {
        "create_calls": 0,
        "task": SimpleNamespace(task_id="task-1", aes_key="key-1"),
        "poll_results": [],
        "decrypted": "decrypted-secret",
    }

    async def fake_create(timeout=10.0):
        state["create_calls"] += 1
        return state["task"]

    async def fake_poll(task_id, timeout=10.0):
        if state["poll_results"]:
            return state["poll_results"].pop(0)
        return _BindPollResult(
            status=BindStatus.PENDING, bot_appid="", bot_encrypt_secret="", user_openid="")

    def fake_decrypt(encrypted, key):
        return state["decrypted"]

    monkeypatch.setattr(sdk_onboard, "_create_bind_task", fake_create)
    monkeypatch.setattr(sdk_onboard, "_poll_bind_result", fake_poll)
    monkeypatch.setattr(sdk_onboard, "_decrypt_secret", fake_decrypt)
    return state


def _completed(app_id="bot-123"):
    return _BindPollResult(
        status=BindStatus.COMPLETED, bot_appid=app_id,
        bot_encrypt_secret="enc", user_openid="user-1")


def _read(cfg_path: Path) -> dict:
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def _write(cfg_path: Path, data: dict) -> None:
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ─── start：建任务 + 二维码 ───────────────────────────────────────────


class TestStart:
    def test_returns_qr_and_session(self, fresh_store, fake_sdk):
        data = asyncio.run(fresh_store.start("acc-1"))
        assert data["status"] == "pending"
        assert data["session_id"]
        assert "task-1" in data["qr_content"]
        assert data["qr_png_data_url"].startswith("data:image/png;base64,")
        assert fake_sdk["create_calls"] == 1

    def test_sessions_are_independent(self, fresh_store, fake_sdk):
        a = asyncio.run(fresh_store.start("acc-1"))["session_id"]
        b = asyncio.run(fresh_store.start("acc-2"))["session_id"]
        assert a != b


# ─── poll：状态机 + 凭据落库 ──────────────────────────────────────────


class TestPoll:
    def test_pending_keeps_waiting(self, fresh_store, fake_sdk, isolated_config):
        sid = asyncio.run(fresh_store.start("acc-1"))["session_id"]
        assert asyncio.run(fresh_store.poll(sid))["status"] == "pending"
        assert not isolated_config.exists()  # 未完成不写配置

    def test_completed_merges_new_account(self, fresh_store, fake_sdk, isolated_config):
        fake_sdk["poll_results"].append(_completed("bot-123"))
        sid = asyncio.run(fresh_store.start("acc-1"))["session_id"]
        data = asyncio.run(fresh_store.poll(sid))
        assert data["status"] == "succeeded"
        assert data["app_id"] == "bot-123"
        assert "secret" not in data          # 明文不回传浏览器
        accounts = _read(isolated_config)["qq"]["accounts"]
        assert len(accounts) == 1
        assert accounts[0]["id"] == "acc-1"
        assert accounts[0]["app_id"] == "bot-123"
        assert accounts[0]["secret"] == "decrypted-secret"
        assert accounts[0]["enabled"] is False   # 新条目不自动启用，由用户保存时定

    def test_completed_updates_existing_account_only_credentials(
            self, fresh_store, fake_sdk, isolated_config):
        _write(isolated_config, {"qq": {"accounts": [
            {"id": "acc-1", "label": "小号", "enabled": True,
             "app_id": "old", "secret": "old-secret", "msg_format": "plain"},
        ]}})
        fake_sdk["poll_results"].append(_completed("bot-9"))
        sid = asyncio.run(fresh_store.start("acc-1"))["session_id"]
        assert asyncio.run(fresh_store.poll(sid))["status"] == "succeeded"
        acc = _read(isolated_config)["qq"]["accounts"][0]
        assert acc["app_id"] == "bot-9"
        assert acc["secret"] == "decrypted-secret"
        assert acc["label"] == "小号"            # 其余字段不动
        assert acc["enabled"] is True
        assert acc["msg_format"] == "plain"

    def test_platform_expired(self, fresh_store, fake_sdk, isolated_config):
        fake_sdk["poll_results"].append(_BindPollResult(
            status=BindStatus.EXPIRED, bot_appid="", bot_encrypt_secret="", user_openid=""))
        sid = asyncio.run(fresh_store.start("acc-1"))["session_id"]
        data = asyncio.run(fresh_store.poll(sid))
        assert data["status"] == "expired"

    def test_unknown_session_reports_expired(self, fresh_store, isolated_config):
        assert asyncio.run(fresh_store.poll("nope"))["status"] == "expired"

    def test_session_consumed_after_success(self, fresh_store, fake_sdk, isolated_config):
        fake_sdk["poll_results"].append(_completed())
        sid = asyncio.run(fresh_store.start("acc-1"))["session_id"]
        assert asyncio.run(fresh_store.poll(sid))["status"] == "succeeded"
        # 二次轮询：会话已消费，不再重复落库
        assert asyncio.run(fresh_store.poll(sid))["status"] == "expired"
        assert len(_read(isolated_config)["qq"]["accounts"]) == 1


# ─── cancel ──────────────────────────────────────────────────────────


class TestCancel:
    def test_cancel_drops_session(self, fresh_store, fake_sdk, isolated_config):
        sid = asyncio.run(fresh_store.start("acc-1"))["session_id"]
        assert asyncio.run(fresh_store.cancel(sid)) == {"status": "cancelled"}
        assert asyncio.run(fresh_store.poll(sid))["status"] == "expired"


# ─── 路由：状态 / 断开连接 / 参数校验 ─────────────────────────────────


class TestRoutes:
    def test_onboard_status_requires_session_id(self):
        from fastapi import HTTPException

        from web.routes_qq import qq_onboard_status
        with pytest.raises(HTTPException) as ei:
            asyncio.run(qq_onboard_status("a1", session_id=""))
        assert ei.value.status_code == 400

    def test_onboard_cancel_requires_session_id(self):
        from fastapi import HTTPException

        from web.routes_qq import qq_onboard_cancel
        with pytest.raises(HTTPException) as ei:
            asyncio.run(qq_onboard_cancel("a1", session_id=""))
        assert ei.value.status_code == 400

    def test_status_reports_bound_and_app_id(self, isolated_config, monkeypatch):
        from web import routes_qq
        # 运行状态打桩：service._hosts 是进程级全局，前面的用例可能留了宿主在跑
        monkeypatch.setattr(routes_qq, "_interface_running", lambda _id: False)

        _write(isolated_config, {"qq": {"accounts": [
            {"id": "a1", "app_id": "123", "secret": "s3cr3t", "enabled": True},
            {"id": "a2"},
        ]}})
        data = asyncio.run(routes_qq.qq_status())
        accs = {a["id"]: a for a in data["accounts"]}
        assert accs["a1"]["bound"] is True
        assert accs["a1"]["app_id"] == "123"
        assert accs["a1"]["running"] is False
        assert accs["a2"]["bound"] is False
        for a in accs.values():
            assert "secret" not in a            # 凭据永不出配置文件

    def test_disconnect_clears_credentials(self, isolated_config):
        from web.routes_qq import qq_disconnect

        _write(isolated_config, {"qq": {"accounts": [
            {"id": "a1", "app_id": "123", "secret": "s3cr3t", "enabled": True},
        ]}})
        data = asyncio.run(qq_disconnect("a1"))
        assert data["success"] is True
        acc = _read(isolated_config)["qq"]["accounts"][0]
        assert acc["app_id"] == ""
        assert acc["secret"] == ""
        assert acc["enabled"] is True           # 启用开关不动

    def test_disconnect_unknown_account_is_noop(self, isolated_config):
        from web.routes_qq import qq_disconnect

        data = asyncio.run(qq_disconnect("ghost"))
        assert data["success"] is True
        assert data["message"] == "该账号尚未绑定"
        assert not isolated_config.exists()     # 没写配置
