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
from pathlib import Path

from core.config import PROJECT_ROOT
from core.tools.shared.base import Tool, ToolResult


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
        "latex(action='compile', file='document.tex')\n"
        "latex(action='compile', file='document.tex', output='output.pdf')\n"
        "latex(action='compile', file='document.tex', compiler='xelatex')\n"
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
            "file": {
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

    # Auxiliary file extensions to clean up
    AUX_EXTENSIONS = {
        ".aux", ".log", ".out", ".toc", ".lof", ".lot",
        ".bbl", ".blg", ".idx", ".ind", ".ilg",
        ".nav", ".snm", ".vrb", ".fls", ".fdb_latexmk",
        ".synctex.gz", ".synctex.gz(busy)",
    }

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session_manager=None):
        super().__init__(cwd, workspace_uuid, session_manager)
        self._compiler_cache: str | None = None

    def execute(
        self,
        action: str = "compile",
        file: str | None = None,
        output: str | None = None,
        compiler: str | None = None,
        clean: bool = True,
    ) -> ToolResult:
        """Execute LaTeX action."""
        if action == "check":
            return self._check_compilers()
        elif action == "compile":
            if not file:
                return ToolResult("Error: 'file' is required for 'compile' action", error=True)
            return self._compile(file, output, compiler, clean)
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
        file: str,
        output: str | None,
        compiler: str | None,
        clean: bool,
    ) -> ToolResult:
        """Compile a LaTeX file to PDF."""
        # Resolve input file path
        tex_path = self._resolve_path(file)
        if not os.path.exists(tex_path):
            return ToolResult(f"Error: File not found: {tex_path}", error=True)

        if not tex_path.endswith(".tex"):
            return ToolResult(f"Error: File must have .tex extension: {tex_path}", error=True)

        # Determine output path
        if output:
            pdf_path = self._resolve_path(output)
        else:
            pdf_path = tex_path[:-4] + ".pdf"

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
            result = self._run_compiler(compiler, compiler_path, tex_path, pdf_path)
        except Exception as e:
            return ToolResult(f"Compilation error: {e}", error=True)

        if result.error:
            return result

        # Clean auxiliary files
        if clean:
            self._clean_aux_files(tex_path)

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
    ) -> ToolResult:
        """Run the LaTeX compiler."""
        tex_dir = os.path.dirname(tex_path)
        tex_name = os.path.basename(tex_path)

        # Build command
        if compiler == "tectonic":
            # Tectonic outputs to current directory by default
            cmd = [compiler_path, tex_path]
        else:
            # Traditional LaTeX: -output-directory
            cmd = [compiler_path, f"-output-directory={tex_dir}", "-interaction=nonstopmode", tex_path]

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
            error_msg = self._extract_error(result.stdout + result.stderr, compiler)
            return ToolResult(
                f"Compilation failed:\n{error_msg}\n\n"
                f"Full output:\n{result.stdout[-2000:] if result.stdout else ''}",
                error=True
            )

        # Verify PDF was created
        expected_pdf = pdf_path
        if compiler == "tectonic":
            # Tectonic creates PDF in same directory as tex file
            expected_pdf = tex_path[:-4] + ".pdf"

        if not os.path.exists(expected_pdf):
            return ToolResult("Error: PDF file was not created", error=True)

        # Move PDF if needed
        if expected_pdf != pdf_path:
            try:
                shutil.move(expected_pdf, pdf_path)
            except Exception as e:
                return ToolResult(f"Error moving PDF: {e}", error=True)

        return ToolResult(output="OK")

    def _extract_error(self, output: str, compiler: str) -> str:
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
        relevant = [l for l in lines if l.strip()][-5:]
        return "\n".join(relevant) if relevant else "Unknown error"

    def _clean_aux_files(self, tex_path: str) -> None:
        """Clean auxiliary files generated by LaTeX."""
        tex_dir = os.path.dirname(tex_path)
        base_name = os.path.splitext(os.path.basename(tex_path))[0]

        for ext in self.AUX_EXTENSIONS:
            aux_file = os.path.join(tex_dir, base_name + ext)
            if os.path.exists(aux_file):
                try:
                    os.remove(aux_file)
                except Exception:
                    pass  # Ignore cleanup errors

    @staticmethod
    def _format_size(size: int) -> str:
        """Format file size in human-readable format."""
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        else:
            return f"{size / (1024 * 1024):.1f} MB"
