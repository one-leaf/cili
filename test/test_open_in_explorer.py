"""Tests for /api/open-in-explorer — 本机系统资源管理器打开。"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import core.config as config_mod

WS = "wsexplorer"


@pytest.fixture(autouse=True)
def _isolate_teen(teen):
    """所有用例隔离青少年模式单例（默认关闭）。"""
    yield


def _prepare(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    config_mod.upsert_workspace_entry({
        "uuid": WS,
        "workspace_name": "Explorer",
        "directory": str(ws_dir),
    })
    return ws_dir


def _request(client=("127.0.0.1", 1), origin=""):
    from starlette.requests import Request as StarletteRequest
    headers = []
    if origin:
        headers.append((b"origin", origin.encode("latin-1")))
    scope = {
        "type": "http", "method": "POST", "path": "/api/open-in-explorer",
        "headers": headers, "query_string": b"", "client": client,
    }
    return StarletteRequest(scope)


def _call(path, client=("127.0.0.1", 1), origin=""):
    from web.routes_files import OpenInExplorerRequest, open_in_explorer
    return open_in_explorer(
        OpenInExplorerRequest(workspace_uuid=WS, path=path),
        _request(client, origin),
    )


@pytest.fixture
def popen_calls(monkeypatch):
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(pid=1234)

    monkeypatch.setattr("web.routes_files.subprocess.Popen", fake_popen)
    return calls


class TestOpenInExplorer:
    def test_directory_opens_explorer(self, tmp_path, monkeypatch, popen_calls):
        ws_dir = _prepare(tmp_path, monkeypatch)
        (ws_dir / "sub").mkdir()

        result = _call("sub")

        assert result["ok"] is True
        assert result["path"] == "sub"
        assert len(popen_calls) == 1
        args = popen_calls[0][0]
        assert args[0] == "explorer.exe"
        assert args[1] == str((ws_dir / "sub").resolve())

    def test_file_uses_select(self, tmp_path, monkeypatch, popen_calls):
        ws_dir = _prepare(tmp_path, monkeypatch)
        (ws_dir / "a.md").write_text("x", encoding="utf-8")

        result = _call("a.md")

        assert result["ok"] is True
        args = popen_calls[0][0]
        assert args[1] == f"/select,{(ws_dir / 'a.md').resolve()}"

    def test_empty_path_opens_workspace_root(self, tmp_path, monkeypatch, popen_calls):
        ws_dir = _prepare(tmp_path, monkeypatch)

        result = _call("")

        assert result["ok"] is True
        assert popen_calls[0][0][1] == str(ws_dir.resolve())

    def test_non_localhost_rejected(self, tmp_path, monkeypatch, popen_calls):
        _prepare(tmp_path, monkeypatch)

        with pytest.raises(HTTPException) as exc:
            _call("", client=("10.0.0.5", 1))
        assert exc.value.status_code == 403
        assert popen_calls == []

    def test_cross_site_origin_rejected(self, tmp_path, monkeypatch, popen_calls):
        _prepare(tmp_path, monkeypatch)

        with pytest.raises(HTTPException) as exc:
            _call("", origin="http://evil.example")
        assert exc.value.status_code == 403
        assert popen_calls == []

    def test_path_traversal_rejected(self, tmp_path, monkeypatch, popen_calls):
        _prepare(tmp_path, monkeypatch)
        (tmp_path / "evil.txt").write_text("x", encoding="utf-8")

        with pytest.raises(HTTPException) as exc:
            _call("../evil.txt")
        assert exc.value.status_code == 403
        assert popen_calls == []

    def test_missing_path(self, tmp_path, monkeypatch, popen_calls):
        _prepare(tmp_path, monkeypatch)

        with pytest.raises(HTTPException) as exc:
            _call("nope")
        assert exc.value.status_code == 404
        assert popen_calls == []

    def test_unknown_workspace(self, tmp_path, monkeypatch, popen_calls):
        _prepare(tmp_path, monkeypatch)

        from web.routes_files import OpenInExplorerRequest, open_in_explorer
        with pytest.raises(HTTPException) as exc:
            open_in_explorer(OpenInExplorerRequest(workspace_uuid="nosuchws", path=""), _request())
        assert exc.value.status_code == 404

    def test_locked_workspace_denied(self, tmp_path, monkeypatch, popen_calls, teen):
        _prepare(tmp_path, monkeypatch)
        teen.enable("123456", policy={"allowed_workspaces": []})

        from core.teen_mode import TeenModeDenied
        with pytest.raises(TeenModeDenied):
            _call("")
        assert popen_calls == []

    def test_teen_mode_blocks_explorer(self, tmp_path, monkeypatch, popen_calls, teen):
        _prepare(tmp_path, monkeypatch)
        teen.enable("123456", policy={"allowed_workspaces": [WS]})

        with pytest.raises(HTTPException) as exc:
            _call("")
        assert exc.value.status_code == 403
        assert "青少年模式" in exc.value.detail
        assert popen_calls == []

    def test_non_windows_unsupported(self, tmp_path, monkeypatch, popen_calls):
        import sys
        _prepare(tmp_path, monkeypatch)
        monkeypatch.setattr(sys, "platform", "linux")

        with pytest.raises(HTTPException) as exc:
            _call("")
        assert exc.value.status_code == 501
        assert popen_calls == []
