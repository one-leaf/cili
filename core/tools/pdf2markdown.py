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

``convert_file()`` is the reusable entry point: it returns markdown text and
lets exceptions propagate (used by external scripts such as the textbook batch
conversion, so there is only one implementation).

Official docs: https://mineru.net/apiManage/docs
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

import requests

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:   # 未安装 pypdf 时无法拆分超限 PDF
    PdfReader = PdfWriter = None   # type: ignore[assignment]

from core.config import Config
from core.security.path_policy import OP_WRITE, PathTarget
from core.tools.base import UNTRUSTED_DATA_BEGIN, UNTRUSTED_DATA_END, Tool, ToolResult

logger = logging.getLogger(__name__)

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

# Token 有效期提示 (官方文档: 90 天)
_TOKEN_VALIDITY_HINT = "API Key 有效期 90 天，请在 https://mineru.net/apiManage 续期"

# 免费额度提示
_FREE_QUOTA_HINT = (
    "Agent 轻量 API 免费无需密钥，但有 IP 限频（HTTP 429）。"
    "Precision API 需配置 system.mineru_api_key（每日有解析上限）。"
    "请在 setting.json 中配置。"
)


def _pdf_page_count(file_path: str) -> int:
    """读取 PDF 页数；失败或无 pypdf 时返回 0（交给云端判断）"""
    if PdfReader is None:
        return 0
    try:
        return len(PdfReader(file_path).pages)
    except Exception as e:
        logger.warning(f"[pdf2markdown] 读取页数失败: {e}")
        return 0


def _split_pdf_for_api(file_path: str, *, max_size_bytes: int,
                       max_pages: int) -> tuple[list[Path], tempfile.TemporaryDirectory]:
    """按页数/大小把 PDF 拆成符合 Precision API 限制的分片（保持页序）"""
    if PdfReader is None or PdfWriter is None:
        raise RuntimeError("缺少 pypdf，无法拆分超限 PDF（请安装 pypdf）")
    reader = PdfReader(file_path)
    total = len(reader.pages)
    size = os.path.getsize(file_path)
    avg = size / max(total, 1)
    # 按平均页大小估算每组页数，留 15% 余量；再用实际大小递归校正
    pages_by_size = max(1, int(max_size_bytes * 0.85 / max(avg, 1)))
    chunk = max(1, min(max_pages, pages_by_size))
    tmp = tempfile.TemporaryDirectory(prefix="pdf2md_split_")
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
        else:
            parts.append(out)

    for start in range(0, total, chunk):
        emit(start, min(start + chunk, total))
    if not parts:
        raise RuntimeError(f"PDF 拆分失败：共 {total} 页")
    return parts, tmp


def _request_with_retry(method: str, url: str, *, attempts: int = 3,
                        backoff: float = 5.0, **kwargs):
    """连接类错误（超时/DNS/连接重置）自动重试；HTTP 状态错误不重试"""
    func = requests.post if method.upper() == "POST" else requests.get
    last = None
    for i in range(attempts):
        try:
            return func(url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last = e
            if i < attempts - 1:
                logger.warning(
                    f"[pdf2markdown] {method} {url} 网络错误，{backoff:.0f}s 后重试 "
                    f"({i + 1}/{attempts}): {e}")
                time.sleep(backoff)
    raise last


class PDF2MarkdownTool(Tool):
    """Convert PDF/image files to Markdown via MinerU API."""

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
                "description": "Max wait time in seconds for parsing (default: 300, max: 600).",
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

    MAX_TOOL_RESULT_SIZE_CHARS = 50_000

    def __init__(self, cwd: str = ".", workspace_uuid: str = "",
                 session=None, config: Config | None = None,
                 permission_store=None, permission_rules=None, permission_policy=None,
                 approval_store=None, approval_rules=None):
        super().__init__(
            cwd=cwd, workspace_uuid=workspace_uuid, session=session,
            permission_store=permission_store, permission_rules=permission_rules,
            permission_policy=permission_policy,
            approval_store=approval_store, approval_rules=approval_rules,
        )
        self._config = config

    def _get_mineru_api_key(self) -> str:
        """Get MinerU API key from config or environment."""
        if self._config and self._config.system.mineru_api_key:
            return self._config.system.mineru_api_key
        return os.environ.get("MINERU_API_KEY", "")

    def execute(self, **kwargs: Any) -> ToolResult:
        file_path = kwargs.get("file_path", "")
        # T14: timeout 安全转换 + clamp（非数字回退默认，范围 1-600）
        try:
            timeout = int(kwargs.get("timeout", 300))
        except (TypeError, ValueError):
            timeout = 300
        timeout = max(1, min(timeout, 600))
        output_path = kwargs.get("output_path", "")
        model_version = kwargs.get("model_version", "")

        if not file_path:
            return ToolResult("Error: file_path is required", error=True)

        # Resolve file path
        abs_path = self._resolve_path(file_path, read_only=True)
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
                     timeout: int = 300) -> str:
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

        api_key = self._get_mineru_api_key()
        if api_key:
            return self._precision_parse_with_split(
                abs_path, api_key, timeout, model_version)

        if ext not in _AGENT_SUPPORTED_EXT:
            raise _AgentLimitError(f"免费 Agent API 不支持 {ext} 类型")
        size_mb = os.path.getsize(abs_path) / (1024 * 1024)
        if ext != ".pdf" and size_mb > _AGENT_MAX_SIZE_MB:
            raise _AgentLimitError(
                f"非 PDF 文件超出免费 Agent API 限制（≤{_AGENT_MAX_SIZE_MB}MB）")
        # PDF 超限（大小/页数）自动分片，逐片解析后合并
        return self._agent_parse_with_split(abs_path, timeout)

    def _parse_with_split(self, file_path: str, parse_fn: Callable[[str], str], *,
                          max_size_mb: int, max_pages: int) -> str:
        """按限额解析：超限 PDF 先分片（pypdf），逐片解析后按页序合并"""
        ext = Path(file_path).suffix.lower()
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        pages = _pdf_page_count(file_path) if ext == ".pdf" else 0
        if ext != ".pdf" or (size_mb <= max_size_mb and pages <= max_pages):
            return parse_fn(file_path)

        parts, tmp = _split_pdf_for_api(
            file_path,
            max_size_bytes=max_size_mb * 1024 * 1024,
            max_pages=max_pages,
        )
        logger.info(f"[pdf2markdown] 文件超限（{size_mb:.0f}MB/{pages}页，"
                    f"限额 {max_size_mb}MB/{max_pages}页），拆分为 {len(parts)} 片")
        try:
            mds = []
            for i, part in enumerate(parts, 1):
                logger.info(f"[pdf2markdown] 分片 {i}/{len(parts)}: {part.name} "
                            f"({part.stat().st_size / (1024 * 1024):.1f} MB)")
                mds.append(parse_fn(str(part)))
            return "\n\n".join(m for m in mds if m.strip())
        finally:
            tmp.cleanup()

    def _precision_parse_with_split(self, file_path: str, api_key: str,
                                    timeout: int, model_version: str) -> str:
        """Precision API：超限（200MB/200页）自动分片后合并"""
        return self._parse_with_split(
            file_path,
            lambda p: self._precision_parse(p, api_key, timeout, model_version),
            max_size_mb=_PRECISION_MAX_SIZE_MB,
            max_pages=_PRECISION_MAX_PAGES,
        )

    def _agent_parse_with_split(self, file_path: str, timeout: int) -> str:
        """免费 Agent API：超限（10MB/20页）自动分片后合并"""
        return self._parse_with_split(
            file_path,
            lambda p: self._agent_parse(p, timeout),
            max_size_mb=_AGENT_MAX_SIZE_MB,
            max_pages=_AGENT_MAX_PAGES,
        )

    def _build_result(self, markdown: str, output_path: str, api_mode: str,
                      fallback_reason: str = "") -> ToolResult:
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
        if fallback_reason:
            output += f"\n\n[降级] Precision API 失败，已回退免费 Agent API。原因: {fallback_reason}"
            meta["fallback_reason"] = fallback_reason
        return ToolResult(output, meta=meta)

    def _save_markdown(self, content: str, output_path: str) -> None:
        """Save markdown content to file."""
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

    # ── Agent 轻量解析 API ─────────────────────────────────────────────────────

    def _agent_parse(self, file_path: str, timeout: int) -> str:
        """Parse file using Agent lightweight API (no key required).

        Agent API: POST /api/v1/agent/parse/file (获取上传URL)
                   POST /api/v1/agent/parse/url (URL解析)
                   GET /api/v1/agent/parse/{task_id} (查询结果)

        无需 Token，IP 限频（HTTP 429）。
        限制: ≤10MB, ≤20页, 支持 PDF/图片/Docx/PPTx/Xlsx
        """
        file_name = Path(file_path).name
        logger.info(f"[Agent API] Parsing: {file_name}")

        # Step 1: Get upload URL
        resp = requests.post(
            "https://mineru.net/api/v1/agent/parse/file",
            json={"file_name": file_name},
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()

        if result.get("code") != 0:
            code = result.get("code")
            msg = result.get("msg", "unknown")
            # Agent 专属错误码 (官方文档)
            if code == -30001:
                raise _AgentLimitError(f"文件大小超出轻量接口限制 (10MB): {msg}")
            elif code == -30002:
                raise _AgentLimitError(f"轻量接口不支持该文件类型: {msg}")
            elif code == -30003:
                raise _AgentLimitError(f"文件页数超出轻量接口限制 (20页): {msg}")
            elif code == -30004:
                raise Exception(f"请求参数错误: {msg}")
            elif code == 429:
                raise _AgentLimitError(f"IP 限频，请稍后再试: {msg}")
            raise Exception(f"Agent API error (code={code}): {msg}")

        data = result["data"]
        task_id = data["task_id"]
        file_url = data["file_url"]

        # Step 2: Upload file (官方文档说 URL 有效期 24 小时)
        with open(file_path, "rb") as f:
            resp = requests.put(file_url, data=f, timeout=(10, 120))
            if resp.status_code not in (200, 201, 204):
                raise Exception(f"文件上传失败: HTTP {resp.status_code}")

        # Step 3: Poll for result
        result_data = self._agent_poll(task_id, timeout)

        # Step 4: Download markdown
        markdown_url = result_data.get("markdown_url")
        if not markdown_url:
            raise Exception("Agent API 返回结果中无 markdown_url")

        return self._download_markdown(markdown_url)

    def _agent_poll(self, task_id: str, timeout: int) -> dict:
        """Poll Agent API for parsing result.

        状态值: waiting-file, uploading, pending, running, done, failed
        """
        url = f"https://mineru.net/api/v1/agent/parse/{task_id}"
        start_time = time.time()
        poll_interval = 3

        while time.time() - start_time < timeout:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            result = resp.json()

            if result.get("code") != 0:
                raise Exception(f"Agent poll error (code={result.get('code')}): {result.get('msg')}")

            data = result.get("data", {})
            state = data.get("state")

            if state == "done":
                return data
            elif state == "failed":
                err_code = data.get("err_code")
                err_msg = data.get("err_msg", "unknown")
                if err_code in (-30001, -30002, -30003):
                    raise _AgentLimitError(f"code={err_code}: {err_msg}")
                raise Exception(f"Agent parse failed: {err_msg}")
            # 其他状态: waiting-file, uploading, pending, running - 继续轮询

            time.sleep(poll_interval)

        raise TimeoutError(f"Agent API timeout ({timeout}s)")

    # ── Precision Parse API v4 ─────────────────────────────────────────────────

    def _precision_parse(
        self, file_path: str, api_key: str, timeout: int, model_version: str
    ) -> str:
        """Parse file using Precision Parse API (needs API key).

        Precision API: POST /api/v4/file-urls/batch (批量申请上传URL)
                       GET /api/v4/extract-results/batch/{batch_id} (获取结果)

        需要 Bearer Token (有效期 90 天)
        限制: ≤200MB, ≤200页, 支持 PDF/图片/Doc/Docx/PPT/PPTx/XLS/XLSX/HTML
        """
        file_name = Path(file_path).name
        logger.info(f"[Precision API] Parsing: {file_name}, model={model_version}")

        # 构建请求体 (官方文档参数)
        request_body = {
            "files": [{"name": file_name, "data_id": Path(file_path).stem}],
            "model_version": model_version,
            # 官方文档默认值
            "enable_formula": True,   # 公式识别 (pipeline/vlm 有效)
            "enable_table": True,     # 表格识别 (pipeline/vlm 有效)
            "is_ocr": False,          # OCR (pipeline/vlm 有效)
        }

        # Step 1: Get upload URLs（连接类错误自动重试）
        resp = _request_with_retry(
            "POST",
            "https://mineru.net/api/v4/file-urls/batch",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json=request_body,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()

        if result.get("code") != 0:
            code = result.get("code")
            msg = result.get("msg", "unknown")
            self._handle_precision_error(code, msg)

        data = result["data"]
        batch_id = data["batch_id"]
        file_urls = data["file_urls"]

        # Step 2: Upload file to OSS (with retries)
        self._upload_to_oss(file_urls[0], file_path)

        # Step 3: Poll for result
        result_data = self._precision_poll(batch_id, api_key, timeout)

        # Step 4: Extract markdown from ZIP
        extract_result = result_data.get("extract_result", [])
        if not extract_result:
            raise Exception("Precision API 返回结果中无 extract_result")

        zip_url = extract_result[0].get("full_zip_url")
        if not zip_url:
            err_msg = extract_result[0].get("err_msg", "")
            if err_msg:
                raise Exception(f"解析结果无 full_zip_url: {err_msg}")
            raise Exception("解析结果无 full_zip_url")

        return self._extract_markdown_from_zip(zip_url)

    def _handle_precision_error(self, code: Any, msg: str) -> None:
        """Handle Precision API error codes (官方文档)."""
        # Token 认证错误
        if str(code) in ("A0202", "A0211") or "authenticate failed" in msg.lower():
            raise _AuthError(f"code={code}: {msg}")

        # 每日限额
        if code == -60018:
            raise _QuotaExceededError(msg)

        # 文件相关错误
        error_map = {
            -500: "传参错误",
            -10001: "服务异常",
            -10002: "请求参数错误",
            -60001: "生成上传 URL 失败",
            -60002: "获取匹配的文件格式失败",
            -60003: "文件读取失败",
            -60004: "空文件",
            -60005: "文件大小超出限制 (200MB)",
            -60006: "文件页数超过限制 (200页)",
            -60007: "模型服务暂时不可用",
            -60008: "文件读取超时",
            -60009: "任务提交队列已满",
            -60010: "解析失败",
            -60011: "获取有效文件失败",
            -60012: "找不到任务",
            -60013: "没有权限访问该任务",
            -60014: "运行中的任务暂不支持删除",
            -60015: "文件转换失败",
            -60016: "文件转换失败",
            -60017: "重试次数达到上限",
            -60018: "每日解析任务数量已达上限",
            -60019: "HTML文件解析额度不足",
            -60020: "文件拆分失败",
            -60021: "读取文件页数失败",
            -60022: "网页读取失败",
        }

        hint = error_map.get(code, "")
        raise Exception(f"Precision API error (code={code}): {hint} - {msg}")

    def _upload_to_oss(self, oss_url: str, file_path: str) -> None:
        """Upload file to OSS with retries.

        官方文档: URL 有效期 24 小时
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with open(file_path, "rb") as f:
                    resp = requests.put(oss_url, data=f, timeout=(10, 120))
                    if resp.status_code not in (200, 201, 204):
                        raise Exception(f"HTTP {resp.status_code}")
                return
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(3 * (attempt + 1))
                else:
                    raise Exception(f"Upload failed after {max_retries} attempts: {e}")

    def _precision_poll(self, batch_id: str, api_key: str, timeout: int) -> dict:
        """Poll Precision API for parsing result.

        状态值: pending, running, done, failed
        """
        url = f"https://mineru.net/api/v4/extract-results/batch/{batch_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        start_time = time.time()
        poll_interval = 3

        while time.time() - start_time < timeout:
            resp = _request_with_retry("GET", url, headers=headers, timeout=30)
            resp.raise_for_status()
            result = resp.json()

            if result.get("code") != 0:
                code = result.get("code")
                msg = result.get("msg", "unknown")
                self._handle_precision_error(code, msg)

            extract_results = result["data"]["extract_result"]

            all_done = True
            for item in extract_results:
                state = item.get("state")
                if state == "failed":
                    err_code = item.get("err_code")
                    err_msg = item.get("err_msg", "unknown")
                    if err_code == -60018:
                        raise _QuotaExceededError(err_msg)
                    raise Exception(f"Precision parse failed (code={err_code}): {err_msg}")
                elif state != "done":
                    all_done = False

            if all_done:
                return result["data"]

            time.sleep(poll_interval)

        raise TimeoutError(f"Precision API timeout ({timeout}s)")

    def _extract_markdown_from_zip(self, zip_url: str) -> str:
        """Download ZIP and extract full.md content.

        官方文档: full_zip_url 有效期 24 小时
        ZIP 包内包含 full.md, 以及 _model.json (模型推理结果) 等文件
        """
        # Download ZIP (bypass proxy for SSL issues)
        # 用 per-request 代理设置而非改 os.environ["no_proxy"]：全局环境变量
        # 在并发场景下会让其他线程的请求（包括 LLM 调用）也绕过代理
        no_proxy = {"http": None, "https": None}
        try:
            resp = requests.get(zip_url, timeout=(15, 120), proxies=no_proxy)
            resp.raise_for_status()
        except requests.exceptions.SSLError as e:
            # 不回退到 verify=False：关闭证书校验会让下载的 markdown 可被中间人篡改
            raise RuntimeError(f"SSL 校验失败，请检查系统证书或代理配置: {e}") from e

        # Extract full.md from ZIP
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            # Look for full.md in the archive
            md_files = [n for n in zf.namelist() if n.endswith(".md")]
            full_md = next((n for n in md_files if "full.md" in n), None)

            if full_md:
                return zf.read(full_md).decode("utf-8")
            elif md_files:
                # Fallback: use first .md file
                return zf.read(md_files[0]).decode("utf-8")
            else:
                raise Exception(f"ZIP 中无 .md 文件，文件列表: {zf.namelist()}")

    def _download_markdown(self, url: str) -> str:
        """Download markdown content from URL.

        Agent API 返回的 CDN 链接可能因 SSL 代理问题失败；失败时直接报错，
        不做 verify=False 回退（否则下载内容可被中间人篡改）。
        """
        no_proxy = {"http": None, "https": None}
        try:
            resp = requests.get(url, timeout=(15, 60), proxies=no_proxy)
            resp.raise_for_status()
        except requests.exceptions.SSLError as e:
            raise RuntimeError(f"SSL 校验失败，请检查系统证书或代理配置: {e}") from e
        # T11: 显式 UTF-8 解码，避免 requests 按 ISO-8859-1 回退导致中文乱码
        return resp.content.decode("utf-8")


class _AgentLimitError(Exception):
    """Raised when Agent API limits are exceeded, triggering fallback."""
    pass


class _AuthError(Exception):
    """Raised when API key authentication fails."""
    pass


class _QuotaExceededError(Exception):
    """Raised when daily quota is exceeded."""
    pass
