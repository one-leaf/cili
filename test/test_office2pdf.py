"""office2pdf 工具测试（COM 转换被 mock，不依赖本机 Office）。"""

import os
from pathlib import Path

import pytest

from core.tools import office2pdf as mod
from core.tools.office2pdf import Office2PDFTool


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    monkeypatch.setattr(mod, "CACHE_DIR", d)
    return d


@pytest.fixture
def fake_worker(monkeypatch):
    calls = []

    def worker(source, output, kind, pid_file, timeout):
        calls.append((source, kind))
        Path(output).write_bytes(b"%PDF-1.7\nfake pdf")
        return "Test Office"

    monkeypatch.setattr(mod, "_run_worker", worker)
    return calls


class TestOffice2PDFToolSchema:
    def test_name_and_schema(self):
        tool = Office2PDFTool(cwd=".")
        assert tool.name == "office2pdf"
        assert "PDF" in tool.description
        assert "Office" in tool.description
        assert "file_path" in tool.parameters["properties"]
        assert "output_path" in tool.parameters["properties"]
        assert tool.parameters["required"] == ["file_path"]
        assert tool.concurrency_safe is False

    def test_supported_extensions(self):
        assert ".docx" in mod.SUPPORTED_EXTENSIONS
        assert ".xlsx" in mod.SUPPORTED_EXTENSIONS
        assert ".pptx" in mod.SUPPORTED_EXTENSIONS
        assert ".txt" not in mod.SUPPORTED_EXTENSIONS

    def test_registered(self):
        from core.tools.registry import TOOL_REGISTRY
        assert "office2pdf" in TOOL_REGISTRY


class TestOffice2PDFValidation:
    def test_missing_file(self, tmp_path):
        tool = Office2PDFTool(cwd=str(tmp_path))
        result = tool.execute(file_path="nope.docx")
        assert result.is_error
        assert "file not found" in result.output

    def test_unsupported_extension(self, tmp_path):
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        tool = Office2PDFTool(cwd=str(tmp_path))
        result = tool.execute(file_path="a.txt")
        assert result.is_error
        assert "unsupported file type" in result.output

    def test_existing_output_without_overwrite(self, tmp_path):
        (tmp_path / "a.docx").write_bytes(b"fake")
        (tmp_path / "a.pdf").write_bytes(b"%PDF-1.7\nexisting")
        tool = Office2PDFTool(cwd=str(tmp_path))
        result = tool.execute(file_path="a.docx", overwrite=False)
        assert result.is_error
        assert "already exists" in result.output

    def test_output_outside_workspace_denied(self, tmp_path):
        (tmp_path / "a.docx").write_bytes(b"fake")
        tool = Office2PDFTool(cwd=str(tmp_path))
        outside = tmp_path.parent / "outside.pdf"
        result = tool.execute(file_path="a.docx", output_path=str(outside))
        assert result.is_error
        assert "不在工作区" in result.output or "Access denied" in result.output


class TestOffice2PDFConversion:
    def test_default_output_and_cache(self, tmp_path, cache_dir, fake_worker):
        src = tmp_path / "a.docx"
        src.write_bytes(b"fake docx")
        tool = Office2PDFTool(cwd=str(tmp_path))

        result = tool.execute(file_path="a.docx")

        assert not result.is_error
        out = tmp_path / "a.pdf"
        assert out.exists()
        assert out.read_bytes().startswith(b"%PDF-")
        assert result.meta["cached"] is False
        assert result.meta["engine"] == "Test Office"
        assert len(fake_worker) == 1

        # 第二次转换命中缓存，不再调用 COM worker
        result2 = tool.execute(file_path="a.docx")
        assert not result2.is_error
        assert result2.meta["cached"] is True
        assert len(fake_worker) == 1

        # 源文件 mtime/size 改变 → 换缓存 key，重新转换
        src.write_bytes(b"fake docx changed")
        os.utime(src, (src.stat().st_atime, src.stat().st_mtime + 5))
        result3 = tool.execute(file_path="a.docx")
        assert not result3.is_error
        assert result3.meta["cached"] is False
        assert len(fake_worker) == 2

    def test_custom_output_path(self, tmp_path, cache_dir, fake_worker):
        (tmp_path / "a.xlsx").write_bytes(b"fake xlsx")
        tool = Office2PDFTool(cwd=str(tmp_path))
        result = tool.execute(file_path="a.xlsx", output_path="out/result.pdf")
        assert not result.is_error
        assert (tmp_path / "out" / "result.pdf").exists()
        assert fake_worker[0][1] == "excel"

    def test_conversion_failure(self, tmp_path, cache_dir, monkeypatch):
        (tmp_path / "a.docx").write_bytes(b"fake")

        def fail(*args, **kwargs):
            raise RuntimeError("Office not available")

        monkeypatch.setattr(mod, "_run_worker", fail)
        tool = Office2PDFTool(cwd=str(tmp_path))
        result = tool.execute(file_path="a.docx")
        assert result.is_error
        assert "conversion failed" in result.output

    def test_timeout(self, tmp_path, cache_dir, monkeypatch):
        (tmp_path / "a.docx").write_bytes(b"fake")

        def timeout(*args, **kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr(mod, "_run_worker", timeout)
        tool = Office2PDFTool(cwd=str(tmp_path))
        result = tool.execute(file_path="a.docx", timeout=10)
        assert result.is_error
        assert "timed out" in result.output

    def test_cache_key_changes_with_stat(self, tmp_path):
        src = tmp_path / "a.docx"
        src.write_bytes(b"one")
        key1, stat1 = mod._cache_key(str(src))
        src.write_bytes(b"two-two")
        key2, stat2 = mod._cache_key(str(src))
        assert key1 != key2
