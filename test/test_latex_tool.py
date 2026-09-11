"""Tests for core/tools/latex.py — LaTeX 编译工具（A45：零覆盖补强）。

编译本身依赖外部 tectonic/TeX 发行版，测试通过 monkeypatch subprocess.run
与编译器探测来覆盖所有分支，不要求本机装有 LaTeX。
"""

import subprocess
from pathlib import Path

import pytest

from core.tools.base import ToolResult
from core.tools.latex import LatexTool


@pytest.fixture
def tool(tmp_path):
    return LatexTool(cwd=str(tmp_path), workspace_uuid="test-ws")


def _fake_process(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class TestActionDispatch:
    def test_unknown_action(self, tool):
        result = tool.execute(action="bogus")
        assert result.error
        assert "Unknown action" in result.output

    def test_compile_requires_file(self, tool):
        result = tool.execute(action="compile")
        assert result.error
        assert "'file' is required" in result.output

    def test_missing_file(self, tool, tmp_path):
        result = tool.execute(action="compile", file="nope.tex")
        assert result.error
        assert "not found" in result.output.lower()

    def test_non_tex_extension(self, tool, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_text("hello", encoding="utf-8")
        result = tool.execute(action="compile", file=str(f))
        assert result.error
        assert ".tex" in result.output


class TestCompilerDiscovery:
    def test_no_compilers(self, tool, monkeypatch):
        monkeypatch.setattr(tool, "_find_all_compilers", lambda: {})
        result = tool._check_compilers()
        assert "No LaTeX compiler found" in result.output

    def test_check_lists_and_best(self, tool, monkeypatch):
        monkeypatch.setattr(
            tool, "_find_all_compilers",
            lambda: {"pdflatex": "/usr/bin/pdflatex", "xelatex": "/usr/bin/xelatex"},
        )
        result = tool._check_compilers()
        assert "pdflatex" in result.output
        assert "xelatex" in result.output
        assert "Best available: pdflatex" in result.output

    def test_priority_tectonic_over_pdflatex(self, tool, monkeypatch):
        monkeypatch.setattr(
            tool, "_find_all_compilers",
            lambda: {"pdflatex": "/a", "tectonic": "/a/tectonic.exe"},
        )
        assert tool._get_best_compiler() == "tectonic"

    def test_priority_pdflatex_over_xelatex(self, tool, monkeypatch):
        monkeypatch.setattr(
            tool, "_find_all_compilers",
            lambda: {"xelatex": "/a", "pdflatex": "/b"},
        )
        assert tool._get_best_compiler() == "pdflatex"

    def test_compiler_cache(self, tool, monkeypatch):
        calls = []

        def fake_find():
            calls.append(1)
            return {"pdflatex": "/b"}

        monkeypatch.setattr(tool, "_find_all_compilers", fake_find)
        assert tool._get_best_compiler() == "pdflatex"
        assert tool._get_best_compiler() == "pdflatex"
        assert len(calls) == 1  # 第二次走缓存

    def test_forced_compiler_not_found(self, tool, monkeypatch):
        monkeypatch.setattr(tool, "_find_compiler", lambda name: None)
        f = tool.cwd and Path(tool.cwd) / "doc.tex"
        f.write_text("\\documentclass{article}", encoding="utf-8")
        result = tool.execute(action="compile", file=str(f), compiler="xelatex")
        assert result.error
        assert "not found" in result.output.lower()


class TestErrorExtraction:
    def test_latex_error_bang(self, tool):
        out = "line1\n! Undefined control sequence.\nl.1 \\nope\n"
        assert tool._extract_error(out, "pdflatex") == "! Undefined control sequence."

    def test_tectonic_error(self, tool):
        out = "some output\nError: cannot find package\nmore"
        assert "cannot find package" in tool._extract_error(out, "tectonic")

    def test_missing_package(self, tool):
        out = "! LaTeX Error: File `nope.sty' not found.\nPackage xxx not found."
        assert "not found" in tool._extract_error(out, "pdflatex")

    def test_falls_back_to_last_lines(self, tool):
        out = "a\nb\nc\n"
        assert tool._extract_error(out, "pdflatex") == "a\nb\nc"


class TestFormatSize:
    def test_bytes(self, tool):
        assert tool._format_size(512) == "512 B"

    def test_kb(self, tool):
        assert tool._format_size(2048) == "2.0 KB"

    def test_mb(self, tool):
        assert tool._format_size(3 * 1024 * 1024) == "3.0 MB"


class TestCleanAux:
    def test_removes_aux_files(self, tool, tmp_path):
        tex = tmp_path / "doc.tex"
        tex.write_text("x", encoding="utf-8")
        (tmp_path / "doc.aux").write_text("a", encoding="utf-8")
        (tmp_path / "doc.log").write_text("l", encoding="utf-8")
        (tmp_path / "doc.pdf").write_text("p", encoding="utf-8")  # 不属于 aux，保留
        tool._clean_aux_files(str(tex))
        assert not (tmp_path / "doc.aux").exists()
        assert not (tmp_path / "doc.log").exists()
        assert (tmp_path / "doc.pdf").exists()


class TestRunCompiler:
    def test_timeout(self, tool, monkeypatch):
        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(["tectonic"], 120)

        monkeypatch.setattr("core.tools.latex.subprocess.run", boom)
        result = tool._run_compiler("tectonic", "/fake/tectonic.exe", "a.tex", "a.pdf")
        assert result.error
        assert "timed out" in result.output.lower()

    def test_nonzero_returncode(self, tool, monkeypatch):
        monkeypatch.setattr(
            "core.tools.latex.subprocess.run",
            lambda *a, **k: _fake_process(returncode=1, stdout="! Missing $ inserted."),
        )
        result = tool._run_compiler("pdflatex", "/fake/pdflatex", "a.tex", "a.pdf")
        assert result.error
        assert "Compilation failed" in result.output
        assert "Missing" in result.output

    def test_pdf_not_created(self, tool, monkeypatch):
        monkeypatch.setattr(
            "core.tools.latex.subprocess.run",
            lambda *a, **k: _fake_process(returncode=0),
        )
        result = tool._run_compiler("tectonic", "/fake/tectonic.exe", "a.tex", "a.pdf")
        assert result.error
        assert "PDF file was not created" in result.output

    def test_tectonic_success_and_move(self, tool, monkeypatch, tmp_path):
        """tectonic 在 tex 同目录产出 pdf，指定不同 output 时被移动到目标。"""
        tex = tmp_path / "doc.tex"
        tex.write_text("\\documentclass{article}", encoding="utf-8")
        custom_out = tmp_path / "out" / "renamed.pdf"
        custom_out.parent.mkdir(parents=True)

        def fake_run(cmd, **kwargs):
            # tectonic 默认在 tex 同目录产出同名 pdf
            (tmp_path / "doc.pdf").write_text("%PDF-fake", encoding="utf-8")
            return _fake_process(returncode=0)

        monkeypatch.setattr("core.tools.latex.subprocess.run", fake_run)
        result = tool._run_compiler(
            "tectonic", "/fake/tectonic.exe", str(tex), str(custom_out)
        )
        assert not result.error
        assert custom_out.exists()
        assert custom_out.read_text(encoding="utf-8") == "%PDF-fake"


class TestCompileFlow:
    def test_compile_success_meta(self, tool, monkeypatch, tmp_path):
        tex = tmp_path / "doc.tex"
        tex.write_text("\\documentclass{article}", encoding="utf-8")
        pdf = tmp_path / "doc.pdf"
        pdf.write_text("%PDF-fake", encoding="utf-8")

        monkeypatch.setattr(
            tool, "_run_compiler",
            lambda c, p, t, out: ToolResult("OK"),
        )
        result = tool.execute(action="compile", file=str(tex))
        assert not result.error
        assert "Compiled successfully" in result.output
        assert result.meta["compiler"] is not None
        assert result.meta["size"] == len("%PDF-fake")

    def test_compile_no_compiler_available(self, tool, monkeypatch, tmp_path):
        tex = tmp_path / "doc.tex"
        tex.write_text("x", encoding="utf-8")
        monkeypatch.setattr(tool, "_get_best_compiler", lambda: None)
        result = tool.execute(action="compile", file=str(tex))
        assert result.error
        assert "No LaTeX compiler found" in result.output
