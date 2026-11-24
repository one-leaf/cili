"""Web 建会话与终端客户端的通道选择测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import core.config as config_mod
from core.channels import session_marker
from core.session import read_meta


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """把工作区数据目录与工作区解析都指向临时目录，返回工作区 uuid。"""
    import interfaces.common.commands as common_commands

    uuid = "ws-test"

    def _entry(u):
        return {"uuid": u, "workspace_name": u, "directory": str(tmp_path)}             if u == uuid else None

    monkeypatch.setattr(config_mod, "get_workspace_data_dir", lambda u: Path(tmp_path) / u)
    monkeypatch.setattr(config_mod, "find_workspace_entry", _entry)
    monkeypatch.setattr(config_mod, "ensure_default_workspace", lambda: uuid)
    monkeypatch.setattr(common_commands, "get_workspace_sessions_dir", lambda u: Path(tmp_path) / u / "sessions")
    monkeypatch.setattr(common_commands, "find_workspace_entry", _entry)
    return uuid


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
