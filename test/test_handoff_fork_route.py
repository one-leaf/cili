"""fork 会话端点测试（web.routes_chat /fork）。"""

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


def _endpoint_module():
    from web import routes_chat

    return routes_chat


def _make_session_dir(tmp_path):
    ws_dir = tmp_path / ".cili"
    session_dir = ws_dir / "sessions" / "sid"
    session_dir.mkdir(parents=True)
    (session_dir / "index.json").write_text("{}", encoding="utf-8")
    return ws_dir


class TestForkSessionEndpoint:
    def test_success(self, tmp_path, monkeypatch):
        routes = _endpoint_module()
        ws_dir = _make_session_dir(tmp_path)
        runner = MagicMock()
        runner.fork_session.return_value = {
            "status": "running", "session_id": "new", "name": "分支",
        }

        async def fake_get(_workspace_uuid, _session_id):
            return runner

        monkeypatch.setattr(routes, "_get_or_create_runner", fake_get)
        monkeypatch.setattr(routes, "_claim_session_run", lambda _key: True)
        monkeypatch.setattr(routes, "_release_session_run", lambda _key: None)

        req = routes.ForkSessionRequest(next_steps="继续")
        result = asyncio.run(
            routes.fork_session("ws", "sid", req, ws_dir)
        )

        assert result["session_id"] == "new"
        assert result["name"] == "分支"
        runner.set_turn_interface.assert_any_call("web")
        runner.set_turn_interface.assert_any_call(None)
        runner.fork_session.assert_called_once()
        call = runner.fork_session.call_args
        assert call.kwargs["next_steps"] == "继续"
        assert result.get("mode") == "snapshot"

    def test_missing_session(self, tmp_path):
        routes = _endpoint_module()
        req = routes.ForkSessionRequest()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                routes.fork_session("ws", "sid", req, tmp_path / ".cili")
            )
        assert exc.value.status_code == 404

    def test_running_session(self, tmp_path, monkeypatch):
        routes = _endpoint_module()
        ws_dir = _make_session_dir(tmp_path)
        runner = MagicMock()

        async def fake_get(_workspace_uuid, _session_id):
            return runner

        monkeypatch.setattr(routes, "_get_or_create_runner", fake_get)
        monkeypatch.setattr(routes, "_claim_session_run", lambda _key: False)

        req = routes.ForkSessionRequest()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                routes.fork_session("ws", "sid", req, ws_dir)
            )
        assert exc.value.status_code == 409

    def test_handoff_error(self, tmp_path, monkeypatch):
        routes = _endpoint_module()
        ws_dir = _make_session_dir(tmp_path)
        runner = MagicMock()
        runner.fork_session.return_value = {
            "status": "error", "message": "boom",
        }

        async def fake_get(_workspace_uuid, _session_id):
            return runner

        monkeypatch.setattr(routes, "_get_or_create_runner", fake_get)
        monkeypatch.setattr(routes, "_claim_session_run", lambda _key: True)
        monkeypatch.setattr(routes, "_release_session_run", lambda _key: None)

        req = routes.ForkSessionRequest()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                routes.fork_session("ws", "sid", req, ws_dir)
            )
        assert exc.value.status_code == 500
        assert "boom" in exc.value.detail
