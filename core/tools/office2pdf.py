"""Office document → PDF conversion tool.

转换基础设施在 ``core/office_pdf/``：缓存（cache.py）、工作区同步（sync.py）、
COM worker（convert.py）。本文件只保留 Tool 门面与公开重导出。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from core.office_pdf import convert as office_convert
from core.office_pdf.cache import (
    _DEFAULT_TIMEOUT,
    SUPPORTED_EXTENSIONS,
    copy_pdf,
    ensure_pdf,
    workspace_cache_dir,
)
from core.office_pdf.sync import sync_workspace_pdf_cache
from core.security.path_policy import OP_WRITE, PathTarget
from core.tools.base import Tool, ToolResult

__all__ = [
    "Office2PDFTool",
    "SUPPORTED_EXTENSIONS",
    "copy_pdf",
    "ensure_pdf",
    "sync_workspace_pdf_cache",
    "workspace_cache_dir",
]

logger = logging.getLogger(__name__)

# 工具 schema 的 timeout 范围（转换层默认值在 cache._DEFAULT_TIMEOUT）
_MIN_TIMEOUT = 10
_MAX_TIMEOUT = 600

class Office2PDFTool(Tool):
    """Convert Word/Excel/PowerPoint documents to PDF via Office/WPS COM."""

    name = "office2pdf"
    description = """Convert a Word, Excel, or PowerPoint document to PDF using the locally installed Microsoft Office or WPS COM engine.

Supported inputs: .doc, .docx, .docm, .rtf, .xls, .xlsx, .xlsm, .xlsb, .ppt, .pptx, .pptm, .ppsx, .ppsm, .potx.

The source is opened read-only with macros and external link updates disabled. Conversion runs in an isolated worker process with a timeout; results are cached by source path + mtime + size, so repeated conversions of an unchanged file reuse the PDF.

By default the PDF is written to <filename>.pdf in the current directory; pass output_path to choose another path. Pass overwrite=false to refuse replacing an existing PDF.

action="sync" batch-syncs workspace Office documents into the per-workspace preview cache ({workspace}/.cili/cache/pdf/): converts new or changed files, and removes cached PDFs whose source no longer exists. Used by the office-pdf-sync cron task; sync writes only inside .cili/cache/pdf and never touches user files.

action="diagnose" reports which Office/WPS COM ProgIDs are registered (64-bit and 32-bit registry views) and which alternative ProgIDs were discovered; use it when conversion fails with "invalid class string" / CO_E_CLASSSTRING or "class not registered".

The tool tries Microsoft Office ProgIDs first, then WPS variants (KWPS/WPS, KET/ET, KWPP/WPP), then scans the registry for any other registered Office/WPS ProgID. On failure the error includes the registry state so the cause (missing or broken Office/WPS COM registration) is visible without guesswork.

Use this tool instead of writing ad-hoc Word/Excel/PowerPoint COM scripts for PDF export.
"""
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["convert", "sync", "diagnose"],
                "description": "convert: convert a single document (default). sync: batch-sync workspaces into the preview cache and clean stale PDFs. diagnose: report registered Office/WPS COM ProgIDs.",
                "default": "convert",
            },
            "file_path": {
                "type": "string",
                "description": "Workspace-relative path to the Office document (convert only).",
            },
            "workspace_uuid": {
                "type": "string",
                "description": "sync only: limit to this workspace UUID. Default: all workspaces (System excluded).",
            },
            "limit": {
                "type": "integer",
                "description": "sync only: max new conversions per workspace per run (default 100).",
                "minimum": 1,
                "maximum": 1000,
                "default": 100,
            },
            "output_path": {
                "type": "string",
                "description": "Optional output PDF path. Defaults to <filename>.pdf in the current directory.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Whether to replace an existing output PDF (default: true).",
                "default": True,
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Per-document conversion timeout in seconds "
                    f"({_MIN_TIMEOUT}-{_MAX_TIMEOUT}, default {_DEFAULT_TIMEOUT}). "
                    "Applies to convert and sync."),
                "minimum": _MIN_TIMEOUT,
                "maximum": _MAX_TIMEOUT,
                "default": _DEFAULT_TIMEOUT,
            },
        },
        "required": [],
    }
    concurrency_safe = False

    def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action") or "convert").strip().lower()
        if action == "sync":
            return self._execute_sync(kwargs)
        if action == "diagnose":
            return ToolResult(self._diagnose())

        file_path = str(kwargs.get("file_path") or "").strip()
        if not file_path:
            return ToolResult("Error: file_path is required", error=True)

        abs_path = self._resolve_path(file_path)
        if not os.path.isfile(abs_path):
            return ToolResult(f"Error: file not found: {abs_path}", error=True)

        ext = Path(abs_path).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            return ToolResult(
                f"Error: unsupported file type '{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
                error=True,
            )

        gate = self._read_gate([abs_path])
        if gate:
            return gate

        output_arg = str(kwargs.get("output_path") or "").strip()
        if output_arg:
            output_path = self._resolve_path(output_arg)
        else:
            output_path = os.path.join(self.cwd, f"{Path(abs_path).stem}.pdf")

        gate = self._path_gate([PathTarget(OP_WRITE, output_path, "output", resolved=output_path)])
        if gate:
            return gate

        overwrite = kwargs.get("overwrite", True)
        if isinstance(overwrite, str):
            overwrite = overwrite.strip().lower() not in ("false", "0", "no", "off")
        if os.path.exists(output_path) and not overwrite:
            return ToolResult(f"Error: output already exists: {output_path}", error=True)

        try:
            timeout = int(kwargs.get("timeout", _DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
        timeout = max(_MIN_TIMEOUT, min(timeout, _MAX_TIMEOUT))

        try:
            ws_uuid = str(getattr(self, "workspace_uuid", "") or "")
            cache_dir = workspace_cache_dir(ws_uuid) if ws_uuid else None
            pdf_path, engine, cached = ensure_pdf(abs_path, cache_dir=cache_dir, timeout=timeout)
            copy_pdf(pdf_path, output_path)
        except TimeoutError as e:
            return ToolResult(f"Error: {e}", error=True)
        except Exception as e:
            logger.warning(f"[office2pdf] conversion failed for {abs_path}: {e}")
            return ToolResult(
                f"Error: Office PDF conversion failed: {e}\n"
                "Ensure Microsoft Office or WPS is installed and no dialogs are blocking automation. "
                "Run office2pdf with action=\"diagnose\" to inspect the COM registration state.",
                error=True,
            )

        output = (
            f"PDF: {output_path}\n"
            f"Engine: {engine}\n"
            f"Source: {abs_path}\n"
            f"Cached: {'yes' if cached else 'no'}"
        )
        return ToolResult(output, meta={
            "output_path": output_path,
            "cached": cached,
            "engine": engine,
        })

    def _execute_sync(self, kwargs: dict) -> ToolResult:
        """批量同步工作区 Office 文档到预览缓存。

        写操作限定在各工作区 ``.cili/cache/pdf/``（工具内部缓存，不触碰
        用户文件），因此不走路径策略门禁；跨工作区文档读取按「区外读取
        默认允许」策略放行。
        """
        try:
            limit = int(kwargs.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 1000))
        try:
            timeout = int(kwargs.get("timeout", _DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
        timeout = max(_MIN_TIMEOUT, min(timeout, _MAX_TIMEOUT))

        ws_arg = str(kwargs.get("workspace_uuid") or "").strip()
        if ws_arg:
            targets = [ws_arg]
        else:
            from core.config import SYSTEM_WORKSPACE_UUID, load_workspaces_index

            targets = [
                str(e.get("uuid", "")) for e in load_workspaces_index()
                if e.get("uuid") and not e.get("system") and str(e.get("uuid")) != SYSTEM_WORKSPACE_UUID
            ]

        results: list[dict] = []
        totals = {"converted": 0, "skipped": 0, "failed": 0, "removed_stale": 0}
        for uuid in targets:
            try:
                r = sync_workspace_pdf_cache(uuid, limit=limit, timeout=timeout)
            except Exception as e:
                logger.warning(f"[office2pdf] sync {uuid} failed: {e}")
                results.append({"workspace_uuid": uuid, "error": str(e)})
                continue
            results.append(r)
            for k in totals:
                totals[k] += int(r.get(k, 0))

        lines = [
            f"工作区数: {len(results)}",
            f"新转换: {totals['converted']} · 复用缓存: {totals['skipped']} · "
            f"清理过期: {totals['removed_stale']} · 失败: {totals['failed']}",
        ]
        for r in results:
            if r.get("error"):
                lines.append(f"- {r['workspace_uuid']}: 错误 {r['error']}")
                continue
            if r.get("converted") or r.get("removed_stale") or r.get("failed"):
                lines.append(
                    f"- {r['workspace_uuid']}: 转换 {r['converted']} / 复用 {r['skipped']} / "
                    f"清理 {r['removed_stale']} / 失败 {r['failed']}"
                )
                for err in r.get("errors", []):
                    lines.append(f"  · {err}")
        return ToolResult("\n".join(lines), meta={"totals": totals, "results": results})

    def _diagnose(self) -> str:
        """报告 Office/WPS COM 注册状态，用于排查转换失败的环境原因。"""
        bits = "64" if sys.maxsize > 2 ** 32 else "32"
        lines = [f"Office/WPS COM 诊断（Python {bits} 位）", ""]
        for kind in ("word", "excel", "slides"):
            order = " → ".join(progid for _l, progid in office_convert._engine_candidates(kind))
            lines.append(f"[{kind}] 尝试顺序: {order}")
        discovered = office_convert._discover_office_engines()
        if discovered:
            lines.append("扫描发现的其它 Office/WPS ProgID:")
            for kind, progid, status in sorted(discovered, key=lambda x: (x[0], x[1])):
                marks = " ".join(
                    f"{view}:{'√' if status.get(view, {}).get('progid') else '×'}"
                    for view in ("64", "32")
                )
                lines.append(f"- {progid} [{kind}] ({marks})")
        else:
            lines.append("扫描发现的其它 Office/WPS ProgID: 无")
        lines.append("")
        lines.append(office_convert._format_registry_report())
        return "\n".join(lines)


