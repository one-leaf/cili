"""office2pdf 工具测试（COM 转换被 mock，不依赖本机 Office）。"""

import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.tools import office2pdf as mod
from core.tools.office2pdf import Office2PDFTool


class _FakePresentation:
    """PowerPoint Presentation 的最小替身。"""

    def __init__(self, output_writer):
        self.output_writer = output_writer
        self.calls = []
        self.saveas_fails = False
        self.export_fails = False
        self.closed = False

    def SaveAs(self, path, fmt):
        self.calls.append(("SaveAs", path, fmt))
        if self.saveas_fails:
            raise RuntimeError("saveas failed")
        self.output_writer(path)

    def ExportAsFixedFormat(self, path, fmt):
        self.calls.append(("Export", path, fmt))
        if self.export_fails:
            raise RuntimeError("export failed")
        self.output_writer(path)

    def Close(self):
        self.closed = True


class _FakeApp:
    """PowerPoint.Application 的最小替身。"""

    def __init__(self, pres):
        self.presentations = pres
        self.Presentations = SimpleNamespace(Open=lambda *a, **k: pres)
        self.quit_called = False

    def Quit(self):
        self.quit_called = True


def _install_fake_win32(monkeypatch, app):
    """把 win32com.client 替换成返回 *app* 的假模块。"""
    pkg = types.ModuleType("win32com")
    client = types.ModuleType("win32com.client")
    client.DispatchEx = lambda progid: app
    pkg.client = client
    monkeypatch.setitem(sys.modules, "win32com", pkg)
    monkeypatch.setitem(sys.modules, "win32com.client", client)


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
        assert tool.parameters.get("required", []) == []
        assert tool.concurrency_safe is False

    def test_sync_action_not_blocked_by_required(self):
        from core.tools.result import ToolResult

        tool = Office2PDFTool(cwd=".")
        coerced = tool.coerce_input({"action": "sync"})
        assert not isinstance(coerced, ToolResult)

    def test_diagnose_action_reports_progids(self):
        tool = Office2PDFTool(cwd=".")
        result = tool.execute(action="diagnose")
        assert not result.is_error
        assert "Office/WPS COM 诊断" in result.output
        assert "Word.Application" in result.output
        assert "KWPS.Application" in result.output

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


class TestComConversion:
    def _pres(self):
        return _FakePresentation(
            lambda path: Path(path).write_bytes(b"%PDF-1.7 fake pdf")
        )

    def test_slides_uses_saveas_pdf(self, tmp_path, monkeypatch):
        """PowerPoint 走 SaveAs(ppSaveAsPDF=32)，不再调用易崩的 ExportAsFixedFormat。"""
        pres = self._pres()
        app = _FakeApp(pres)
        _install_fake_win32(monkeypatch, app)
        out = tmp_path / "slides.pdf"

        engine = mod._convert_with_com(
            str(tmp_path / "a.pptx"), str(out), "slides", str(tmp_path / "p.pid")
        )

        assert engine == "Microsoft Office PowerPoint"
        assert pres.calls == [("SaveAs", os.path.normpath(str(out)), 32)]
        assert out.read_bytes().startswith(b"%PDF-")
        assert pres.closed and app.quit_called

    def test_slides_falls_back_to_export(self, tmp_path, monkeypatch):
        pres = self._pres()
        pres.saveas_fails = True
        app = _FakeApp(pres)
        _install_fake_win32(monkeypatch, app)
        out = tmp_path / "slides.pdf"

        engine = mod._convert_with_com(
            str(tmp_path / "a.pptx"), str(out), "slides", str(tmp_path / "p.pid")
        )

        assert engine == "Microsoft Office PowerPoint"
        assert [c[0] for c in pres.calls] == ["SaveAs", "Export"]
        assert pres.calls[1][2] == 2
        assert out.exists()

    def test_paths_normalized_to_native_separators(self, tmp_path, monkeypatch):
        """PowerPoint 的 SaveAs 拒绝正斜杠路径，传入前统一 normpath。"""
        pres = self._pres()
        app = _FakeApp(pres)
        _install_fake_win32(monkeypatch, app)
        out = tmp_path / "slides.pdf"
        forward_out = str(out).replace("\\", "/")

        mod._convert_with_com(
            str(tmp_path / "a.pptx").replace("\\", "/"),
            forward_out,
            "slides",
            str(tmp_path / "p.pid"),
        )

        assert pres.calls[0][1] == os.path.normpath(forward_out)

    def test_both_engines_failing_reports_both(self, tmp_path, monkeypatch):
        pres = self._pres()
        pres.saveas_fails = True
        pres.export_fails = True
        app = _FakeApp(pres)
        _install_fake_win32(monkeypatch, app)

        with pytest.raises(RuntimeError) as exc:
            mod._convert_with_com(
                str(tmp_path / "a.pptx"), str(tmp_path / "s.pdf"),
                "slides", str(tmp_path / "p.pid"),
            )
        assert "Microsoft Office PowerPoint" in str(exc.value)
        assert "WPS Presentation" in str(exc.value)
        assert "SaveAs failed" in str(exc.value)
        assert "ExportAsFixedFormat failed" in str(exc.value)
        # 引擎能创建、只是转换失败 → 不附注册表诊断（避免噪音）
        assert "COM 注册检查" not in str(exc.value)


class TestEngineDiscovery:
    """ProgID 候选与注册表扫描（失败兜底）。"""

    def test_engine_candidates_include_wps_aliases(self):
        """WPS 各发行版命名不一致：带 K 前缀与别名 ProgID 都要尝试。"""
        assert [p for _l, p in mod._engine_candidates("word")] == [
            "Word.Application", "KWPS.Application", "WPS.Application",
        ]
        assert [p for _l, p in mod._engine_candidates("excel")] == [
            "Excel.Application", "KET.Application", "ET.Application",
        ]
        assert [p for _l, p in mod._engine_candidates("slides")] == [
            "PowerPoint.Application", "KWPP.Application", "WPP.Application",
        ]

    def test_server_exe_name_parses_commands(self):
        assert mod._server_exe_name(
            r'"C:\Program Files\WPS\wps.exe" /automation') == "wps.exe"
        assert mod._server_exe_name(
            r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE /automation") == "excel.exe"
        assert mod._server_exe_name("") == ""

    def test_discovered_candidates_uses_exe_label(self, monkeypatch):
        view = "64" if sys.maxsize > 2 ** 32 else "32"
        status = {
            "64": {"progid": True, "clsid": "{x}", "clsid_visible": view == "64",
                   "server": r"C:\WPS\wps.exe"},
            "32": {"progid": True, "clsid": "{x}", "clsid_visible": view == "32",
                   "server": r"C:\WPS\wps.exe"},
        }
        monkeypatch.setattr(
            mod, "_discover_office_engines",
            lambda: [("word", "WPS.Office.Application", status)],
        )
        assert mod._discovered_candidates("word") == [("WPS Writer", "WPS.Office.Application")]

    def test_discovered_candidates_skips_other_view_only(self, monkeypatch):
        """CLSID 只在另一注册表视图：本进程无法激活，不能当作候选。"""
        other = "32" if sys.maxsize > 2 ** 32 else "64"
        status = {
            "64": {"progid": True, "clsid": "{x}", "clsid_visible": other == "64",
                   "server": r"C:\WPS\wps.exe"},
            "32": {"progid": True, "clsid": "{x}", "clsid_visible": other == "32",
                   "server": r"C:\WPS\wps.exe"},
        }
        monkeypatch.setattr(
            mod, "_discover_office_engines",
            lambda: [("word", "WPS.Office.Application", status)],
        )
        assert mod._discovered_candidates("word") == []

    def test_convert_falls_back_to_discovered_progid(self, tmp_path, monkeypatch):
        """静态 ProgID 全部无效时，用注册表扫描到的 ProgID 完成转换。"""
        pres = _FakePresentation(lambda path: Path(path).write_bytes(b"%PDF-1.7 fake"))
        app = _FakeApp(pres)

        pkg = types.ModuleType("win32com")
        client = types.ModuleType("win32com.client")

        def dispatch(progid):
            if progid == "Found.Application":
                return app
            raise RuntimeError("invalid class string")

        client.DispatchEx = dispatch
        pkg.client = client
        monkeypatch.setitem(sys.modules, "win32com", pkg)
        monkeypatch.setitem(sys.modules, "win32com.client", client)

        monkeypatch.setattr(mod, "_ENGINE_PROGIDS", {
            "slides": [("Microsoft Office PowerPoint", ("Missing.Application",))],
        })
        monkeypatch.setattr(mod, "_discover_office_engines", lambda: [
            ("slides", "Found.Application", {
                "64": {"progid": True, "clsid": "{x}", "clsid_visible": True,
                       "server": r"C:\WPS\wpp.exe"},
                "32": {"progid": True, "clsid": "{x}", "clsid_visible": True,
                       "server": r"C:\WPS\wpp.exe"},
            }),
        ])

        engine = mod._convert_with_com(
            str(tmp_path / "a.pptx"), str(tmp_path / "a.pdf"),
            "slides", str(tmp_path / "p.pid"),
        )

        assert engine == "WPS Presentation"
        assert (tmp_path / "a.pdf").read_bytes().startswith(b"%PDF-")


class TestWorkspaceCacheAndSync:
    def test_workspace_cache_dir_layout(self, tmp_path, monkeypatch):
        import core.config as config_mod

        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        config_mod.upsert_workspace_entry({
            "uuid": "wscache",
            "workspace_name": "Cache WS",
            "directory": str(tmp_path),
        })

        from core.tools.office2pdf import workspace_cache_dir

        d = workspace_cache_dir("wscache")
        assert d == config_mod.get_workspace_data_dir("wscache") / "cache" / "pdf"
        assert d.exists()

    def test_ensure_pdf_uses_given_cache_dir(self, tmp_path, fake_worker):
        src = tmp_path / "a.docx"
        src.write_bytes(b"fake docx")
        cdir = tmp_path / "cachepdf"

        pdf, engine, cached = mod.ensure_pdf(str(src), cache_dir=cdir)

        assert pdf.parent == cdir
        assert cached is False
        assert len(list(cdir.glob("*.pdf"))) == 1

        # 复用：不再调用 worker
        pdf2, _e, cached2 = mod.ensure_pdf(str(src), cache_dir=cdir)
        assert cached2 is True and pdf2 == pdf
        assert len(fake_worker) == 1

    def test_superseded_version_removed(self, tmp_path, fake_worker):
        src = tmp_path / "a.docx"
        src.write_bytes(b"v1")
        cdir = tmp_path / "cachepdf"

        mod.ensure_pdf(str(src), cache_dir=cdir)
        assert len(list(cdir.glob("*.pdf"))) == 1

        src.write_bytes(b"v2 with different content")
        os.utime(src, (src.stat().st_atime, src.stat().st_mtime + 5))
        mod.ensure_pdf(str(src), cache_dir=cdir)

        # 同源旧版本被清理：目录里只有最新一份 pdf+meta
        pdfs = list(cdir.glob("*.pdf"))
        metas = list(cdir.glob("*.json"))
        assert len(pdfs) == 1 and len(metas) == 1
        assert pdfs[0].stem == metas[0].stem

    def test_sync_converts_and_cleans_stale(self, tmp_path, monkeypatch, fake_worker):
        import json as json_mod

        import core.config as config_mod

        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        (ws_dir / "a.docx").write_bytes(b"doc a")
        (ws_dir / "sub").mkdir()
        (ws_dir / "sub" / "b.xlsx").write_bytes(b"doc b")
        config_mod.upsert_workspace_entry({
            "uuid": "wssync",
            "workspace_name": "Sync WS",
            "directory": str(ws_dir),
        })

        from core.tools.office2pdf import workspace_cache_dir

        cdir = workspace_cache_dir("wssync")
        # 预置一个源已删除的过期缓存 + 一个损坏的 meta
        stale_meta = cdir / "deadbeef.json"
        stale_meta.write_text(json_mod.dumps({"source": str(tmp_path / "gone.docx")}), encoding="utf-8")
        (cdir / "deadbeef.pdf").write_bytes(b"%PDF-1.7\nstale")
        broken_meta = cdir / "broken.json"
        broken_meta.write_text("not json", encoding="utf-8")

        result = mod.sync_workspace_pdf_cache("wssync")

        assert result["docs"] == 2
        assert result["converted"] == 2
        assert result["failed"] == 0
        assert result["removed_stale"] == 2
        assert not stale_meta.exists() and not (cdir / "deadbeef.pdf").exists()
        assert not broken_meta.exists()

        # 第二轮全部命中缓存
        result2 = mod.sync_workspace_pdf_cache("wssync")
        assert result2["converted"] == 0 and result2["skipped"] == 2

    def test_sync_limit_defers_conversion(self, tmp_path, monkeypatch, fake_worker):
        import core.config as config_mod

        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        for i in range(3):
            (ws_dir / f"d{i}.docx").write_bytes(b"x")
        config_mod.upsert_workspace_entry({
            "uuid": "wslimit",
            "workspace_name": "Limit WS",
            "directory": str(ws_dir),
        })

        result = mod.sync_workspace_pdf_cache("wslimit", limit=1)
        assert result["converted"] == 1
        assert result["failed"] == 0

    def test_tool_sync_action_reports(self, tmp_path, monkeypatch, fake_worker):
        import core.config as config_mod

        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        (ws_dir / "a.docx").write_bytes(b"doc")
        config_mod.upsert_workspace_entry({
            "uuid": "wssync2",
            "workspace_name": "Sync2 WS",
            "directory": str(ws_dir),
        })

        tool = Office2PDFTool(cwd=str(tmp_path))
        result = tool.execute(action="sync")

        assert not result.is_error
        assert "新转换: 1" in result.output
        assert "wssync2" in result.output

    def test_tool_sync_skips_system_workspace(self, tmp_path, monkeypatch, fake_worker):
        import core.config as config_mod

        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        config_mod.upsert_workspace_entry({
            "uuid": "sysws",
            "workspace_name": "System",
            "directory": str(tmp_path),
            "system": True,
        })

        tool = Office2PDFTool(cwd=str(tmp_path))
        result = tool.execute(action="sync")

        assert not result.is_error
        assert "工作区数: 0" in result.output
