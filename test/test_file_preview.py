"""Tests for /api/file-preview — 只读文本预览端点。"""

import pytest
from fastapi import Response

import core.config as config_mod


class _Base:
    WS_UUID = "wspreview"

    def _prepare(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        config_mod.upsert_workspace_entry({
            "uuid": self.WS_UUID,
            "workspace_name": "Preview",
            "directory": str(ws_dir),
        })
        return ws_dir

    def _call(self, path, ws_uuid=None):
        from web.routes_files import preview_file
        return preview_file(
            workspace_uuid=ws_uuid or self.WS_UUID,
            response=Response(),
            path=path,
        )


@pytest.fixture(autouse=True)
def _isolate_teen(teen):
    """所有用例隔离青少年模式单例（默认关闭），避免读取本机真实状态。"""
    yield


class TestPreviewEndpoint(_Base):
    def test_utf8_text(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "a.md").write_bytes("# 标题\n正文".encode("utf-8"))

        data = self._call("a.md")

        assert data["kind"] == "text"
        assert data["encoding"] == "utf-8"
        assert data["content"] == "# 标题\n正文"
        assert data["path"] == "a.md"
        assert data["truncated"] is False

    def test_gb18030_text(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "gbk.txt").write_bytes("中文内容".encode("gbk"))

        data = self._call("gbk.txt")

        assert data["kind"] == "text"
        assert data["encoding"] == "gb18030"
        assert data["content"] == "中文内容"

    def test_binary_detected(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "bin.dat").write_bytes(b"abc\x00def")

        data = self._call("bin.dat")

        assert data["kind"] == "binary"
        assert data["content"] is None
        assert data["encoding"] is None

    def test_truncated_large_text(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "big.txt").write_text("a" * (600 * 1024), encoding="utf-8")

        data = self._call("big.txt")

        assert data["kind"] == "text"
        assert data["truncated"] is True
        assert len(data["content"]) == 512 * 1024

    def test_truncated_multibyte_tail_keeps_utf8(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        # 600KB 中文：512KB 截断点必然落在某个字符中间
        (ws_dir / "big-cn.txt").write_bytes("中".encode("utf-8") * (200 * 1024))

        data = self._call("big-cn.txt")

        assert data["kind"] == "text"
        assert data["encoding"] == "utf-8"
        assert data["truncated"] is True
        assert "中" in data["content"]

    def test_path_traversal_rejected(self, tmp_path, monkeypatch):
        self._prepare(tmp_path, monkeypatch)
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            self._call("../evil.md")
        assert exc.value.status_code == 403

    def test_absolute_path_rejected(self, tmp_path, monkeypatch):
        self._prepare(tmp_path, monkeypatch)
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            self._call(str(tmp_path / "evil.md"))
        assert exc.value.status_code == 403

    def test_directory_rejected(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "sub").mkdir()
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            self._call("sub")
        assert exc.value.status_code == 400

    def test_missing_file(self, tmp_path, monkeypatch):
        self._prepare(tmp_path, monkeypatch)
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            self._call("nope.txt")
        assert exc.value.status_code == 404

    def test_empty_path(self, tmp_path, monkeypatch):
        self._prepare(tmp_path, monkeypatch)
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            self._call("")
        assert exc.value.status_code == 400

    def test_unknown_workspace(self, tmp_path, monkeypatch):
        self._prepare(tmp_path, monkeypatch)
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            self._call("a.md", ws_uuid="nosuchws")
        assert exc.value.status_code == 404

    def test_locked_workspace_denied(self, tmp_path, monkeypatch, teen):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "a.md").write_text("x", encoding="utf-8")
        teen.enable("123456", policy={"allowed_workspaces": []})

        from core.teen_mode import TeenModeDenied

        with pytest.raises(TeenModeDenied):
            self._call("a.md")


class TestListFilesPosixPaths(_Base):
    def test_relative_paths_use_forward_slash(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "docs").mkdir()
        (ws_dir / "docs" / "a.md").write_text("x", encoding="utf-8")

        from web.routes_files import list_files

        root = list_files(workspace_uuid=self.WS_UUID, path="")
        assert all("\\" not in item["path"] for item in root["items"])
        docs = list_files(workspace_uuid=self.WS_UUID, path="docs")
        assert docs["items"][0]["path"] == "docs/a.md"
        assert docs["parent"] == ""
