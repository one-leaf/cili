"""PDF/Image to Markdown conversion via MinerU API.

Two API modes with automatic fallback:
1. Agent API (free, no key) — ≤10MB, ≤20 pages, Markdown only
2. Precision Parse API (needs API key) — ≤200MB, ≤200 pages, ZIP output

Fallback order: Agent → Precision (when file exceeds Agent limits)

Official docs: https://mineru.net/apiManage/docs
"""

from __future__ import annotations

import io
import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Any

import requests

from core.config import Config
from core.tools.shared.base import Tool, ToolResult

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
# - pipeline: 默认，传统流水线，速度快
# - vlm: 视觉语言模型，更准确但更慢
# - MinerU-HTML: 专用于 HTML 文件
_MODEL_VERSION_DEFAULT = "pipeline"
_MODEL_VERSION_HTML = "MinerU-HTML"

# Token 有效期提示 (官方文档: 90 天)
_TOKEN_VALIDITY_HINT = "API Key 有效期 90 天，请在 https://mineru.net/apiManage 续期"

# 免费额度提示
_FREE_QUOTA_HINT = (
    "Agent 轻量 API 免费无需密钥，但有 IP 限频（HTTP 429）。"
    "Precision API 需配置 system.mineru_api_key（每日有解析上限）。"
    "请在 setting.json 中配置。"
)


class PDF2MarkdownTool(Tool):
    """Convert PDF/image files to Markdown via MinerU API."""

    name = "pdf2markdown"
    description = (
        "Convert PDF, image, or office files to Markdown using MinerU OCR API. "
        "Supports PDF, PNG, JPG, DOCX, PPTX, XLSX, HTML. "
        "Two modes: Agent API (free, no key, ≤10MB/≤20 pages) and "
        "Precision API (needs mineru_api_key, ≤200MB/≤200 pages). "
        "Automatically tries Agent API first, falls back to Precision API if needed. "
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
                "description": "Model version: 'pipeline' (fast, default), 'vlm' (accurate), 'MinerU-HTML' (for HTML). Only for Precision API.",
                "enum": ["pipeline", "vlm", "MinerU-HTML"],
            },
        },
        "required": ["file_path"],
    }

    MAX_TOOL_RESULT_SIZE_CHARS = 50_000

    def __init__(self, cwd: str = ".", workspace_uuid: str = "",
                 session_manager=None, config: Config | None = None):
        super().__init__(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager)
        self._config = config

    def _get_mineru_api_key(self) -> str:
        """Get MinerU API key from config or environment."""
        if self._config and self._config.system.mineru_api_key:
            return self._config.system.mineru_api_key
        return os.environ.get("MINERU_API_KEY", "")

    def execute(self, **kwargs: Any) -> ToolResult:
        file_path = kwargs.get("file_path", "")
        timeout = min(int(kwargs.get("timeout", 300)), 600)
        output_path = kwargs.get("output_path", "")
        model_version = kwargs.get("model_version", "")

        if not file_path:
            return ToolResult("Error: file_path is required", error=True)

        # Resolve file path
        abs_path = self._resolve_path(file_path)
        if not os.path.isfile(abs_path):
            return ToolResult(f"Error: file not found: {abs_path}", error=True)

        file_ext = Path(abs_path).suffix.lower()
        file_size_mb = os.path.getsize(abs_path) / (1024 * 1024)

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

        # Determine model version (HTML files need MinerU-HTML)
        if file_ext == ".html":
            model_version = _MODEL_VERSION_HTML
        elif not model_version:
            model_version = _MODEL_VERSION_DEFAULT

        api_key = self._get_mineru_api_key()

        # Decide whether Agent API can handle this file
        agent_eligible = (
            file_size_mb <= _AGENT_MAX_SIZE_MB
            and file_ext in _AGENT_SUPPORTED_EXT
        )

        # Try Agent API first if eligible
        if agent_eligible:
            try:
                markdown = self._agent_parse(abs_path, timeout)
                self._save_markdown(markdown, output_path)
                return self._build_result(markdown, output_path, api_mode="Agent")
            except _AgentLimitError as e:
                # File exceeds Agent limits, fall through to Precision
                logger.info(f"Agent API limit exceeded: {e}, falling back to Precision API")
            except Exception as e:
                logger.warning(f"Agent API failed: {e}, falling back to Precision API")

        # Fall back to Precision API
        if not api_key:
            return ToolResult(
                f"Agent API 解析失败，且未配置 MinerU API Key，无法使用 Precision API。\n"
                f"{_FREE_QUOTA_HINT}",
                error=True,
            )

        try:
            markdown = self._precision_parse(abs_path, api_key, timeout, model_version)
            self._save_markdown(markdown, output_path)
            return self._build_result(markdown, output_path, api_mode="Precision")
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
        except Exception as e:
            return ToolResult(
                f"解析失败: {e}\n{_FREE_QUOTA_HINT}",
                error=True,
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
            f"--- Markdown 预览 ---\n{preview}"
        )
        return ToolResult(output, meta={"output_path": output_path, "api_mode": api_mode})

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

        # Step 1: Get upload URLs
        resp = requests.post(
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
            resp = requests.get(url, headers=headers, timeout=30)
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
        old_no_proxy = os.environ.get("no_proxy", "")
        os.environ["no_proxy"] = "*"
        try:
            resp = requests.get(zip_url, timeout=(15, 120))
            resp.raise_for_status()
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
            # Fallback: try with verify=False
            resp = requests.get(zip_url, timeout=(15, 120), verify=False)
            resp.raise_for_status()
        finally:
            os.environ["no_proxy"] = old_no_proxy

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
        """Download markdown content from URL with SSL fallback.

        Agent API 返回的 CDN 链接可能因 SSL 代理问题失败
        """
        old_no_proxy = os.environ.get("no_proxy", "")
        os.environ["no_proxy"] = "*"
        try:
            resp = requests.get(url, timeout=(15, 60))
            resp.raise_for_status()
            return resp.text
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
            resp = requests.get(url, timeout=(15, 60), verify=False)
            resp.raise_for_status()
            return resp.text
        finally:
            os.environ["no_proxy"] = old_no_proxy


class _AgentLimitError(Exception):
    """Raised when Agent API limits are exceeded, triggering fallback."""
    pass


class _AuthError(Exception):
    """Raised when API key authentication fails."""
    pass


class _QuotaExceededError(Exception):
    """Raised when daily quota is exceeded."""
    pass
