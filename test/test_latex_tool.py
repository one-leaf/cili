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
        assert "'tex_file' is required" in result.output

    def test_missing_file(self, tool, tmp_path):
        result = tool.execute(action="compile", tex_file="nope.tex")
        assert result.error
        assert "not found" in result.output.lower()

    def test_non_tex_extension(self, tool, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_text("hello", encoding="utf-8")
        result = tool.execute(action="compile", tex_file=str(f))
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
        result = tool.execute(action="compile", tex_file=str(f), compiler="xelatex")
        assert result.error
        assert "not found" in result.output.lower()


class TestErrorExtraction:
    def test_latex_error_bang(self, tool):
        out = "line1\n! Undefined control sequence.\nl.1 \\nope\n"
        assert tool._extract_error(out) == "! Undefined control sequence."

    def test_tectonic_error(self, tool):
        out = "some output\nError: cannot find package\nmore"
        assert "cannot find package" in tool._extract_error(out)

    def test_missing_package(self, tool):
        out = "! LaTeX Error: File `nope.sty' not found.\nPackage xxx not found."
        assert "not found" in tool._extract_error(out)

    def test_falls_back_to_last_lines(self, tool):
        out = "a\nb\nc\n"
        assert tool._extract_error(out) == "a\nb\nc"


class TestFormatSize:
    def test_bytes(self, tool):
        assert tool._format_size(512) == "512 B"

    def test_kb(self, tool):
        assert tool._format_size(2048) == "2.0 KB"

    def test_mb(self, tool):
        assert tool._format_size(3 * 1024 * 1024) == "3.0 MB"


class TestRunCompiler:
    @staticmethod
    def _output_dir(cmd):
        for i, arg in enumerate(cmd):
            if arg.startswith("-output-directory="):
                return arg.split("=", 1)[1]
            if arg == "--outdir":
                return cmd[i + 1]
        raise AssertionError(f"no output dir in {cmd}")

    def test_timeout(self, tool, monkeypatch, tmp_path):
        tex = tmp_path / "a.tex"
        tex.write_text("x", encoding="utf-8")

        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(["tectonic"], 120)

        monkeypatch.setattr("core.tools.latex.subprocess.run", boom)
        result = tool._run_compiler("tectonic", "/fake/tectonic.exe", str(tex), str(tmp_path / "a.pdf"))
        assert result.error
        assert "timed out" in result.output.lower()

    def test_nonzero_returncode(self, tool, monkeypatch, tmp_path):
        tex = tmp_path / "a.tex"
        tex.write_text("x", encoding="utf-8")
        monkeypatch.setattr(
            "core.tools.latex.subprocess.run",
            lambda *a, **k: _fake_process(returncode=1, stdout="! Missing $ inserted."),
        )
        result = tool._run_compiler("pdflatex", "/fake/pdflatex", str(tex), str(tmp_path / "a.pdf"))
        assert result.error
        assert "Compilation failed" in result.output
        assert "Missing" in result.output

    def test_pdf_not_created(self, tool, monkeypatch, tmp_path):
        tex = tmp_path / "a.tex"
        tex.write_text("x", encoding="utf-8")
        monkeypatch.setattr(
            "core.tools.latex.subprocess.run",
            lambda *a, **k: _fake_process(returncode=0),
        )
        result = tool._run_compiler("tectonic", "/fake/tectonic.exe", str(tex), str(tmp_path / "a.pdf"))
        assert result.error
        assert "PDF file was not created" in result.output

    def test_tectonic_success_and_move(self, tool, monkeypatch, tmp_path):
        """tectonic 在 --outdir 产出 pdf，指定不同 output 时被移动到目标。"""
        tex = tmp_path / "doc.tex"
        tex.write_text("\\documentclass{article}", encoding="utf-8")
        custom_out = tmp_path / "out" / "renamed.pdf"
        custom_out.parent.mkdir(parents=True)

        def fake_run(cmd, **kwargs):
            outdir = Path(self._output_dir(cmd))
            (outdir / "doc.pdf").write_text("%PDF-fake", encoding="utf-8")
            return _fake_process(returncode=0)

        monkeypatch.setattr("core.tools.latex.subprocess.run", fake_run)
        result = tool._run_compiler(
            "tectonic", "/fake/tectonic.exe", str(tex), str(custom_out)
        )
        assert not result.error
        assert custom_out.exists()
        assert custom_out.read_text(encoding="utf-8") == "%PDF-fake"

    def test_existing_same_name_pdf_not_clobbered(self, tool, monkeypatch, tmp_path):
        """输出到别的文件名时，源目录里既有的同名 PDF 必须原样保留。

        回归 2026-11-12 自检：编译器此前直接写 <tex名>.pdf，覆盖了用户文件，
        随后被 move 走，表现为「编译一次丢掉一个无关 PDF」。
        """
        tex = tmp_path / "doc.tex"
        tex.write_text("\\documentclass{article}", encoding="utf-8")
        user_pdf = tmp_path / "doc.pdf"
        user_pdf.write_text("USER-PDF-DO-NOT-TOUCH", encoding="utf-8")
        custom_out = tmp_path / "out" / "renamed.pdf"
        custom_out.parent.mkdir(parents=True)

        def fake_run(cmd, **kwargs):
            outdir = Path(self._output_dir(cmd))
            (outdir / "doc.pdf").write_text("%PDF-fake", encoding="utf-8")
            return _fake_process(returncode=0)

        monkeypatch.setattr("core.tools.latex.subprocess.run", fake_run)
        result = tool._run_compiler(
            "pdflatex", "/fake/pdflatex", str(tex), str(custom_out)
        )
        assert not result.error
        assert custom_out.read_text(encoding="utf-8") == "%PDF-fake"
        assert user_pdf.exists()
        assert user_pdf.read_text(encoding="utf-8") == "USER-PDF-DO-NOT-TOUCH"

    def test_clean_true_leaves_no_aux(self, tool, monkeypatch, tmp_path):
        tex = tmp_path / "doc.tex"
        tex.write_text("\\documentclass{article}", encoding="utf-8")

        def fake_run(cmd, **kwargs):
            outdir = Path(self._output_dir(cmd))
            (outdir / "doc.pdf").write_text("%PDF-fake", encoding="utf-8")
            (outdir / "doc.aux").write_text("a", encoding="utf-8")
            (outdir / "doc.log").write_text("l", encoding="utf-8")
            return _fake_process(returncode=0)

        monkeypatch.setattr("core.tools.latex.subprocess.run", fake_run)
        result = tool._run_compiler(
            "pdflatex", "/fake/pdflatex", str(tex), str(tmp_path / "doc.pdf"), True
        )
        assert not result.error
        assert not (tmp_path / "doc.aux").exists()
        assert not (tmp_path / "doc.log").exists()

    def test_clean_false_restores_aux(self, tool, monkeypatch, tmp_path):
        tex = tmp_path / "doc.tex"
        tex.write_text("\\documentclass{article}", encoding="utf-8")

        def fake_run(cmd, **kwargs):
            outdir = Path(self._output_dir(cmd))
            (outdir / "doc.pdf").write_text("%PDF-fake", encoding="utf-8")
            (outdir / "doc.aux").write_text("a", encoding="utf-8")
            (outdir / "doc.log").write_text("l", encoding="utf-8")
            return _fake_process(returncode=0)

        monkeypatch.setattr("core.tools.latex.subprocess.run", fake_run)
        result = tool._run_compiler(
            "pdflatex", "/fake/pdflatex", str(tex), str(tmp_path / "doc.pdf"), False
        )
        assert not result.error
        assert (tmp_path / "doc.aux").read_text(encoding="utf-8") == "a"
        assert (tmp_path / "doc.log").read_text(encoding="utf-8") == "l"


class TestCompileFlow:
    def test_compile_success_meta(self, tool, monkeypatch, tmp_path):
        tex = tmp_path / "doc.tex"
        tex.write_text("\\documentclass{article}", encoding="utf-8")
        pdf = tmp_path / "doc.pdf"
        pdf.write_text("%PDF-fake", encoding="utf-8")

        # CI/无 LaTeX 环境下也稳定：mock 编译器发现与执行
        monkeypatch.setattr(tool, "_get_best_compiler", lambda: "pdflatex")
        monkeypatch.setattr(tool, "_find_compiler", lambda name: f"/fake/{name}")
        monkeypatch.setattr(
            tool, "_run_compiler",
            lambda c, p, t, out, clean: ToolResult("OK"),
        )
        result = tool.execute(action="compile", tex_file=str(tex))
        assert not result.error
        assert "Compiled successfully" in result.output
        assert result.meta["compiler"] is not None
        assert result.meta["size"] == len("%PDF-fake")

    def test_compile_no_compiler_available(self, tool, monkeypatch, tmp_path):
        tex = tmp_path / "doc.tex"
        tex.write_text("x", encoding="utf-8")
        monkeypatch.setattr(tool, "_get_best_compiler", lambda: None)
        result = tool.execute(action="compile", tex_file=str(tex))
        assert result.error
        assert "No LaTeX compiler found" in result.output
