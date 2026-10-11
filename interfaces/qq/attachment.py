"""入站附件：下载、分类、落盘。

**为什么不直接用 SDK 的 ``AttachmentDownloader``**（§4.11 / §4.12）：它只校验
scheme（``http``/``https`` + 有 netloc），**没有 DNS 解析、没有内网/保留网段
拦截**（注释写着可子类化扩展），**允许 http**，且**没有下载大小上限**。这四项
我们都需要，所以自己实现下载（cili 已有 httpx），只在分类规则上对齐官方插件
``middleware/attachment.ts``。

落盘位置：``{workspace}/uploads/``——与 Web 拖放上传同一目录（用户要求两边统一）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from core.fs_utils import safe_upload_filename

logger = logging.getLogger(__name__)

# 云元数据端点：硬拒绝，任何情况下不下载
_METADATA_HOSTS = {
    "metadata.tencentyun.com",
    "metadata.google.internal",
    "metadata",
}

_IMAGE_CONTENT_PREFIX = "image/"


@dataclass
class InboundMedia:
    """一个已落盘的入站附件。"""

    kind: str            # "image" | "voice" | "file"
    path: str            # 绝对路径
    rel_path: str        # 工作区相对路径（写进消息文本给模型看）
    filename: str
    content_type: str = ""
    transcript: str = ""  # 语音：QQ 自带的 asr_refer_text（本次不做 STT）


# ─── URL 安全校验（SSRF） ─────────────────────────────────────────────


def check_download_url(url: str) -> str | None:
    """校验下载 URL。返回 None 表示放行；否则返回拒绝原因。

    策略：
    - 仅 https（官方 TS 插件同样强制 https）
    - 云元数据端点硬拒绝
    - **解析 DNS 后逐个校验**：任一解析结果是私网/环回/链路本地/保留地址即拒绝。
      这是与 cili 既有浏览器导航校验的关键差异——那边把域名交给浏览器解析、
      无法静态预判；我们服务端自己发请求，必须先解析再判定。
    """
    if not url:
        return "URL 为空"
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return f"仅允许 https（收到 {parsed.scheme or '空'}）"
    host = (parsed.hostname or "").lower()
    if not host:
        return "URL 缺少主机名"
    if host in _METADATA_HOSTS:
        return f"{host} 是云元数据端点，拒绝下载"

    # 字面 IP 直接判定
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None:
        return None if addr.is_global else f"{host} 不是公网地址，拒绝下载"

    # 域名：解析后逐个判定（防止域名指向内网）
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        return f"域名解析失败：{e}"
    for info in infos:
        ip = info[4][0]
        try:
            if not ipaddress.ip_address(ip).is_global:
                return f"{host} 解析到非公网地址 {ip}，拒绝下载"
        except ValueError:
            return f"{host} 解析出无法识别的地址 {ip}"
    return None


# ─── 下载 ─────────────────────────────────────────────────────────────


def _resolve_target(uploads_dir: Path, filename_hint: str, fallback_ext: str) -> Path:
    """在 uploads 目录里挑一个不冲突的目标路径。"""
    safe = safe_upload_filename(filename_hint) if filename_hint else None
    if not safe:
        safe = f"qq_{int(time.time() * 1000)}{fallback_ext}"
    target = uploads_dir / safe
    if target.exists():
        target = uploads_dir / f"{target.stem}_{int(time.time() * 1000)}{target.suffix}"
    return target


async def download_to_uploads(
    url: str,
    filename_hint: str,
    *,
    uploads_dir: Path,
    max_bytes: int,
    client,
) -> Path | None:
    """流式下载到 ``uploads_dir``，返回落盘路径；失败返回 None。

    边写边校验大小（``max_bytes``），避免大文件吃内存；先写 ``.part`` 再原子改名。
    """
    reason = await asyncio.to_thread(check_download_url, url)
    if reason:
        logger.warning(f"[qq/attachment] 拒绝下载 {url[:80]}：{reason}")
        return None

    uploads_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(urlparse(url).path).suffix or Path(filename_hint or "").suffix or ".bin"
    target = _resolve_target(uploads_dir, filename_hint, ext)
    tmp = target.with_suffix(target.suffix + ".part")

    written = 0
    try:
        async with client.stream("GET", url, follow_redirects=False) as resp:
            if resp.status_code != 200:
                logger.warning(
                    f"[qq/attachment] 下载失败 status={resp.status_code} url={url[:80]}"
                )
                return None
            with open(tmp, "wb") as f:
                async for chunk in resp.aiter_bytes(256 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        logger.warning(
                            f"[qq/attachment] 附件超过上限 {max_bytes} 字节，放弃：{url[:80]}"
                        )
                        return None
                    f.write(chunk)
        os.replace(tmp, target)
        logger.info(f"[qq/attachment] 已保存附件: {target}")
        return target
    except Exception as e:  # noqa: BLE001 — 网络/IO 任何异常都不该打断回合
        logger.warning(f"[qq/attachment] 下载异常 url={url[:80]}：{e}")
        return None
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ─── 分类与批量处理 ───────────────────────────────────────────────────


def classify(content_type: str, filename: str = "") -> str:
    """按 content_type / 扩展名判定类别。"""
    ct = (content_type or "").strip().lower()
    if ct.startswith(_IMAGE_CONTENT_PREFIX):
        return "image"
    if ct == "voice" or ct.startswith("audio/"):
        return "voice"
    return "file"


def _attachment_url(att) -> str:
    """取附件下载 URL（SDK 的 DTO 提供 ``resolved_url``，已归一化协议相对地址）。"""
    resolved = getattr(att, "resolved_url", None)
    if resolved:
        return str(resolved)
    url = getattr(att, "url", "") or ""
    if isinstance(url, str) and url.startswith("//"):
        return f"https:{url}"
    return str(url)


async def process_attachments(
    attachments,
    *,
    workspace_dir: Path,
    uploads_dir: Path,
    max_bytes: int,
    client,
) -> list[InboundMedia]:
    """下载并分类全部入站附件（失败的静默跳过，只记日志）。

    官方 TS 插件用 ``Promise.all`` 并行下载；这里也并发（SDK 的
    ``AttachmentProcessor`` 反倒是顺序的）。
    """
    if not attachments:
        return []

    async def _one(att) -> InboundMedia | None:
        url = _attachment_url(att)
        if not url:
            return None
        filename = str(getattr(att, "filename", "") or "")
        content_type = str(getattr(att, "content_type", "") or "")
        kind = classify(content_type, filename)

        # 语音优先用 QQ 自带的 ASR 结果，拿不到就只留文件
        transcript = str(getattr(att, "asr_refer_text", "") or "")
        source_url = url
        if kind == "voice":
            wav = getattr(att, "voice_wav_url", "") or ""
            if wav:
                source_url = str(wav)

        path = await download_to_uploads(
            source_url,
            filename,
            uploads_dir=uploads_dir,
            max_bytes=max_bytes,
            client=client,
        )
        if path is None:
            return None
        try:
            rel = os.path.relpath(str(path), str(workspace_dir))
        except ValueError:
            rel = str(path)
        return InboundMedia(
            kind=kind,
            path=str(path),
            rel_path=rel,
            filename=path.name,
            content_type=content_type,
            transcript=transcript,
        )

    results = await asyncio.gather(*(_one(a) for a in attachments), return_exceptions=True)
    out: list[InboundMedia] = []
    for r in results:
        if isinstance(r, InboundMedia):
            out.append(r)
        elif isinstance(r, BaseException):
            logger.warning(f"[qq/attachment] 附件处理异常：{r}")
    return out
