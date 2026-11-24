"""通道会话解析：标记三元组、latest/dedicated 模式、锁定与 pin。

对应方案：workspace/default/session-channel-routing-plan-2026-11-16.md
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.config as config_mod
from core.channels import marker_metadata, session_marker
from core.session import SessionStore, read_meta

# ─── 工具 ─────────────────────────────────────────────────────────────


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """把工作区数据目录与工作区解析都指向临时目录，返回工作区 uuid。"""
    import interfaces.common.commands as common_commands

    uuid = "ws-test"

    def _entry(u):
        return {"uuid": u, "workspace_name": u, "directory": str(tmp_path)} \
            if u == uuid else None

    # 两处都要打：core.config（state 层惰性 import）与 commands（模块级 import 已绑定）
    monkeypatch.setattr(config_mod, "get_workspace_data_dir", lambda u: Path(tmp_path) / u)
    monkeypatch.setattr(config_mod, "find_workspace_entry", _entry)
    monkeypatch.setattr(config_mod, "ensure_default_workspace", lambda: uuid)
    monkeypatch.setattr(common_commands, "get_workspace_sessions_dir", lambda u: Path(tmp_path) / u / "sessions")
    monkeypatch.setattr(common_commands, "find_workspace_entry", _entry)
    return uuid


def _cfg(**kw) -> SimpleNamespace:
    base = {
        "workspace_locked": False,
        "default_workspace_uuid": "",
        "session_mode": "dedicated",
        "session_locked": False,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _sessions_dir(tmp_path, uuid="ws-test") -> Path:
    return Path(tmp_path) / uuid / "sessions"


def _make_session(tmp_path, name="会话", metadata=None, uuid="ws-test") -> str:
    d = _sessions_dir(tmp_path, uuid)
    d.mkdir(parents=True, exist_ok=True)
    store = SessionStore.create_new_session(d, name, metadata=metadata)
    return store.session_id


def _meta(tmp_path, sid, uuid="ws-test") -> dict:
    return read_meta(_sessions_dir(tmp_path, uuid) / sid)


def _resolve(account, chat_id, cfg, *, channel="qq", account_id="acc1", title="QQ 会话"):
    from interfaces.common.session_routing import resolve_turn_session

    return resolve_turn_session(
        account, chat_id, cfg,
        channel=channel, account_id=account_id, title=title,
    )


# ─── 解析 ─────────────────────────────────────────────────────────────


class TestResolve:
    def test_dedicated_creates_marked_session_then_reuses(self, ws, tmp_path):
        from interfaces.qq.state import QQAccountState

        state = QQAccountState()
        chat = _resolve(state, "u1", _cfg(), account_id="acc1")
        sid = chat.session_id
        assert sid and not chat.pinned

        meta = _meta(tmp_path, sid)
        assert meta["name"] == "QQ 会话"
        assert session_marker(meta["metadata"]) == ("qq", "acc1", "u1")

        # 第二次解析找回同一条，不新建
        chat = _resolve(state, "u1", _cfg(), account_id="acc1")
        assert chat.session_id == sid
        assert len(list(_sessions_dir(tmp_path).iterdir())) == 1

    def test_accounts_and_chats_are_isolated(self, ws, tmp_path):
        from interfaces.qq.state import QQAccountState

        state = QQAccountState()
        a1 = _resolve(state, "u1", _cfg(), account_id="acc1").session_id
        a2 = _resolve(state, "u1", _cfg(), account_id="acc2").session_id
        b1 = _resolve(state, "u2", _cfg(), account_id="acc1").session_id

        assert len({a1, a2, b1}) == 3
        assert session_marker(_meta(tmp_path, a2)["metadata"]) == ("qq", "acc2", "u1")
        assert session_marker(_meta(tmp_path, b1)["metadata"]) == ("qq", "acc1", "u2")

    def test_latest_uses_newest_visible_session(self, ws, tmp_path):
        from interfaces.qq.state import QQAccountState

        web_sid = _make_session(
            tmp_path, "网页会话", marker_metadata("web"))
        hidden_sid = _make_session(
            tmp_path, "隐藏", {**marker_metadata("web"), "hidden": True})
        # 让 hidden 的 updated_at 最新（list_sessions 已按它排序）
        from core.session import SessionStore as SS
        store = SS.load_session(hidden_sid, _sessions_dir(tmp_path))
        store.rename("隐藏最新")
        store.set_hidden(True)

        state = QQAccountState()
        chat = _resolve(state, "u1", _cfg(session_mode="latest"))
        assert chat.session_id != hidden_sid
        assert chat.session_id == web_sid

    def test_latest_skips_cron_sessions(self, ws, tmp_path):
        from interfaces.qq.state import QQAccountState

        web_sid = _make_session(tmp_path, "网页会话", marker_metadata("web"))
        # cron 会话更新（updated_at 最新），但不该成为 latest 落点
        cron_sid = _make_session(tmp_path, "[Cron] 日报", marker_metadata("cron"))
        assert cron_sid != web_sid

        state = QQAccountState()
        chat = _resolve(state, "u1", _cfg(session_mode="latest"))
        assert chat.session_id == web_sid

    def test_dedicated_ignores_other_channel_sessions(self, ws, tmp_path):
        from interfaces.qq.state import QQAccountState

        _make_session(tmp_path, "网页会话", marker_metadata("web"))
        state = QQAccountState()
        chat = _resolve(state, "u1", _cfg())
        assert session_marker(_meta(tmp_path, chat.session_id)["metadata"]) == (
            "qq", "acc1", "u1")

    def test_locked_ignores_latest_and_stays_dedicated(self, ws, tmp_path):
        from interfaces.qq.state import QQAccountState

        _make_session(tmp_path, "网页会话", marker_metadata("web"))
        state = QQAccountState()
        chat = _resolve(state, "u1", _cfg(session_mode="latest", session_locked=True))
        assert session_marker(_meta(tmp_path, chat.session_id)["metadata"]) == (
            "qq", "acc1", "u1")

    def test_pin_wins(self, ws, tmp_path):
        from interfaces.qq.state import QQAccountState

        pinned_sid = _make_session(tmp_path, "网页会话", marker_metadata("web"))
        state = QQAccountState()
        chat = state.chat("u1")
        chat.workspace_uuid = ws
        chat.session_id = pinned_sid
        chat.pinned = True

        resolved = _resolve(state, "u1", _cfg())
        assert resolved.session_id == pinned_sid

    def test_deleted_session_creates_new_instead_of_fallback(self, ws, tmp_path):
        from interfaces.qq.state import QQAccountState

        state = QQAccountState()
        old = _resolve(state, "u1", _cfg()).session_id
        import shutil
        shutil.rmtree(_sessions_dir(tmp_path) / old)

        # Web 最新会话存在，也不能被独立模式借用
        web_sid = _make_session(tmp_path, "网页会话", marker_metadata("web"))
        chat = _resolve(state, "u1", _cfg())
        assert chat.session_id not in (old, web_sid)

    def test_legacy_binding_is_adopted(self, ws, tmp_path):
        from interfaces.qq.state import QQAccountState

        legacy = _make_session(tmp_path, "QQ 会话")   # 无标记，旧命名
        state = QQAccountState()
        chat = state.chat("u1")
        chat.workspace_uuid = ws
        chat.session_id = legacy

        resolved = _resolve(state, "u1", _cfg())
        assert resolved.session_id == legacy
        assert resolved.pinned is True
        assert session_marker(_meta(tmp_path, legacy)["metadata"]) == (
            "qq", "acc1", "u1")

    def test_legacy_binding_with_other_title_not_adopted(self, ws, tmp_path):
        from interfaces.qq.state import QQAccountState

        web_sid = _make_session(tmp_path, "网页会话")   # 无标记但标题不符
        state = QQAccountState()
        chat = state.chat("u1")
        chat.workspace_uuid = ws
        chat.session_id = web_sid

        resolved = _resolve(state, "u1", _cfg())
        assert resolved.session_id != web_sid
        assert session_marker(_meta(tmp_path, web_sid)["metadata"]) == ("", "", "")

    def test_peek_does_not_create(self, ws, tmp_path):
        from interfaces.common.session_routing import peek_turn_session
        from interfaces.qq.state import QQAccountState

        state = QQAccountState()
        chat = state.chat("u1")
        chat.workspace_uuid = ws
        assert peek_turn_session(chat, _cfg(), channel="qq",
                                 account_id="acc1", chat_id="u1") == ""
        assert not _sessions_dir(tmp_path).exists() or not list(
            _sessions_dir(tmp_path).iterdir())


# ─── 指令 ─────────────────────────────────────────────────────────────


class _FakeRuntime:
    def __init__(self, cfg=None):
        from interfaces.qq.config import QQConfig

        self.cfg = cfg or QQConfig()
        self.account_id = self.cfg.id or "acc1"
        self.state = None
        self.limiter = None
        self.api = None
        self.loop = None


def _qq_state():
    from interfaces.qq.state import QQAccountState

    return QQAccountState()


def _dispatch(rt, state, chat_id, text):
    from interfaces.qq.commands import dispatch

    return asyncio.run(dispatch(rt, state, chat_id, text))


class TestCommands:
    def test_mode_show_and_switch(self, ws, tmp_path, monkeypatch):
        import interfaces.qq.state as qq_state

        monkeypatch.setattr(qq_state, "DATA_DIR", tmp_path)
        rt = _FakeRuntime()
        state = _qq_state()
        state.chat("u1").workspace_uuid = ws

        out = _dispatch(rt, state, "u1", "/mode")
        assert "独立会话" in out

        out = _dispatch(rt, state, "u1", "/mode latest")
        assert "接着最新会话" in out
        assert state.chats["u1"].session_mode == "latest"

        assert "用法" in _dispatch(rt, state, "u1", "/mode bogus")

    def test_locked_rejects_session_commands(self, ws, tmp_path, monkeypatch):
        import interfaces.qq.state as qq_state

        monkeypatch.setattr(qq_state, "DATA_DIR", tmp_path)
        rt = _FakeRuntime()
        rt.cfg.session_locked = True
        state = _qq_state()
        state.chat("u1").workspace_uuid = ws

        out = _dispatch(rt, state, "u1", "/mode latest")
        assert "已锁定会话" in out
        assert state.chats["u1"].session_mode == ""

        assert "已锁定会话" in _dispatch(rt, state, "u1", "/new")
        assert not _sessions_dir(tmp_path).exists() or not list(
            _sessions_dir(tmp_path).iterdir())

        assert "已锁定会话" in _dispatch(rt, state, "u1", "/s 1")

    def test_s_pins_and_new_marks(self, ws, tmp_path, monkeypatch):
        import interfaces.qq.state as qq_state

        monkeypatch.setattr(qq_state, "DATA_DIR", tmp_path)
        rt = _FakeRuntime()
        state = _qq_state()
        state.chat("u1").workspace_uuid = ws
        first = _make_session(tmp_path, "旧会话", marker_metadata("qq", "acc1", "u1"))

        out = _dispatch(rt, state, "u1", "/s 1")
        assert "已切换会话" in out
        assert state.chats["u1"].pinned is True
        assert state.chats["u1"].session_id == first

        out = _dispatch(rt, state, "u1", "/new 新会话")
        assert "已新建" in out
        sid = state.chats["u1"].session_id
        assert sid != first
        assert session_marker(_meta(tmp_path, sid)["metadata"]) == ("qq", "acc1", "u1")


# ─── Web 创建 / TUI 会话选择 ─────────────────────────────────────────


class TestWebCreate:
    def test_create_marks_channel(self, tmp_path):
        from web.routes_workspace import CreateSessionRequest, create_session

        resp = asyncio.run(create_session(
            "ws", CreateSessionRequest(name="终端会话", channel="cli"),
            ws_dir=tmp_path,
        ))
        meta = read_meta(tmp_path / "sessions" / resp["session_id"])
        assert session_marker(meta["metadata"]) == ("cli", "", "")

    def test_rejects_remote_channel(self, tmp_path):
        from fastapi import HTTPException

        from web.routes_workspace import CreateSessionRequest, create_session

        with pytest.raises(HTTPException):
            asyncio.run(create_session(
                "ws", CreateSessionRequest(name="x", channel="qq"),
                ws_dir=tmp_path,
            ))


class _FakeTuiClient:
    def __init__(self, sessions):
        self._sessions = list(sessions)
        self.created: list[str | None] = []

    def list_sessions(self, workspace_uuid):
        return self._sessions

    def create_session(self, workspace_uuid, name=None, *, channel=None):
        self.created.append(channel)
        return {"session_id": "new1", "name": name or "New Session"}


class _FakeRenderer:
    def status_line(self, *a, **k):
        pass

    def error_line(self, *a, **k):
        pass


class TestTuiSelection:
    def _app(self, sessions, mode):
        from interfaces.cli.app import TuiApp

        client = _FakeTuiClient(sessions)
        app = TuiApp(client, _FakeRenderer(), mode=mode)
        app.workspace_uuid = "ws"
        return app, client

    def test_dedicated_adopts_cli_session(self):
        app, _ = self._app([
            {"session_id": "w1", "channel": "web", "name": "网页"},
            {"session_id": "c1", "channel": "cli", "name": "终端"},
        ], "dedicated")
        assert app._setup_session() is True
        assert app.session_id == "c1"

    def test_dedicated_creates_when_none(self):
        app, client = self._app([{"session_id": "w1", "channel": "web"}], "dedicated")
        assert app._setup_session() is True
        assert client.created == ["cli"]
        assert app.session_id == "new1"

    def test_latest_and_new(self):
        app, _ = self._app([
            {"session_id": "w1", "channel": "web", "name": "网页"},
            {"session_id": "c1", "channel": "cli", "name": "终端"},
        ], "latest")
        assert app._setup_session() is True
        assert app.session_id == "w1"

        app, client = self._app([], "new")
        assert app._setup_session() is True
        assert client.created == ["cli"]
