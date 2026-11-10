"""Tests for 可编辑预览后端：PUT /api/files 乐观锁与门控、file-preview 的 mtime_ns。"""

import os

import pytest
from fastapi import Response

import core.config as config_mod


def _request():
    """_csrf_protect 只读 headers；空头 → 无 Origin/Referer → 放行。"""
    from starlette.requests import Request as StarletteRequest

    return StarletteRequest({
        "type": "http", "method": "PUT", "path": "/api/files",
        "headers": [], "query_string": b"", "client": ("127.0.0.1", 1),
    })


class _Base:
    WS_UUID = "wsedit"

    def _prepare(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        config_mod.upsert_workspace_entry({
            "uuid": self.WS_UUID,
            "workspace_name": "Edit",
            "directory": str(ws_dir),
        })
        return ws_dir

    def _save(self, path, content, *, expected_mtime_ns=None, expected_size=None, ws_uuid=None):
        from web.routes_files import FileUpdateRequest, update_file

        req = FileUpdateRequest(
            workspace_uuid=ws_uuid or self.WS_UUID,
            path=path,
            content=content,
            expected_mtime_ns=expected_mtime_ns,
            expected_size=expected_size,
        )
        return update_file(request=req, request_raw=_request())

    def _preview(self, path, ws_uuid=None):
        from web.routes_files import preview_file

        return preview_file(
            workspace_uuid=ws_uuid or self.WS_UUID,
            response=Response(),
            path=path,
        )


@pytest.fixture(autouse=True)
def _isolate_teen(teen):
    """隔离青少年模式单例（默认关闭），避免读取本机真实状态。"""
    yield


class TestPreviewMtimeNs(_Base):
    def test_preview_has_mtime_ns_as_string(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        f = ws_dir / "a.txt"
        f.write_text("hello", encoding="utf-8")

        data = self._preview("a.txt")

        # 纳秒时间戳必须以字符串传输：JS Number 无法精确表示 1.7e18 级整数
        assert isinstance(data["mtime_ns"], str)
        assert data["mtime_ns"] == str(f.stat().st_mtime_ns)


class TestSaveContent(_Base):
    def test_save_ok(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        f = ws_dir / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        st = f.stat()

        res = self._save(
            "a.py", "x = 2\n",
            expected_mtime_ns=str(st.st_mtime_ns), expected_size=st.st_size,
        )

        assert res["action"] == "save"
        assert isinstance(res["mtime_ns"], str)
        assert f.read_text(encoding="utf-8") == "x = 2\n"

    def test_save_without_expected(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        f = ws_dir / "a.txt"
        f.write_text("old", encoding="utf-8")

        res = self._save("a.txt", "new")

        assert res["action"] == "save"
        assert f.read_text(encoding="utf-8") == "new"

    def test_stale_mtime_conflict(self, tmp_path, monkeypatch):
        from fastapi import HTTPException

        ws_dir = self._prepare(tmp_path, monkeypatch)
        f = ws_dir / "a.txt"
        f.write_text("v1", encoding="utf-8")
        st = f.stat()
        # 用 utime 强制改变 mtime，避免同粒度写入导致误判
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

        with pytest.raises(HTTPException) as exc:
            self._save(
                "a.txt", "mine",
                expected_mtime_ns=str(st.st_mtime_ns), expected_size=st.st_size,
            )

        assert exc.value.status_code == 409
        detail = exc.value.detail
        assert "current_mtime_ns" in detail and "current_size" in detail
        assert isinstance(detail["current_mtime_ns"], str)

    def test_size_conflict(self, tmp_path, monkeypatch):
        from fastapi import HTTPException

        ws_dir = self._prepare(tmp_path, monkeypatch)
        f = ws_dir / "a.txt"
        f.write_text("v1", encoding="utf-8")
        st = f.stat()
        f.write_text("v1-longer", encoding="utf-8")  # size 改变

        with pytest.raises(HTTPException) as exc:
            self._save("a.txt", "mine", expected_size=st.st_size)

        assert exc.value.status_code == 409

    def test_invalid_expected_mtime_ns(self, tmp_path, monkeypatch):
        from fastapi import HTTPException

        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "a.txt").write_text("v1", encoding="utf-8")

        with pytest.raises(HTTPException) as exc:
            self._save("a.txt", "mine", expected_mtime_ns="not-an-int")

        assert exc.value.status_code == 422

    def test_unknown_workspace(self, tmp_path, monkeypatch):
        from fastapi import HTTPException

        self._prepare(tmp_path, monkeypatch)

        with pytest.raises(HTTPException) as exc:
            self._save("a.txt", "x", ws_uuid="nosuchws")

        assert exc.value.status_code == 404


class TestSaveGates(_Base):
    def test_too_large_rejected(self, tmp_path, monkeypatch):
        from fastapi import HTTPException

        ws_dir = self._prepare(tmp_path, monkeypatch)
        f = ws_dir / "big.txt"
        f.write_text("a" * (513 * 1024), encoding="utf-8")

        with pytest.raises(HTTPException) as exc:
            self._save("big.txt", "small")

        assert exc.value.status_code == 422

    def test_binary_rejected(self, tmp_path, monkeypatch):
        from fastapi import HTTPException

        ws_dir = self._prepare(tmp_path, monkeypatch)
        f = ws_dir / "bin.dat"
        f.write_bytes(b"abc\x00def")

        with pytest.raises(HTTPException) as exc:
            self._save("bin.dat", "text")

        assert exc.value.status_code == 422

    def test_gbk_rejected(self, tmp_path, monkeypatch):
        from fastapi import HTTPException

        ws_dir = self._prepare(tmp_path, monkeypatch)
        f = ws_dir / "gbk.txt"
        f.write_bytes("中文内容".encode("gbk"))

        with pytest.raises(HTTPException) as exc:
            self._save("gbk.txt", "修改")

        assert exc.value.status_code == 422

    def test_traversal_rejected(self, tmp_path, monkeypatch):
        from fastapi import HTTPException

        self._prepare(tmp_path, monkeypatch)

        with pytest.raises(HTTPException) as exc:
            self._save("../evil.txt", "x")

        assert exc.value.status_code == 403

    def test_directory_rejected(self, tmp_path, monkeypatch):
        from fastapi import HTTPException

        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "sub").mkdir()

        with pytest.raises(HTTPException) as exc:
            self._save("sub", "x")

        assert exc.value.status_code == 400

    def test_teen_locked_rejected(self, tmp_path, monkeypatch, teen):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "a.txt").write_text("x", encoding="utf-8")
        teen.enable("123456", policy={"allowed_workspaces": []})

        from core.teen_mode import TeenModeDenied

        with pytest.raises(TeenModeDenied):
            self._save("a.txt", "y")
