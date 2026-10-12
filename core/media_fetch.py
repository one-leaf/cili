"""入站附件下载、分类、落盘（接口无关）。

**为什么不直接用平台 SDK 的下载器**（QQ 侧原话）：它只校验 scheme
（``http``/``https`` + 有 netloc），**没有 DNS 解析、没有内网/保留网段拦截**，
**允许 http**，且**没有下载大小上限**。这四项我们都需要，所以自己实现（cili 已有
httpx），只在分类规则上对齐官方插件 ``middleware/attachment.ts``。

落盘位置：``{workspace}/uploads/``——与 Web 拖放上传同一目录（用户要求两边统一）。

``transform`` 钩子
-----------------
微信 iLink 的媒体是 **AES-128-ECB 加密**的，必须边下载边解密。与其让它先落一份
密文再原地解密，不如在写入前挂一个转换器：``transform(chunk, is_last) -> bytes``
（``is_last=True`` 表示这是最后一块，解密器据此去 PKCS7 填充）。默认 None = 原样写入。
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
from typing import Callable
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

# 每块解密器签名：(数据块, 是否最后一块) -> 明文块
Transform = Callable[[bytes, bool], bytes]


@dataclass
class InboundMedia:
    """一个已落盘的入站附件。"""

    kind: str            # "image" | "voice" | "file"
    path: str            # 绝对路径
    rel_path: str        # 工作区相对路径（写进消息文本给模型看）
    filename: str
    content_type: str = ""
    transcript: str = ""  # 语音：平台自带的转写文本（不做本地 STT）


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


def _resolve_target(
    uploads_dir: Path,
    filename_hint: str,
    fallback_ext: str,
    interface: str,
) -> Path:
    """在 uploads 目录里挑一个不冲突的目标路径。"""
    safe = safe_upload_filename(filename_hint) if filename_hint else None
    if not safe:
        safe = f"{interface}_{int(time.time() * 1000)}{fallback_ext}"
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
    interface: str = "media",
    transform: Transform | None = None,
) -> Path | None:
    """流式下载到 ``uploads_dir``，返回落盘路径；失败返回 None。

    边写边校验大小（``max_bytes``），避免大文件吃内存；先写 ``.part`` 再原子改名。
    ``transform`` 见模块 docstring（微信用它做 AES 流式解密）。
    """
    reason = await asyncio.to_thread(check_download_url, url)
    if reason:
        logger.warning(f"[{interface}] 拒绝下载 {url[:80]}：{reason}")
        return None

    uploads_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(urlparse(url).path).suffix or Path(filename_hint or "").suffix or ".bin"
    target = _resolve_target(uploads_dir, filename_hint, ext, interface)
    tmp = target.with_suffix(target.suffix + ".part")

    written = 0
    try:
        async with client.stream("GET", url, follow_redirects=False) as resp:
            if resp.status_code != 200:
                logger.warning(
                    f"[{interface}] 下载失败 status={resp.status_code} url={url[:80]}"
                )
                return None
            with open(tmp, "wb") as f:
                # 需要「预读一块」才能告诉转换器哪一块是最后一块（PKCS7 填充在末尾）
                pending: bytes | None = None
                async for chunk in resp.aiter_bytes(256 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        logger.warning(
                            f"[{interface}] 附件超过上限 {max_bytes} 字节，放弃：{url[:80]}"
                        )
                        return None
                    if pending is not None:
                        out = transform(pending, False) if transform else pending
                        if out:
                            f.write(out)
                    pending = chunk
                if pending is not None:
                    out = transform(pending, True) if transform else pending
                    if out:
                        f.write(out)
        os.replace(tmp, target)
        logger.info(f"[{interface}] 已保存附件: {target}")
        return target
    except Exception as e:  # noqa: BLE001 — 网络/IO 任何异常都不该打断回合
        logger.warning(f"[{interface}] 下载异常 url={url[:80]}：{e}")
        return None
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ─── 分类 ─────────────────────────────────────────────────────────────


def classify(content_type: str, filename: str = "") -> str:
    """按 content_type / 扩展名判定类别。"""
    ct = (content_type or "").strip().lower()
    if ct.startswith(_IMAGE_CONTENT_PREFIX):
        return "image"
    if ct == "voice" or ct.startswith("audio/"):
        return "voice"
    return "file"
