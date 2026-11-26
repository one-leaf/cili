"""PDF/Image to Markdown conversion via MinerU API.

Two API modes:
1. Precision Parse API (commercial, needs API key) — ≤200MB, ≤200 pages,
   model_version defaults to "vlm" for best accuracy
2. Agent API (free, no key) — ≤10MB, ≤20 pages, Markdown only

Mode selection:
- mineru_api_key configured: Precision only, no free Agent fallback
  (the free light API has lower accuracy); failures are returned as errors
- no key: free Agent API only

PDFs exceeding API limits (size or pages) are split locally with pypdf into
compliant parts for both the Precision API (≤200MB/≤200 pages) and the free
Agent API (≤10MB/≤20 pages); the parts are parsed one by one and merged back
into a single markdown document in page order.

Polling semantics: while the server reports ``pending``/``running`` the task is
still executing and polling keeps waiting, but a total timeout applies (default
1 hour per file) so a lost task cannot hang the caller forever; repeated
consecutive network errors also abort.

``convert_file()`` is the reusable entry point: it returns markdown text and
lets exceptions propagate (used by external scripts such as the textbook batch
conversion, so there is only one implementation).

Official docs: https://mineru.net/apiManage/docs
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    from pypdf import PdfReader, PdfWriter, apply_configuration, get_configuration
except ImportError:   # 未安装 pypdf 时无法拆分超限 PDF
    PdfReader = PdfWriter = None   # type: ignore[assignment]

from core.config import Config
from core.mineru_client import (
    MinerUClient,
    _AgentLimitError,
    _AuthError,
    _QuotaExceededError,
)
from core.security.path_target import OP_WRITE, PathTarget
from core.tool_limits import PDF2MD_RESULT_MAX_CHARS
from core.tools.base import UNTRUSTED_DATA_BEGIN, UNTRUSTED_DATA_END, Tool, ToolResult

logger = logging.getLogger(__name__)

# pypdf 读取不规范 PDF 时会刷大量 "Ignoring wrong pointing object" 警告，
# 这些告警不影响解析结果，降到 ERROR 级别以保持日志可读
logging.getLogger("pypdf").setLevel(logging.ERROR)

# Agent API limits (官方文档)
_AGENT_MAX_SIZE_MB = 10
_AGENT_MAX_PAGES = 20
_AGENT_SUPPORTED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".pptx", ".xlsx"}

# Precision API limits (官方文档)
_PRECISION_MAX_SIZE_MB = 200
_PRECISION_MAX_PAGES = 200
_PRECISION_SUPPORTED_EXT = {
    ".pdf", ".png", ".jpg", ".jpeg",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".html",
}

# model_version 选择 (官方文档: pipeline/vlm/MinerU-HTML)
# - vlm: 视觉语言模型，效果更好（商业 Precision API 默认）
# - pipeline: 传统流水线，速度快
# - MinerU-HTML: 专用于 HTML 文件
_MODEL_VERSION_DEFAULT = "vlm"
_MODEL_VERSION_HTML = "MinerU-HTML"

_DEFAULT_TIMEOUT_S = 7200      # 单次解析默认总超时（2 小时）
_MAX_TIMEOUT_S = 7200          # 参数上限

# Token 有效期提示 (官方文档: 90 天)
_TOKEN_VALIDITY_HINT = "API Key 有效期 90 天，请在 https://mineru.net/apiManage 续期"

# 免费额度提示
_FREE_QUOTA_HINT = (
    "Agent 轻量 API 免费无需密钥，但有 IP 限频（HTTP 429）。"
    "Precision API 需配置 system.mineru_api_key（每日有解析上限）。"
    "请在 setting.json 中配置。"
)


# pypdf 解析保护上限放宽：扫描版教材 PDF 的资源数组巨大，默认 10 万元素
# 会把完好文件误判为损坏（LimitReachedError）
_PDF_ARRAY_LIMIT = 10_000_000


def _pdf_read_limits(file_path: str) -> dict:
    """放宽 pypdf 解析保护上限（stream 按文件实际大小），供 apply_configuration 用。

    pypdf 6.x 移除了 ``array_maximum_element_count``；``apply_configuration``
    对未知字段直接 TypeError。这里按当前版本实际支持的字段
    构造参数，兼容新旧版本。
    """
    cfg = get_configuration()
    try:
        stream = max(cfg.maximum_declared_stream_length, os.path.getsize(file_path))
    except OSError:
        stream = cfg.maximum_declared_stream_length
    limits = {"maximum_declared_stream_length": stream}
    if "array_maximum_element_count" in vars(cfg):
        limits["array_maximum_element_count"] = _PDF_ARRAY_LIMIT
    return limits


def _pdf_page_count(file_path: str) -> int:
    """读取 PDF 页数；失败或无 pypdf 时返回 0（交给云端判断）"""
    if PdfReader is None:
        return 0
    try:
        with apply_configuration(**_pdf_read_limits(file_path)):
            return len(PdfReader(file_path).pages)
    except Exception as e:
        logger.warning(f"[pdf2markdown] 读取页数失败: {e}")
        return 0


def _rasterize_page_to_pdf(file_path: str, page_index: int,
                           out_path: Path, max_size_bytes: int) -> bool:
    """单页超限的兜底：把该页渲染成位图 PDF 并覆写 out_path。

    电子版教材可能单页内嵌超大资源（音视频/巨型图片），无法再拆页；
    内嵌媒体本来也无法转 markdown，视觉内容转 200dpi 位图足够云端解析。
    返回 True 表示已用栅格化结果覆写 out_path 且大小达标。
    """
    try:
        import pymupdf
    except ImportError:
        logger.warning("[pdf2markdown] 单页超限且 pymupdf 不可用")
        return False
    rasterized = out_path.with_suffix(".rasterized.pdf")
    try:
        doc = pymupdf.open(file_path)
        try:
            page = doc.load_page(page_index)
            rect = page.rect
            for dpi in (200, 150, 100):
                png = page.get_pixmap(dpi=dpi).tobytes("png")
                if len(png) > max_size_bytes:
                    continue
                tmp_doc = pymupdf.open()
                try:
                    new_page = tmp_doc.new_page(width=rect.width, height=rect.height)
                    new_page.insert_image(rect, stream=png)
                    tmp_doc.save(str(rasterized), garbage=4, deflate=True)
                finally:
                    tmp_doc.close()
                if rasterized.stat().st_size <= max_size_bytes:
                    os.replace(rasterized, out_path)
                    return True
            return False
        finally:
            doc.close()
    except Exception as e:
        logger.warning(f"[pdf2markdown] 单页栅格化失败: {e}")
        rasterized.unlink(missing_ok=True)
        return False


def _split_pdf_for_api(file_path: str, *, max_size_bytes: int,
                       max_pages: int, tmp_dir: str | None = None) -> tuple[list[Path], tempfile.TemporaryDirectory]:
    """按页数/大小把 PDF 拆成符合 Precision API 限制的分片（保持页序）"""
    if PdfReader is None or PdfWriter is None:
        raise RuntimeError("缺少 pypdf，无法拆分超限 PDF（请安装 pypdf）")
    with apply_configuration(**_pdf_read_limits(file_path)):
        reader = PdfReader(file_path)
        total = len(reader.pages)
        size = os.path.getsize(file_path)
        avg = size / max(total, 1)
        # 按平均页大小估算每组页数，留 15% 余量；再用实际大小递归校正
        pages_by_size = max(1, int(max_size_bytes * 0.85 / max(avg, 1)))
        chunk = max(1, min(max_pages, pages_by_size))
        if tmp_dir:
            os.makedirs(tmp_dir, exist_ok=True)
        tmp = tempfile.TemporaryDirectory(prefix="pdf2md_split_", dir=tmp_dir)
        try:
            parts: list[Path] = []

            def emit(start: int, end: int) -> None:
                writer = PdfWriter()
                for i in range(start, end):
                    writer.add_page(reader.pages[i])
                out = Path(tmp.name) / f"part_{len(parts) + 1:03d}.pdf"
                with open(out, "wb") as f:
                    writer.write(f)
                if out.stat().st_size > max_size_bytes and (end - start) > 1:
                    out.unlink()   # 实际大小仍超限：二分继续拆
                    mid = (start + end) // 2
                    emit(start, mid)
                    emit(mid, end)
                elif out.stat().st_size > max_size_bytes:
                    # 单页超限（页内嵌大资源，无法再拆）：栅格化兜底
                    if not _rasterize_page_to_pdf(file_path, start, out, max_size_bytes):
                        logger.warning("[pdf2markdown] 第 %d 页栅格化失败，"
                                       "该分片仍超限（可能被云端拒绝）", start + 1)
                    parts.append(out)
                else:
                    parts.append(out)

            for start in range(0, total, chunk):
                emit(start, min(start + chunk, total))
            if not parts:
                raise RuntimeError(f"PDF 拆分失败：共 {total} 页")
            return parts, tmp
        except Exception:
            tmp.cleanup()
            raise



class PDF2MarkdownTool(Tool):
    """Convert PDF/image files to Markdown via MinerU API."""

    streams_output = True
    mutates_workspace = True

    name = "pdf2markdown"
    description = (
        "Convert PDF, image, or office files to Markdown using MinerU OCR API. "
        "Supports PDF, PNG, JPG, DOCX, PPTX, XLSX, HTML. "
        "Two modes: Precision API (commercial, needs mineru_api_key, ≤200MB/≤200 pages, "
        "vlm model) and Agent API (free, no key, ≤10MB/≤20 pages). "
        "Oversized PDFs are split automatically to satisfy the active API's "
        "size/page limits (Precision: 200MB/200 pages, Agent: 10MB/20 pages) "
        "and the parts are merged into one Markdown file. "
        "If mineru_api_key is configured, only the Precision API is used "
        "(no free fallback; failures are reported as errors). "
        "Without a key, only the free Agent API is available. "
        "Output is saved as a .md file in the current working directory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to convert (PDF, image, DOCX, PPTX, XLSX, HTML).",
            },
            "timeout": {
                "type": "integer",
                "description": "Total wait time in seconds (default: 7200, max: 7200). Polling keeps waiting while the task is pending/running; the call fails with a timeout once this deadline is reached.",
            },
            "output_path": {
                "type": "string",
                "description": "Optional output .md file path. Defaults to <filename>.md in cwd.",
            },
            "model_version": {
                "type": "string",
                "description": "Model version: 'vlm' (accurate, default for Precision API), 'pipeline' (fast), 'MinerU-HTML' (for HTML). Only for Precision API.",
                "enum": ["pipeline", "vlm", "MinerU-HTML"],
            },
        },
        "required": ["file_path"],
    }

    MAX_TOOL_RESULT_SIZE_CHARS = PDF2MD_RESULT_MAX_CHARS

    def __init__(self, cwd: str = ".", workspace_uuid: str = "",
                 session=None, config: Config | None = None,
                 permission_store=None, permission_rules=None, permission_policy=None,
                 tmp_dir: str | None = None):
        super().__init__(
            cwd=cwd, workspace_uuid=workspace_uuid, session=session,
            permission_store=permission_store, permission_rules=permission_rules,
            permission_policy=permission_policy,
        )
        self._config = config
        # 超限 PDF 分割临时目录：默认 cwd 下的 .cili/tmp（与系统临时目录约定一致），外部调用可覆盖
        self._tmp_dir = tmp_dir or str(Path(cwd) / ".cili" / "tmp")
        # 实时进度（仅推前端/输出文件，不进 ToolResult/上下文）
        self._progress_offset = 0
        self._progress_lock = threading.Lock()
        self._client = MinerUClient(self._progress)

    def _progress(self, text: str) -> None:
        """推送一条实时进度：追加到 output_file（runner 注入）并触发 on_output。

        这些增量只走 UI 通道（事件总线/前端工具卡片），不进入 ToolResult，
        因此不会被写入会话上下文。未挂载输出通道时退化为服务端日志。
        """
        line = f"[{datetime.now():%H:%M:%S}] {text}\n"
        with self._progress_lock:
            wrote_file = False
            if self.output_file:
                try:
                    with open(self.output_file, "a", encoding="utf-8") as f:
                        f.write(line)
                        f.flush()
                    wrote_file = True
                except OSError:
                    pass
            # 文本模式写 Windows 下 \n -> \r\n（多 1 字节），补偿后与文件真实偏移一致
            self._progress_offset += len(line.encode("utf-8")) + line.count("\n")
            offset = self._progress_offset
        if not wrote_file:
            logger.info(f"[pdf2markdown] {text}")
        self._emit_output(line, offset)

    def _get_mineru_api_key(self) -> str:
        """Get MinerU API key from config or environment."""
        if self._config and self._config.system.mineru_api_key:
            return self._config.system.mineru_api_key
        return os.environ.get("MINERU_API_KEY", "")

    def execute(self, **kwargs: Any) -> ToolResult:
        file_path = kwargs.get("file_path", "")
        # 每次调用重置进度偏移（runner 会为新调用创建并清空输出文件）
        with self._progress_lock:
            self._progress_offset = 0
        # T14: timeout 安全转换 + clamp（非数字回退默认，范围 1-600）
        try:
            timeout = int(kwargs.get("timeout", _DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT_S
        timeout = max(1, min(timeout, _MAX_TIMEOUT_S))
        output_path = kwargs.get("output_path", "")
        model_version = kwargs.get("model_version", "")

        if not file_path:
            return ToolResult("Error: file_path is required", error=True)

        # Resolve file path
        escape = self._relative_escape_guard(file_path)
        if escape is not None:
            return escape
        abs_path = self._resolve_path(file_path)
        if not os.path.isfile(abs_path):
            return ToolResult(f"Error: file not found: {abs_path}", error=True)
        gate = self._read_gate([abs_path])
        if gate:
            return gate

        file_ext = Path(abs_path).suffix.lower()

        # Validate extension
        if file_ext not in _PRECISION_SUPPORTED_EXT:
            return ToolResult(
                f"Error: unsupported file type '{file_ext}'. "
                f"Supported: {', '.join(sorted(_PRECISION_SUPPORTED_EXT))}",
                error=True,
            )

        # Determine output path
        if not output_path:
            stem = Path(abs_path).stem
            output_path = os.path.join(self.cwd, f"{stem}.md")
        else:
            escape = self._relative_escape_guard(output_path)
            if escape is not None:
                return escape
            output_path = self._resolve_path(output_path)

        # 路径权限门：输出 .md 写入工作区外需审批/拒绝
        gate = self._path_gate([PathTarget(OP_WRITE, output_path, "output", resolved=output_path)])
        if gate:
            return gate

        # Determine model version (HTML files need MinerU-HTML)
        if file_ext == ".html":
            model_version = _MODEL_VERSION_HTML
        elif not model_version:
            model_version = _MODEL_VERSION_DEFAULT

        api_key = self._get_mineru_api_key()

        # 商业 API 优先：配置了 mineru_api_key 时走 Precision（vlm，超限自动分片），
        # 不做免费 Agent 兜底；未配置 key 时只能走免费 Agent API
        try:
            markdown = self.convert_file(
                abs_path, model_version=model_version, timeout=timeout)
        except _AuthError as e:
            return ToolResult(
                f"Precision API 认证失败: {e}\n"
                f"{_TOKEN_VALIDITY_HINT}，请更新 system.mineru_api_key 配置。",
                error=True,
            )
        except _QuotaExceededError as e:
            return ToolResult(
                f"每日解析任务数量已达上限: {e}\n"
                f"请明日再试，或检查 API Key 状态。",
                error=True,
            )
        except _AgentLimitError as e:
            return ToolResult(
                f"文件超出免费 Agent API 限制，且未配置 MinerU API Key，"
                f"无法使用 Precision API。\n{_FREE_QUOTA_HINT}\n原因: {e}",
                error=True,
            )
        except Exception as e:
            if api_key:
                return ToolResult(
                    f"Precision API 解析失败: {e}\n"
                    f"（已配置 mineru_api_key，不做免费 Agent 降级）",
                    error=True,
                )
            return ToolResult(
                f"Agent API 解析失败，且未配置 MinerU API Key，无法使用 Precision API。\n"
                f"{_FREE_QUOTA_HINT}\n原因: {e}",
                error=True,
            )

        self._save_markdown(markdown, output_path)
        api_mode = "Precision" if api_key else "Agent"
        return self._build_result(markdown, output_path, api_mode=api_mode)

    def convert_file(self, file_path: str, *, model_version: str = "",
                     timeout: int = _DEFAULT_TIMEOUT_S) -> str:
        """把文件转换为 markdown 文本（可复用入口：不做路径/输出门禁）。

        - 配置了 mineru_api_key → Precision API（默认 vlm）；PDF 超限自动分片后合并
        - 未配置 key → 免费 Agent API；PDF 超限（10MB/20页）同样自动分片后合并
        失败时抛出 _AuthError / _QuotaExceededError / _AgentLimitError 或普通异常。
        """
        abs_path = str(file_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"file not found: {abs_path}")
        ext = Path(abs_path).suffix.lower()
        if ext not in _PRECISION_SUPPORTED_EXT:
            raise ValueError(f"unsupported file type '{ext}'")
        if not model_version:
            model_version = _MODEL_VERSION_HTML if ext == ".html" else _MODEL_VERSION_DEFAULT

        size_mb = os.path.getsize(abs_path) / (1024 * 1024)
        api_key = self._get_mineru_api_key()
        mode = "Precision" if api_key else "Agent"
        self._progress(f"开始处理 {Path(abs_path).name}"
                       f"（{size_mb:.1f} MB，{mode} 模式，model={model_version}）")
        if api_key:
            md = self._precision_parse_with_split(
                abs_path, api_key, timeout, model_version)
        else:
            if ext not in _AGENT_SUPPORTED_EXT:
                raise _AgentLimitError(f"免费 Agent API 不支持 {ext} 类型")
            if ext != ".pdf" and size_mb > _AGENT_MAX_SIZE_MB:
                raise _AgentLimitError(
                    f"非 PDF 文件超出免费 Agent API 限制（≤{_AGENT_MAX_SIZE_MB}MB）")
            # PDF 超限（大小/页数）自动分片，逐片解析后合并
            md = self._agent_parse_with_split(abs_path, timeout)
        self._progress(f"转换完成：{len(md):,} 字符")
        return md

    def _agent_parse_with_split(self, file_path: str, timeout: int) -> str:
        """免费 Agent API：超限（10MB/20页）自动分片后合并"""
        return self._parse_with_split(
            file_path,
            lambda p: self._client._agent_parse(p, timeout),
            max_size_mb=_AGENT_MAX_SIZE_MB,
            max_pages=_AGENT_MAX_PAGES,
        )

    def _build_result(self, markdown: str, output_path: str, api_mode: str) -> ToolResult:
        """Build successful result with metadata."""
        char_count = len(markdown)
        line_count = markdown.count('\n') + 1
        preview = markdown[:2000]
        if char_count > 2000:
            preview += f"\n\n... (共 {char_count:,} 字符，{line_count:,} 行)"

        output = (
            f"[{api_mode} API] 解析完成\n"
            f"文件: {Path(output_path).name}\n"
            f"输出: {output_path}\n"
            f"字符数: {char_count:,}\n"
            f"行数: {line_count:,}\n\n"
            f"--- Markdown 预览 ---\n{UNTRUSTED_DATA_BEGIN}{preview}{UNTRUSTED_DATA_END}"
        )
        meta = {"output_path": output_path, "api_mode": api_mode}
        return ToolResult(output, meta=meta)

    def _save_markdown(self, content: str, output_path: str) -> None:
        """Save markdown content to file."""
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

    def _parse_with_split(self, file_path: str, parse_fn: Callable[[str], str], *,
                          max_size_mb: int, max_pages: int) -> str:
        """按限额解析：超限 PDF 先分片（pypdf），逐片解析后按页序合并"""
        ext = Path(file_path).suffix.lower()
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        pages = _pdf_page_count(file_path) if ext == ".pdf" else 0
        # 服务端按十进制 MB 校验（200MB = 200e6 字节），本地若按 MiB 计算会留出
        # (200e6, 209.7e6] 字节窗口——落在窗口内的文件/分片会被服务端拒绝，
        # 故统一乘 0.9 留余量，直接提交与分片预算共用同一阈值
        safe_bytes = int(max_size_mb * 1e6 * 0.9)
        size = os.path.getsize(file_path)
        if ext != ".pdf" or (size <= safe_bytes and pages <= max_pages):
            return parse_fn(file_path)

        parts, tmp = _split_pdf_for_api(
            file_path,
            max_size_bytes=safe_bytes,
            max_pages=max_pages,
            tmp_dir=self._tmp_dir,
        )
        self._progress(f"文件超限（{size_mb:.0f}MB/{pages}页，"
                       f"限额 {max_size_mb}MB/{max_pages}页），拆分为 {len(parts)} 片")
        try:
            mds = []
            for i, part in enumerate(parts, 1):
                self._progress(f"[分片 {i}/{len(parts)}] 开始解析 {part.name}"
                               f"（{part.stat().st_size / (1024 * 1024):.1f} MB）")
                mds.append(parse_fn(str(part)))
                self._progress(f"[分片 {i}/{len(parts)}] 完成")
            return "\n\n".join(m for m in mds if m.strip())
        finally:
            tmp.cleanup()

    def _precision_parse_with_split(self, file_path: str, api_key: str,
                                    timeout: int, model_version: str) -> str:
        """Precision API：超限（200MB/200页）自动分片后合并"""
        return self._parse_with_split(
            file_path,
            lambda p: self._client._precision_parse(p, api_key, timeout, model_version),
            max_size_mb=_PRECISION_MAX_SIZE_MB,
            max_pages=_PRECISION_MAX_PAGES,
        )




