"""LaTeX tool - compile .tex files to PDF.

Design philosophy:
- Priority: data/deps/tectonic > PATH tectonic > pdflatex/xelatex/lualatex
- Tectonic is preferred: modern, self-contained, auto-downloads packages
- Falls back to traditional LaTeX distributions (MiKTeX/TeX Live)
- Supports cleaning auxiliary files after compilation
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from core.config import PROJECT_ROOT
from core.security.path_policy import OP_WRITE, PathTarget
from core.tools.base import Tool, ToolResult

# Tectonic directory under data/deps/
_TECTONIC_DIR = str(PROJECT_ROOT / "data" / "deps" / "tectonic")


class LatexTool(Tool):
    """Compile LaTeX files to PDF.

    Priority order for LaTeX compilers:
    1. data/deps/tectonic/tectonic.exe (preferred, self-contained)
    2. tectonic in PATH
    3. pdflatex/xelatex/lualatex in PATH (traditional LaTeX distributions)
    """

    name = "latex"
    description = (
        "**Compile LaTeX files to PDF.**\n\n"
        "This tool compiles .tex files into PDF documents using LaTeX.\n\n"
        "## Actions:\n"
        "- **compile**: Compile a .tex file to PDF (default)\n"
        "- **check**: Check available LaTeX compilers\n\n"
        "## Usage:\n"
        "```\n"
        "latex(action='compile', tex_file='document.tex')\n"
        "latex(action='compile', tex_file='document.tex', output='output.pdf')\n"
        "latex(action='compile', tex_file='document.tex', compiler='xelatex')\n"
        "latex(action='check')\n"
        "```\n\n"
        "## Supported compilers:\n"
        "- **tectonic** (preferred): Modern, self-contained, auto-downloads packages\n"
        "- **pdflatex**: Traditional LaTeX (requires full TeX distribution)\n"
        "- **xelatex**: For Unicode/advanced typography\n"
        "- **lualatex**: Modern engine with Lua scripting\n\n"
        "## Notes:\n"
        "- Tectonic is automatically downloaded to data/deps/tectonic/ if not found\n"
        "- Auxiliary files (.aux, .log, .out, etc.) are cleaned up by default\n"
        "- For Chinese documents, use xelatex compiler\n"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["compile", "check"],
                "description": "Action: compile (default) or check available compilers",
                "default": "compile",
            },
            "tex_file": {
                "type": "string",
                "description": "Path to the .tex file to compile. Required for compile action.",
            },
            "output": {
                "type": "string",
                "description": "Output PDF path. Defaults to same directory as input with .pdf extension.",
            },
            "compiler": {
                "type": "string",
                "enum": ["tectonic", "pdflatex", "xelatex", "lualatex"],
                "description": "Force a specific compiler. If not specified, uses the best available.",
            },
            "clean": {
                "type": "boolean",
                "description": "Clean auxiliary files after compilation. Default: true",
                "default": True,
            },
        },
        "required": [],
    }

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session=None,
                 permission_store=None, permission_rules=None, permission_policy=None):
        super().__init__(
            cwd, workspace_uuid, session,
            permission_store=permission_store, permission_rules=permission_rules,
            permission_policy=permission_policy,
        )
        self._compiler_cache: str | None = None

    def execute(
        self,
        action: str = "compile",
        tex_file: str | None = None,
        output: str | None = None,
        compiler: str | None = None,
        clean: bool = True,
    ) -> ToolResult:
        """Execute LaTeX action."""
        if action == "check":
            return self._check_compilers()
        elif action == "compile":
            if not tex_file:
                return ToolResult("Error: 'tex_file' is required for 'compile' action", error=True)
            return self._compile(tex_file, output, compiler, clean)
        else:
            return ToolResult(f"Error: Unknown action '{action}'", error=True)

    def _check_compilers(self) -> ToolResult:
        """Check available LaTeX compilers."""
        compilers = self._find_all_compilers()

        if not compilers:
            return ToolResult(
                "No LaTeX compiler found.\n"
                "Please install Tectonic or a LaTeX distribution (MiKTeX/TeX Live)."
            )

        lines = ["Available LaTeX compilers:"]
        for name, path in compilers.items():
            marker = " (preferred)" if name == "tectonic" else ""
            lines.append(f"  - {name}: {path}{marker}")

        best = self._get_best_compiler()
        lines.append(f"\nBest available: {best}")

        return ToolResult("\n".join(lines))

    def _find_all_compilers(self) -> dict[str, str]:
        """Find all available LaTeX compilers."""
        compilers = {}

        # 1. Check deps tectonic
        deps_tectonic = os.path.join(_TECTONIC_DIR, "tectonic.exe")
        if os.path.exists(deps_tectonic):
            compilers["tectonic"] = deps_tectonic

        # 2. Check PATH compilers
        for cmd in ["tectonic", "pdflatex", "xelatex", "lualatex"]:
            if cmd in compilers:
                continue
            path = shutil.which(cmd)
            if path:
                compilers[cmd] = path

        return compilers

    def _get_best_compiler(self) -> str | None:
        """Get the best available LaTeX compiler."""
        if self._compiler_cache:
            return self._compiler_cache

        compilers = self._find_all_compilers()
        if not compilers:
            return None

        # Priority: tectonic > pdflatex > xelatex > lualatex
        priority = ["tectonic", "pdflatex", "xelatex", "lualatex"]
        for cmd in priority:
            if cmd in compilers:
                self._compiler_cache = cmd
                return cmd

        # Return any available
        result = next(iter(compilers))
        self._compiler_cache = result
        return result

    def _compile(
        self,
        tex_file: str,
        output: str | None,
        compiler: str | None,
        clean: bool,
    ) -> ToolResult:
        """Compile a LaTeX file to PDF."""
        # Resolve input file path
        tex_path = self._resolve_path(tex_file, read_only=True)
        if not os.path.exists(tex_path):
            return ToolResult(f"Error: File not found: {tex_path}", error=True)

        if not tex_path.endswith(".tex"):
            return ToolResult(f"Error: File must have .tex extension: {tex_path}", error=True)

        gate = self._read_gate([tex_path])
        if gate:
            return gate

        # Determine output path
        if output:
            pdf_path = self._resolve_path(output)
        else:
            pdf_path = tex_path[:-4] + ".pdf"

        # 路径权限门：输出 PDF 写入工作区外需审批/拒绝
        gate = self._path_gate([PathTarget(OP_WRITE, pdf_path, "output", resolved=pdf_path)])
        if gate:
            return gate

        # Get compiler
        if compiler:
            compiler_path = self._find_compiler(compiler)
            if not compiler_path:
                return ToolResult(
                    f"Error: Compiler '{compiler}' not found.\n"
                    f"Run latex(action='check') to see available compilers.",
                    error=True
                )
        else:
            compiler = self._get_best_compiler()
            if not compiler:
                return ToolResult(
                    "Error: No LaTeX compiler found.\n"
                    "Please install Tectonic or a LaTeX distribution.",
                    error=True
                )
            compiler_path = self._find_compiler(compiler)

        # Compile
        try:
            result = self._run_compiler(compiler, compiler_path, tex_path, pdf_path, clean)
        except Exception as e:
            return ToolResult(f"Compilation error: {e}", error=True)

        if result.error:
            return result

        # Build result message
        output_size = os.path.getsize(pdf_path)
        size_str = self._format_size(output_size)

        lines = [
            f"Compiled successfully: {os.path.basename(tex_path)}",
            f"Output: {pdf_path}",
            f"Size: {size_str}",
            f"Compiler: {compiler}",
        ]

        return ToolResult(
            output="\n".join(lines),
            meta={
                "input": tex_path,
                "output": pdf_path,
                "compiler": compiler,
                "size": output_size,
            }
        )

    def _find_compiler(self, name: str) -> str | None:
        """Find a specific compiler by name."""
        # Special case: deps tectonic
        if name == "tectonic":
            deps_tectonic = os.path.join(_TECTONIC_DIR, "tectonic.exe")
            if os.path.exists(deps_tectonic):
                return deps_tectonic

        # Check PATH
        return shutil.which(name)

    def _run_compiler(
        self,
        compiler: str,
        compiler_path: str,
        tex_path: str,
        pdf_path: str,
        clean: bool = True,
    ) -> ToolResult:
        """Run the LaTeX compiler in an isolated output directory.

        编译产物先落到私有临时目录再移出：编译器会无条件写入
        ``<源文件名>.pdf``，若直接输出到源目录，同目录既有的同名 PDF 会被
        覆盖，移动产物时用户文件随之消失（2026-11-12 自检复现）。隔离后
        只有 pdf_path 会被写，源目录里的同名文件不再被触碰。

        临时目录建在 cili 工作区临时区 ``{workspace}/.cili/tmp/{session_id}/``
        下（与 temp 工具同根），不使用系统临时目录：编译中间产物统一落在
        cili 自有目录，便于 temp 工具清理，也不会被系统清理程序中途删除。
        编译时 cwd 仍为 tex_dir，相对路径的图片与 \\input 依赖照常解析。
        """
        tex_dir = os.path.dirname(os.path.abspath(tex_path))
        tex_name = os.path.basename(tex_path)
        stem = os.path.splitext(tex_name)[0]

        try:
            work_dir = self._make_work_dir()
        except OSError as e:
            return ToolResult(f"Error creating temp output dir: {e}", error=True)

        try:
            # Build command
            if compiler == "tectonic":
                # tectonic 支持 --outdir（0.15+），默认输出到 cwd
                cmd = [compiler_path, "--outdir", work_dir, tex_path]
            else:
                # Traditional LaTeX: -output-directory
                cmd = [compiler_path, f"-output-directory={work_dir}",
                       "-interaction=nonstopmode", tex_path]

            # Run compilation
            try:
                result = subprocess.run(
                    cmd,
                    cwd=tex_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,  # 2 minute timeout
                    encoding="utf-8",
                    errors="replace",
                )
            except subprocess.TimeoutExpired:
                return ToolResult("Error: Compilation timed out (120s)", error=True)
            except Exception as e:
                return ToolResult(f"Error running compiler: {e}", error=True)

            # Check result
            if result.returncode != 0:
                # Extract error from output
                error_msg = self._extract_error(result.stdout + result.stderr)
                return ToolResult(
                    f"Compilation failed:\n{error_msg}\n\n"
                    f"Full output:\n{result.stdout[-2000:] if result.stdout else ''}",
                    error=True
                )

            # Verify PDF was created（两种编译器都写在输出目录下的 <stem>.pdf）
            expected_pdf = os.path.join(work_dir, stem + ".pdf")
            if not os.path.exists(expected_pdf):
                return ToolResult("Error: PDF file was not created", error=True)

            # Move PDF to the requested path（输出目录不存在时补建）
            try:
                out_dir = os.path.dirname(pdf_path)
                if out_dir:
                    os.makedirs(out_dir, exist_ok=True)
                shutil.move(expected_pdf, pdf_path)
            except Exception as e:
                return ToolResult(f"Error moving PDF: {e}", error=True)

            # clean=false 时把辅助文件回迁到源目录（保持原有「保留 aux」语义）
            if not clean:
                self._restore_aux_files(work_dir, tex_dir)

            return ToolResult(output="OK")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _make_work_dir(self) -> str:
        """在工作区临时区建本次编译的私有输出目录。

        与 temp 工具同根：``{workspace}/.cili/tmp/{session_id}/latex-xxxx``。
        每次编译独立目录，避免并行编译互相覆盖；用完即删。
        """
        # 局部导入：测试会 monkeypatch core.config.get_workspace_data_dir
        from core.config import get_workspace_data_dir

        session_id = "no-session"
        if self.session and hasattr(self.session, "session_id"):
            session_id = self.session.session_id or "no-session"
        tmp_base = get_workspace_data_dir(self.workspace_uuid) / "tmp" / session_id
        tmp_base.mkdir(parents=True, exist_ok=True)
        return tempfile.mkdtemp(prefix="latex-", dir=tmp_base)

    @staticmethod
    def _restore_aux_files(work_dir: str, tex_dir: str) -> None:
        """把临时输出目录里的辅助文件复制回源目录（clean=false 时）。"""
        for name in os.listdir(work_dir):
            src = os.path.join(work_dir, name)
            if not os.path.isfile(src) or name.lower().endswith(".pdf"):
                continue
            try:
                shutil.copy2(src, os.path.join(tex_dir, name))
            except OSError:
                pass

    def _extract_error(self, output: str) -> str:
        """Extract meaningful error message from compiler output."""
        lines = output.split("\n")

        # Look for common error patterns
        for line in lines:
            line = line.strip()
            if not line:
                continue

            # LaTeX errors
            if line.startswith("!"):
                return line

            # Tectonic errors
            if "error:" in line.lower():
                return line

            # Missing package
            if "Package" in line and "not found" in line:
                return line

        # Return last few lines if no specific error found
        relevant = [ln for ln in lines if ln.strip()][-5:]
        return "\n".join(relevant) if relevant else "Unknown error"

    @staticmethod
    def _format_size(size: int) -> str:
        """Format file size in human-readable format."""
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        else:
            return f"{size / (1024 * 1024):.1f} MB"
