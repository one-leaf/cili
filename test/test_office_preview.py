"""Tests for /api/office-preview — Office 文档转 PDF 预览端点。"""

from pathlib import Path

import pytest
from fastapi import HTTPException

import core.config as config_mod

WS = "wsoffice"


@pytest.fixture(autouse=True)
def _isolate_teen(teen):
    yield


class _Base:
    def _prepare(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        config_mod.upsert_workspace_entry({
            "uuid": WS,
            "workspace_name": "Office",
            "directory": str(ws_dir),
        })
        return ws_dir

    def _fake_ensure(self, pdf_path, engine="Test Office", cached=False):
        def fake(source, timeout=120):
            Path(pdf_path).write_bytes(b"%PDF-1.7\nfake")
            return Path(pdf_path), engine, cached
        return fake

    def _status(self, path):
        from web.routes_files import office_preview_status
        return office_preview_status(workspace_uuid=WS, path=path)

    def _pdf(self, path):
        from web.routes_files import office_preview_pdf
        return office_preview_pdf(workspace_uuid=WS, path=path)


class TestOfficePreview(_Base):
    def test_status_ready(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "a.docx").write_bytes(b"fake")
        pdf = tmp_path / "cached.pdf"
        monkeypatch.setattr("web.routes_files.ensure_pdf", self._fake_ensure(pdf))

        data = self._status("a.docx")

        assert data["status"] == "ready"
        assert data["engine"] == "Test Office"
        assert data["cached"] is False
        assert data["pdf_url"].startswith("/api/office-preview/pdf?")
        assert "workspace_uuid=wsoffice" in data["pdf_url"]
        assert "path=a.docx" in data["pdf_url"]

    def test_unsupported_extension(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "a.txt").write_text("x", encoding="utf-8")

        with pytest.raises(HTTPException) as exc:
            self._status("a.txt")
        assert exc.value.status_code == 400
        assert "Unsupported Office" in exc.value.detail

    def test_path_traversal(self, tmp_path, monkeypatch):
        self._prepare(tmp_path, monkeypatch)

        with pytest.raises(HTTPException) as exc:
            self._status("../evil.docx")
        assert exc.value.status_code == 403

    def test_missing_file(self, tmp_path, monkeypatch):
        self._prepare(tmp_path, monkeypatch)

        with pytest.raises(HTTPException) as exc:
            self._status("missing.docx")
        assert exc.value.status_code == 404

    def test_timeout_maps_504(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "a.docx").write_bytes(b"fake")

        def timeout(source, timeout=120):
            raise TimeoutError("conversion timed out")

        monkeypatch.setattr("web.routes_files.ensure_pdf", timeout)
        with pytest.raises(HTTPException) as exc:
            self._status("a.docx")
        assert exc.value.status_code == 504

    def test_conversion_failure_maps_500(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "a.docx").write_bytes(b"fake")

        def fail(source, timeout=120):
            raise RuntimeError("Office not available")

        monkeypatch.setattr("web.routes_files.ensure_pdf", fail)
        with pytest.raises(HTTPException) as exc:
            self._status("a.docx")
        assert exc.value.status_code == 500

    def test_pdf_route_serves_file(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "a.docx").write_bytes(b"fake")
        pdf = tmp_path / "cached.pdf"
        monkeypatch.setattr("web.routes_files.ensure_pdf", self._fake_ensure(pdf))

        response = self._pdf("a.docx")

        assert response.media_type == "application/pdf"
        assert Path(response.path).exists()

    def test_locked_workspace_denied(self, tmp_path, monkeypatch, teen):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "a.docx").write_bytes(b"fake")
        teen.enable("123456", policy={"allowed_workspaces": []})

        from core.teen_mode import TeenModeDenied
        with pytest.raises(TeenModeDenied):
            self._status("a.docx")
