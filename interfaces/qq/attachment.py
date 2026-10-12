"""QQ 侧的入站附件处理。

下载、SSRF 校验、分类的实现在 ``core/media_fetch.py``（微信接入端共用同一份，
避免安全逻辑两边漂移）；本模块只保留 QQ 特有的一层——把 SDK 的附件 DTO 翻成
``InboundMedia``。

**为什么不直接用 SDK 的 ``AttachmentDownloader``**（§4.11 / §4.12）：它只校验
scheme（``http``/``https`` + 有 netloc），**没有 DNS 解析、没有内网/保留网段
拦截**，**允许 http**，且**没有下载大小上限**。这四项我们都需要，所以自己实现
（cili 已有 httpx），只在分类规则上对齐官方插件 ``middleware/attachment.ts``。

落盘位置：``{workspace}/uploads/``——与 Web 拖放上传同一目录（用户要求两边统一）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from core.media_fetch import (
    InboundMedia,
    check_download_url,
    classify,
    download_to_uploads,
)

logger = logging.getLogger(__name__)

__all__ = [
    "InboundMedia",
    "check_download_url",
    "classify",
    "download_to_uploads",
    "process_attachments",
]


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
            interface="qq",
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
