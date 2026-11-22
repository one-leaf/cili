"""MinerU API 客户端：Agent / Precision 两种解析模式、轮询与结果下载。

同步实现（工具层本身是同步的）；进度通过构造时注入的 ``progress`` 回调推送。
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 5
_POLL_MAX_CONSECUTIVE_ERRORS = 12
_POLL_HEARTBEAT_EVERY = 12
_SUBMIT_RATE_PER_MIN = 40


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


class _RateLimiter:
    """线程安全的等间隔限流器：每分钟最多 rate_per_min 次"""

    def __init__(self, rate_per_min: int) -> None:
        self._lock = threading.Lock()
        self._interval = 60.0 / max(rate_per_min, 1)
        self._next_at = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                logger.info(f"[pdf2markdown] 提交限流等待 {wait:.1f}s"
                            f"（上限 {_SUBMIT_RATE_PER_MIN} 文件/分钟）")
                time.sleep(wait)
                now = time.monotonic()
                self._next_at = max(now, self._next_at) + self._interval
            else:
                self._next_at = now + self._interval


_submit_limiter = _RateLimiter(_SUBMIT_RATE_PER_MIN)

class _AgentLimitError(Exception):
    """Raised when Agent API limits are exceeded, triggering fallback."""
    pass


class _AuthError(Exception):
    """Raised when API key authentication fails."""
    pass


class _QuotaExceededError(Exception):
    """Raised when daily quota is exceeded."""
    pass

class MinerUClient:
    """MinerU API 客户端；progress(text) 用于推送实时进度。"""

    def __init__(self, progress: Callable[[str], None]):
        self._progress = progress

    def _upload_file(self, url: str, file_path: str, *, max_retries: int = 3) -> None:
        """上传文件（连接类错误最多重试 3 次）。"""
        last: Exception | None = None
        for attempt in range(max_retries):
            try:
                with open(file_path, "rb") as f:
                    resp = requests.put(url, data=f, timeout=(10, 120))
                if resp.status_code in (200, 201, 204):
                    return
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise Exception(f"文件上传失败: HTTP {resp.status_code}")
                last = Exception(f"文件上传失败: HTTP {resp.status_code}")
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last = e
            if attempt < max_retries - 1:
                time.sleep(3 * (attempt + 1))
        raise Exception(f"文件上传失败（重试 {max_retries} 次）: {last}")

    # ── Agent 轻量解析 API ─────────────────────────────────────────────────────

    def _agent_parse(self, file_path: str, timeout: int) -> str:
        """用免费 Agent 轻量 API 解析文件（无需密钥）。

        Agent API: POST /api/v1/agent/parse/file（获取上传 URL）
                   POST /api/v1/agent/parse/url（URL 解析）
                   GET /api/v1/agent/parse/{task_id}（查询结果）

        无需 Token，IP 限频（HTTP 429）。
        限制: ≤10MB, ≤20页, 支持 PDF/图片/Docx/PPTx/Xlsx
        """
        file_name = Path(file_path).name
        logger.info(f"[Agent API] Parsing: {file_name}")
        self._progress(f"申请上传 URL（Agent）：{file_name}")

        # Step 1: Get upload URL
        resp = requests.post(
            "https://mineru.net/api/v1/agent/parse/file",
            json={"file_name": file_name},
            timeout=30,
        )
        if resp.status_code == 429:
            raise _AgentLimitError("IP 限频，请稍后再试")
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

        data = result.get("data") or {}
        task_id = data["task_id"]
        file_url = data["file_url"]

        # Step 2: Upload file (官方文档说 URL 有效期 24 小时)
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        self._progress(f"上传中（{size_mb:.1f} MB）...")
        self._upload_file(file_url, file_path)
        self._progress("上传完成，开始轮询解析结果")

        # Step 3: Poll for result
        result_data = self._agent_poll(task_id, timeout)

        # Step 4: Download markdown
        markdown_url = result_data.get("markdown_url")
        if not markdown_url:
            raise Exception("Agent API 返回结果中无 markdown_url")

        return self._download_markdown(markdown_url)

    def _poll_until_done(self, label: str, url: str, timeout: int, parse,
                         headers: dict | None = None) -> dict:
        """通用轮询：``parse(result) -> (done_payload | None, state)``。

        非终态持续等待，但受总超时与连续网络错误双重约束。
        """
        start_time = time.monotonic()
        deadline = start_time + timeout
        consecutive_errors = 0
        polls = 0

        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"{label} API 轮询超时（{timeout}s），任务可能已丢失")
            polls += 1
            try:
                resp = requests.get(url, headers=headers, timeout=30)
                resp.raise_for_status()
                result = resp.json()
                consecutive_errors = 0
            except requests.exceptions.RequestException as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                retryable = (
                    isinstance(e, (requests.exceptions.ConnectionError,
                                   requests.exceptions.Timeout))
                    or status == 429
                    or (status is not None and status >= 500)
                )
                if not retryable:
                    raise
                consecutive_errors += 1
                if consecutive_errors >= _POLL_MAX_CONSECUTIVE_ERRORS:
                    raise TimeoutError(
                        f"{label} API 连续 {consecutive_errors} 次网络错误: {e}")
                logger.warning(
                    f"[pdf2markdown] {label} 轮询网络错误，{_POLL_INTERVAL_S}s 后重试 "
                    f"({consecutive_errors}/{_POLL_MAX_CONSECUTIVE_ERRORS}): {e}")
                time.sleep(_POLL_INTERVAL_S)
                continue

            done_payload, state = parse(result)
            if done_payload is not None:
                return done_payload

            if polls % _POLL_HEARTBEAT_EVERY == 0:
                self._progress(f"解析中 state={state}，"
                               f"已等待 {time.monotonic() - start_time:.0f}s")
            time.sleep(_POLL_INTERVAL_S)

    def _agent_poll(self, task_id: str, timeout: int) -> dict:
        """轮询 Agent 结果，直到 done/failed。"""
        url = f"https://mineru.net/api/v1/agent/parse/{task_id}"

        def parse(result: dict) -> tuple[dict | None, str | None]:
            if result.get("code") != 0:
                raise Exception(
                    f"Agent poll error (code={result.get('code')}): {result.get('msg')}"
                )
            data = result.get("data") or {}
            state = data.get("state")
            if state == "done":
                return data, state
            if state == "failed":
                err_code = data.get("err_code")
                err_msg = data.get("err_msg", "unknown")
                if err_code in (-30001, -30002, -30003):
                    raise _AgentLimitError(f"code={err_code}: {err_msg}")
                raise Exception(f"Agent parse failed: {err_msg}")
            # waiting-file / uploading / pending / running：继续轮询
            return None, state

        return self._poll_until_done("Agent", url, timeout, parse)

    # ── Precision Parse API v4 ─────────────────────────────────────────────────

    def _precision_parse(
        self, file_path: str, api_key: str, timeout: int, model_version: str
    ) -> str:
        """用 Precision API 解析文件（需要 API Key）。

        Precision API: POST /api/v4/file-urls/batch（批量申请上传 URL）
                       GET /api/v4/extract-results/batch/{batch_id}（获取结果）

        需要 Bearer Token（有效期 90 天）；限制 ≤200MB/≤200 页，
        支持 PDF/图片/Doc/Docx/PPT/PPTx/XLS/XLSX/HTML。
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

        # Step 1: 申请上传 URL（先过提交限流：40 文件/分钟；连接类错误自动重试）
        self._progress(f"申请上传 URL：{file_name}")
        _submit_limiter.acquire()
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

        data = result.get("data") or {}
        batch_id = data["batch_id"]
        file_urls = data["file_urls"]

        # Step 2: Upload file to OSS (with retries)
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        self._progress(f"上传中（{size_mb:.1f} MB）...")
        self._upload_to_oss(file_urls[0], file_path)
        self._progress("上传完成，已提交异步解析，开始轮询（每 5s）")

        # Step 3: Poll for result
        result_data = self._precision_poll(batch_id, api_key, timeout)
        self._progress("云端解析完成，下载结果包")

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
        """处理 Precision API 错误码（官方文档）。"""
        code_str = str(code)
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            code_int = None

        # Token 认证错误
        if code_str in ("A0202", "A0211") or "authenticate failed" in msg.lower():
            raise _AuthError(f"code={code}: {msg}")

        # 每日限额
        if code_int == -60018:
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

        hint = error_map.get(code_int, "") if code_int is not None else ""
        raise Exception(f"Precision API error (code={code}): {hint} - {msg}")

    def _upload_to_oss(self, oss_url: str, file_path: str) -> None:
        """上传文件到 OSS（失败重试；官方文档：URL 有效期 24 小时）。"""
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
        """轮询 Precision 结果，直到 done/failed。"""
        url = f"https://mineru.net/api/v4/extract-results/batch/{batch_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        empty_since = {"t": None}

        def parse(result: dict) -> tuple[dict | None, str | None]:
            if result.get("code") != 0:
                self._handle_precision_error(
                    result.get("code"), result.get("msg", "unknown")
                )

            data = result.get("data") or {}
            extract_results = data.get("extract_result") or []
            if not extract_results:
                now = time.monotonic()
                if empty_since["t"] is None:
                    empty_since["t"] = now
                elif now - empty_since["t"] >= 300:
                    raise TimeoutError("Precision API 连续 5 分钟返回空结果，任务可能已丢失")
                return None, None
            empty_since["t"] = None
            all_done = True
            state = None
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
            return (data if all_done else None), state

        return self._poll_until_done("Precision", url, timeout, parse, headers)

    def _get_with_proxy_fallback(self, url: str, timeout: tuple[int, int]):
        """先直连（绕过系统代理），失败再走系统代理。"""
        no_proxy = {"http": None, "https": None}  # type: ignore[arg-type]
        try:
            return requests.get(url, timeout=timeout, proxies=no_proxy)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.SSLError):
            return requests.get(url, timeout=timeout)

    def _extract_markdown_from_zip(self, zip_url: str) -> str:
        """下载结果 ZIP 并提取 full.md（官方文档：full_zip_url 有效期 24 小时）。"""
        try:
            resp = self._get_with_proxy_fallback(zip_url, (15, 120))
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
        """下载 markdown 内容（直连失败时回退系统代理；不关闭证书校验）。"""
        try:
            resp = self._get_with_proxy_fallback(url, (15, 60))
            resp.raise_for_status()
        except requests.exceptions.SSLError as e:
            raise RuntimeError(f"SSL 校验失败，请检查系统证书或代理配置: {e}") from e
        # T11: 显式 UTF-8 解码，避免 requests 按 ISO-8859-1 回退导致中文乱码
        return resp.content.decode("utf-8")
