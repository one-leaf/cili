"""接入端多账号迁移测试（设计 §9）。

覆盖四类数据：setting.json 的 qq/wechat 段、两侧 state、微信凭证、QQ ws 会话。
幂等与「配置已迁移而状态未迁移」的续接路径是重点。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import core.config as config_mod
    import core.migration as mig
    cfg_path = tmp_path / "setting.json"
    data_dir = tmp_path / "cili"
    monkeypatch.setattr(mig, "GLOBAL_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(mig, "DATA_DIR", data_dir)
    return mig, tmp_path, data_dir


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestConfigSectionMigration:
    def test_flat_qq_section_becomes_accounts(self, isolated):
        mig, _, _ = isolated
        _write_json(mig.GLOBAL_CONFIG_PATH, {
            "qq": {"enabled": True, "app_id": "102849133", "secret": "s",
                   "msg_format": "markdown", "flush_interval_seconds": 9},
        })
        mig.migrate_interfaces_to_multi_account()
        section = _read_json(mig.GLOBAL_CONFIG_PATH)["qq"]
        assert section["accounts"][0]["id"] == "102849133"   # id 取 app_id（§3.1）
        assert section["accounts"][0]["secret"] == "s"
        assert section["accounts"][0]["enabled"] is True
        assert "flush_interval_seconds" not in section["accounts"][0]   # 调参键丢弃

    def test_use_sandbox_false_warns_about_env(self, isolated, caplog):
        """旧 use_sandbox=false（正式环境）无法再按账号表达 → 提示设环境变量。"""
        mig, _, _ = isolated
        _write_json(mig.GLOBAL_CONFIG_PATH, {
            "qq": {"enabled": True, "app_id": "1", "secret": "s", "use_sandbox": False},
        })
        with caplog.at_level("WARNING"):
            mig.migrate_interfaces_to_multi_account()
        assert any("QQ_API_BASE" in r.message for r in caplog.records)

    def test_flat_wechat_section_uses_legacy_bot_id(self, isolated):
        mig, _, data_dir = isolated
        _write_json(data_dir / "tools" / "wechat" / "account.json", {"ilink_bot_id": "b1"})
        _write_json(mig.GLOBAL_CONFIG_PATH, {"wechat": {"enabled": True}})
        mig.migrate_interfaces_to_multi_account()
        section = _read_json(mig.GLOBAL_CONFIG_PATH)["wechat"]
        assert section["accounts"][0]["id"] == "b1"

    def test_wechat_without_credentials_falls_back_to_default_id(self, isolated):
        mig, _, _ = isolated
        _write_json(mig.GLOBAL_CONFIG_PATH, {"wechat": {"enabled": True}})
        mig.migrate_interfaces_to_multi_account()
        section = _read_json(mig.GLOBAL_CONFIG_PATH)["wechat"]
        assert section["accounts"][0]["id"] == "default"

    def test_idempotent(self, isolated):
        mig, _, _ = isolated
        _write_json(mig.GLOBAL_CONFIG_PATH, {
            "qq": {"enabled": True, "app_id": "1", "secret": "s"},
            "wechat": {"enabled": True},
        })
        mig.migrate_interfaces_to_multi_account()
        first = _read_json(mig.GLOBAL_CONFIG_PATH)
        mig.migrate_interfaces_to_multi_account()
        assert _read_json(mig.GLOBAL_CONFIG_PATH) == first

    def test_backup_created(self, isolated):
        mig, _, _ = isolated
        _write_json(mig.GLOBAL_CONFIG_PATH, {"qq": {"enabled": True, "app_id": "1"}})
        mig.migrate_interfaces_to_multi_account()
        assert mig.GLOBAL_CONFIG_PATH.with_name("setting.json.bak").exists()


class TestStateMigration:
    def test_qq_state_keyed_by_app_id(self, isolated):
        mig, _, data_dir = isolated
        _write_json(mig.GLOBAL_CONFIG_PATH, {"qq": {"enabled": True, "app_id": "app1"}})
        _write_json(data_dir / "tools" / "qq" / "state.json", {
            "owner_openid": "o1", "workspace_uuid": "w1", "session_id": "s1",
        })
        mig.migrate_interfaces_to_multi_account()
        state = _read_json(data_dir / "tools" / "qq" / "state.json")
        acct = state["accounts"]["app1"]
        assert acct["owner"] == "o1"
        assert acct["chats"]["o1"] == {"workspace_uuid": "w1", "session_id": "s1"}

    def test_wechat_state_keyed_by_bot_id(self, isolated):
        mig, _, data_dir = isolated
        _write_json(data_dir / "tools" / "wechat" / "account.json", {"ilink_bot_id": "b1"})
        _write_json(mig.GLOBAL_CONFIG_PATH, {"wechat": {"enabled": True}})
        _write_json(data_dir / "tools" / "wechat" / "state.json", {
            "owner_user_id": "u1", "workspace_uuid": "w1", "session_id": "s1",
        })
        mig.migrate_interfaces_to_multi_account()
        state = _read_json(data_dir / "tools" / "wechat" / "state.json")
        assert state["accounts"]["b1"]["chats"]["u1"]["workspace_uuid"] == "w1"

    def test_state_migration_continues_after_partial_failure(self, isolated):
        """配置已迁移（前次跑完）而状态还是旧形态 → 用第一个账号的 id 续接（§9.2）。"""
        mig, _, data_dir = isolated
        _write_json(mig.GLOBAL_CONFIG_PATH, {"qq": {"accounts": [
            {"id": "app1", "app_id": "app1", "enabled": True},
        ]}})
        _write_json(data_dir / "tools" / "qq" / "state.json", {
            "owner_openid": "o1", "workspace_uuid": "w1", "session_id": "s1",
        })
        mig.migrate_interfaces_to_multi_account()
        state = _read_json(data_dir / "tools" / "qq" / "state.json")
        assert "app1" in state["accounts"]

    def test_state_idempotent(self, isolated):
        mig, _, data_dir = isolated
        _write_json(mig.GLOBAL_CONFIG_PATH, {"qq": {"enabled": True, "app_id": "app1"}})
        state_path = data_dir / "tools" / "qq" / "state.json"
        _write_json(state_path, {"owner_openid": "o1", "workspace_uuid": "w1", "session_id": "s1"})
        mig.migrate_interfaces_to_multi_account()
        first = _read_json(state_path)
        mig.migrate_interfaces_to_multi_account()
        assert _read_json(state_path) == first

    def test_no_state_file_noop(self, isolated):
        """没装过接入端的全新实例：迁移只动配置，不凭空造状态文件。"""
        mig, _, data_dir = isolated
        _write_json(mig.GLOBAL_CONFIG_PATH, {"qq": {"enabled": True, "app_id": "app1"}})
        mig.migrate_interfaces_to_multi_account()      # 不应抛错
        assert not (data_dir / "tools" / "qq" / "state.json").exists()
        assert not (data_dir / "tools" / "wechat" / "state.json").exists()


class TestCredentialAndWsSessionMigration:
    def test_wechat_account_file_moves_to_accounts_dir(self, isolated):
        mig, _, data_dir = isolated
        _write_json(mig.GLOBAL_CONFIG_PATH, {"wechat": {"enabled": True}})
        legacy = data_dir / "tools" / "wechat" / "account.json"
        _write_json(legacy, {"bot_token": "tok", "ilink_bot_id": "b1"})
        mig.migrate_interfaces_to_multi_account()
        new_path = data_dir / "tools" / "wechat" / "accounts" / "b1.json"
        assert _read_json(new_path)["bot_token"] == "tok"
        assert not legacy.exists()                     # 旧文件删除（§5.1）
        assert legacy.with_name("account.json.bak").exists()   # 备份保留

    def test_wechat_account_migration_idempotent(self, isolated):
        mig, _, data_dir = isolated
        _write_json(mig.GLOBAL_CONFIG_PATH, {"wechat": {"enabled": True}})
        legacy = data_dir / "tools" / "wechat" / "account.json"
        _write_json(legacy, {"bot_token": "tok", "ilink_bot_id": "b1"})
        mig.migrate_interfaces_to_multi_account()
        content = _read_json(data_dir / "tools" / "wechat" / "accounts" / "b1.json")
        mig.migrate_interfaces_to_multi_account()      # 目标已存在 → 跳过
        assert _read_json(data_dir / "tools" / "wechat" / "accounts" / "b1.json") == content

    def test_qq_ws_sessions_split_by_app_id(self, isolated):
        """单文件按 app_id 拆到每账号目录（设计 §4.5：整读整写会互相覆盖）。"""
        mig, _, data_dir = isolated
        _write_json(data_dir / "tools" / "qq" / "ws_sessions.json", {
            "app1": {"session_id": "s1", "seq": 3},
        })
        mig.migrate_interfaces_to_multi_account()
        new_path = data_dir / "tools" / "qq" / "app1" / "ws_sessions.json"
        assert _read_json(new_path) == {"app1": {"session_id": "s1", "seq": 3}}
        assert not (data_dir / "tools" / "qq" / "ws_sessions.json").exists()

    def test_qq_ws_sessions_idempotent(self, isolated):
        mig, _, data_dir = isolated
        _write_json(data_dir / "tools" / "qq" / "ws_sessions.json", {
            "app1": {"session_id": "s1", "seq": 3},
        })
        mig.migrate_interfaces_to_multi_account()
        content = _read_json(data_dir / "tools" / "qq" / "app1" / "ws_sessions.json")
        mig.migrate_interfaces_to_multi_account()
        assert _read_json(data_dir / "tools" / "qq" / "app1" / "ws_sessions.json") == content

    def test_multi_account_legacy_ws_sessions_all_split(self, isolated):
        """（手工编辑出的）多键旧文件：每个 app_id 各自拆目录。"""
        mig, _, data_dir = isolated
        _write_json(data_dir / "tools" / "qq" / "ws_sessions.json", {
            "app1": {"session_id": "s1", "seq": 1},
            "app2": {"session_id": "s2", "seq": 2},
        })
        mig.migrate_interfaces_to_multi_account()
        for app_id, seq in (("app1", "s1"), ("app2", "s2")):
            path = data_dir / "tools" / "qq" / app_id / "ws_sessions.json"
            assert _read_json(path) == {app_id: {"session_id": seq, "seq": 1 if app_id == "app1" else 2}}
        assert not (data_dir / "tools" / "qq" / "ws_sessions.json").exists()
