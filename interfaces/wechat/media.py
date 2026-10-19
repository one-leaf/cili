"""微信媒体：入站下载解密、出站加密上传。

**iLink 的媒体是 AES-128-ECB 加密的**——入站要解密，出站要加密。密钥由发送方生成：

- **入站**：图片的 key 在 item 顶层 ``aeskey``（32 字符 hex），其余在 ``media.aes_key``
  （base64，解出来可能是 raw16，也可能是 hex 串的 ASCII）。非图片**必须**有 key。
- **出站**：我们随机生成 16 字节。注意两个地方用的是**不同编码**——``getuploadurl``
  报 hex 串，而 ``sendmessage`` 的 ``media.aes_key`` 填 ``base64(hex串)``（**不是**
  raw16 的 base64）。这个细节弄错了服务端不会报错，只会让用户下载到坏文件。

出站上传链路（iLink 没有 SDK，得自己走）：``getuploadurl`` → AES 加密 → POST CDN →
读响应头 ``x-encrypted-param`` 作为下载参数。对标官方插件 v1.0.3。

**本次范围**：出站只发图片与文件（视频/语音出站未实现）；入站四类都下载，图片走
多模态、语音用微信自带转写、其余把路径写进正文。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from core.media_fetch import InboundMedia, download_to_uploads
from interfaces.wechat.config import WechatConfig
from interfaces.wechat.protocol import (
    ITEM_FILE,
    ITEM_IMAGE,
    UPLOAD_MEDIA_FILE,
    UPLOAD_MEDIA_IMAGE,
    EcbStreamDecryptor,
    MediaRef,
    aes_ecb_encrypt,
    parse_aes_key,
    pkcs7_padded_size,
)

if TYPE_CHECKING:
    from interfaces.wechat.client import WechatClient
    from interfaces.wechat.outbound import OutboundSender

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".ico", ".svg"}

# 服务端不给图片的扩展名，下载时先按这个占位（真实类型靠嗅探，见 _sniff_image_mime）
_DEFAULT_IMAGE_EXT = ".jpg"

_IMAGE_MIME_EXTS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


class MediaError(Exception):
    """出站媒体处理失败（文案可直接给用户看）。"""


# ─── 入站：下载 + 解密 ────────────────────────────────────────────────


def _download_urls(ref: MediaRef, cdn_base: str) -> list[str]:
    """候选下载地址，按优先级：``full_url`` 优先，其次按参数拼。"""
    urls: list[str] = []
    if ref.full_url:
        urls.append(ref.full_url)
    if ref.encrypt_query_param:
        urls.append(
            f"{cdn_base.rstrip('/')}/download"
            f"?encrypted_query_param={quote(ref.encrypt_query_param)}"
        )
    return urls


def _sniff_image_mime(path: Path) -> str:
    """从文件头判断图片类型。

    服务端不告诉我们图片格式，而多模态接口会**校验 ``media_type`` 与真实字节是否
    一致**，所以必须看魔数而不是猜。
    """
    try:
        head = path.read_bytes()[:12]
    except OSError:
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _align_image_extension(path: Path, mime: str) -> Path:
    """把图片文件的扩展名改成与真实类型一致。

    下载时只能用占位扩展名（服务端不给格式）。扩展名错了不只是难看：按扩展名判类型的
    工具（如 ``read_image``）会把错误的 ``media_type`` 报给模型接口，那边会直接报错。
    """
    want = _IMAGE_MIME_EXTS.get(mime)
    if not want or path.suffix.lower() == want:
        return path
    target = path.with_suffix(want)
    try:
        path.rename(target)
    except OSError:
        return path
    return target


async def download_media(
    ref: MediaRef,
    *,
    cfg: WechatConfig,
    client,
    uploads_dir: Path,
    workspace_dir: Path,
) -> InboundMedia | None:
    """下载并（按需）解密一条入站媒体，落盘到 ``uploads_dir``。

    失败返回 None，由调用方降级成一条说明文本——单条媒体拿不到不该让整个回合失败。
    """
    key = parse_aes_key(ref.aes_key)
    if ref.kind != "image" and key is None:
        # 非图片必须解密，没 key 就没法用（官方插件同样放弃）
        logger.warning(f"[wechat/media] 入站 {ref.kind} 缺少 aeskey，放弃")
        return None

    urls = _download_urls(ref, cfg.cdn_base)
    if not urls:
        logger.warning(f"[wechat/media] 入站 {ref.kind} 没有可用的下载地址")
        return None

    if ref.file_name:
        hint = ref.file_name
    elif ref.kind == "image":
        hint = f"wechat_image_{int(time.time() * 1000)}{_DEFAULT_IMAGE_EXT}"
    else:
        hint = f"wechat_{ref.kind}_{int(time.time() * 1000)}"

    path: Path | None = None
    for url in urls:
        # 每次尝试都用新的解密器：中途失败时旧的可能已经被喂过数据
        path = await download_to_uploads(
            url,
            hint,
            uploads_dir=uploads_dir,
            max_bytes=cfg.max_inbound_bytes,
            client=client,
            interface="wechat",
            transform=EcbStreamDecryptor(key) if key else None,
        )
        if path is not None:
            break
    if path is None:
        return None

    # 图片的真实类型靠魔数（下载时占位扩展名可能不对），并把扩展名对齐过来
    if ref.kind == "image":
        content_type = _sniff_image_mime(path)
        path = _align_image_extension(path, content_type)
    else:
        content_type = mimetypes.guess_type(path.name)[0] or ""
    try:
        rel = os.path.relpath(str(path), str(workspace_dir))
    except ValueError:
        rel = str(path)
    return InboundMedia(
        kind=ref.kind,
        path=str(path),
        rel_path=rel,
        filename=path.name,
        content_type=content_type,
        transcript=ref.voice_text,
    )


# ─── 出站：加密 + 上传 ────────────────────────────────────────────────


def infer_upload_type(path: str | Path) -> tuple[int, int, str]:
    """扩展名 → ``(getuploadurl 的 media_type, item 类型, item 键)``。

    本次只区分「图片」与「其它文件」：图片内嵌成预览，其余走文件卡片。
    """
    if Path(path).suffix.lower() in _IMAGE_EXTS:
        return UPLOAD_MEDIA_IMAGE, ITEM_IMAGE, "image_item"
    return UPLOAD_MEDIA_FILE, ITEM_FILE, "file_item"


def validate_media_path(path: str | Path, workspace_root: str | Path) -> Path:
    """路径白名单校验（realpath 双侧比对，防符号链接绕过）。

    ``send_user_file`` 工具已经校验过一遍，这里再校验一次是纵深防御——本模块才是真正把
    文件内容交给网络的地方。
    """
    target = Path(os.path.realpath(str(path)))
    root = Path(os.path.realpath(str(workspace_root)))
    try:
        inside = os.path.commonpath([str(target), str(root)]) == str(root)
    except ValueError:
        inside = False
    if not inside:
        raise MediaError(f"文件不在工作区内：{path}")
    if not target.is_file():
        raise MediaError(f"文件不存在：{path}")
    return target


async def upload_media(
    client: "WechatClient",
    cfg: WechatConfig,
    path: Path,
    user_id: str,
) -> dict:
    """上传本地文件到微信 CDN，返回可直接放进 ``item_list`` 的媒体项。"""
    raw = path.read_bytes()
    raw_size = len(raw)
    upload_type, item_type, item_key = infer_upload_type(path)

    aes_key_raw = os.urandom(16)
    file_key = os.urandom(16).hex()
    body = {
        "filekey": file_key,
        "media_type": upload_type,
        "to_user_id": user_id,
        "rawsize": raw_size,
        "rawfilemd5": hashlib.md5(raw).hexdigest(),
        "filesize": pkcs7_padded_size(raw_size),
        "no_need_thumb": True,
        "aeskey": aes_key_raw.hex(),
    }

    resp = await client.get_upload_url(body)
    upload_full_url = str(resp.get("upload_full_url", "") or "").strip()
    upload_param = str(resp.get("upload_param", "") or "")
    if upload_full_url:
        cdn_url = upload_full_url
    elif upload_param:
        cdn_url = (
            f"{cfg.cdn_base.rstrip('/')}/upload"
            f"?encrypted_query_param={quote(upload_param)}&filekey={quote(file_key)}"
        )
    else:
        raise MediaError("服务端没有返回上传地址")

    download_param = await client.upload_encrypted(cdn_url, aes_ecb_encrypt(raw, aes_key_raw))

    media: dict = {
        "encrypt_query_param": download_param,
        # ⚠️ hex 串的 base64，不是 raw16 的 base64（见模块 docstring）
        "aes_key": base64.b64encode(aes_key_raw.hex().encode()).decode(),
        "encrypt_type": 1,
    }
    item: dict = {"media": media}
    if item_type == ITEM_IMAGE:
        item["mid_size"] = pkcs7_padded_size(raw_size)
    else:
        item["file_name"] = path.name
        item["len"] = str(raw_size)
    return {"type": item_type, item_key: item}


async def send_user_file(
    sender: "OutboundSender",
    user_id: str,
    path: str | Path,
    *,
    workspace_root: str | Path | None = None,
    reserve: int = 0,
) -> None:
    """上传并发送一个文件/图片（走 ``OutboundSender``，与文本共享发送预算）。

    失败一律抛 ``MediaError``，文案可直接展示给用户。
    """
    target = Path(path)
    if workspace_root is not None:
        target = validate_media_path(target, workspace_root)
    if not target.is_file():
        raise MediaError(f"文件不存在：{target}")

    try:
        item = await upload_media(sender.client, sender.cfg, target, user_id)
    except MediaError:
        raise
    except Exception as e:  # noqa: BLE001 — 上传链路任何异常都翻成可展示的文案
        logger.exception("[wechat/media] 上传文件失败")
        raise MediaError(f"发送文件失败：{str(e)[:120]}") from e

    if not await sender.send_media_item(user_id, item, reserve=reserve):
        raise MediaError("本轮发送配额不足，文件未能发出")
