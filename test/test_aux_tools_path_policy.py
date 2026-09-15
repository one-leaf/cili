# -*- coding: utf-8 -*-
"""latex/pdf2markdown 输出路径权限门测试。

输出路径（PDF / .md）在工作区外 → 审批占位；区内 → 门放行继续执行。
"""

from pathlib import Path

from core.tools.approval import ApprovalStore, META_KEY
from core.tools.latex import LatexTool
from core.tools.pdf2markdown import PDF2MarkdownTool

_TEX = "\\documentclass{article}\n\\begin{document}hi\\end{document}\n"


class TestLatexGate:
    def _tool(self, cwd, store=None):
        return LatexTool(cwd=cwd, approval_store=store)

    def test_outside_output_placeholder(self, test_workspace, tmp_path):
        (Path(test_workspace) / "doc.tex").write_text(_TEX, encoding="utf-8")
        tool = self._tool(test_workspace, ApprovalStore())
        outside = tmp_path / "out.pdf"
        result = tool.execute(action="compile", file="doc.tex", output=str(outside))
        assert result.completed is False
        assert not result.error
        assert result.meta[META_KEY]["kind"] == "path:write"
        assert result.meta[META_KEY]["command"] == str(outside)

    def test_inside_output_passes_gate(self, test_workspace):
        (Path(test_workspace) / "doc.tex").write_text(_TEX, encoding="utf-8")
        tool = self._tool(test_workspace, ApprovalStore())
        result = tool.execute(action="compile", file="doc.tex", output="out.pdf")
        # 门放行后进入编译逻辑（无编译器报错），不应返回审批占位
        assert result.completed is not False

    def test_no_store_outside_deny(self, test_workspace, tmp_path):
        (Path(test_workspace) / "doc.tex").write_text(_TEX, encoding="utf-8")
        tool = self._tool(test_workspace)
        outside = tmp_path / "out.pdf"
        result = tool.execute(action="compile", file="doc.tex", output=str(outside))
        assert result.error is True


class TestPDF2MarkdownGate:
    def _tool(self, cwd, store=None):
        return PDF2MarkdownTool(cwd=cwd, approval_store=store)

    def test_outside_output_placeholder(self, test_workspace, tmp_path):
        (Path(test_workspace) / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
        tool = self._tool(test_workspace, ApprovalStore())
        outside = tmp_path / "out.md"
        result = tool.execute(file_path="doc.pdf", output_path=str(outside))
        assert result.completed is False
        assert not result.error
        assert result.meta[META_KEY]["kind"] == "path:write"

    def test_inside_output_passes_gate(self, test_workspace, monkeypatch):
        (Path(test_workspace) / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
        tool = self._tool(test_workspace, ApprovalStore())
        monkeypatch.setattr(tool, "_agent_parse", lambda file_path, timeout: "# md")
        monkeypatch.setattr(tool, "_save_markdown", lambda content, output_path: None)
        result = tool.execute(file_path="doc.pdf", output_path="out.md")
        assert not result.error
        assert "out.md" in result.output

    def test_no_store_outside_deny(self, test_workspace, tmp_path):
        (Path(test_workspace) / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
        tool = self._tool(test_workspace)
        outside = tmp_path / "out.md"
        result = tool.execute(file_path="doc.pdf", output_path=str(outside))
        assert result.error is True
