"""出站富媒体：类型映射 + 路径校验，实际上传委托 SDK。

分工（§4.12「两个官方实现的分歧」）：
- **SDK 负责传输**：``MediaUploader.upload()`` 对本地文件自动走分片上传
  （prepare → PUT 预签名 → part_finish → complete），自带分片级重试；URL 源走
  URL 上传。我们不再判断 5MB 阈值、不算 md5/sha1/md5_10m、不自己 PUT。
- **我们负责策略**：扩展名 → ``file_type`` 映射、路径白名单校验、错误文案化。
  SDK 的 ``MediaUploader`` **不做路径白名单**（只 ``(Path.cwd()/p).resolve()``），
  官方 TS 插件才有 ``isPathInAllowedRoots``——所以这块必须自己实现。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# 与 SDK 常量对齐（本地定义一份，避免模块导入期就依赖 SDK）
MEDIA_TYPE_IMAGE = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_VOICE = 3
MEDIA_TYPE_FILE = 4

# ⚠️ 保守映射：官方文档两处自相矛盾——「支持的消息类型」概述说图片含
# gif/webp/bmp，但「文件类型与限制」表说 file_type=1 只保证 png/jpg。
# 只有 png/jpg 能保证内嵌成图片；其余一律归 4（文件卡片），实测后再放宽。
_IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
_VIDEO_EXTS = {".mp4"}
_VOICE_EXTS = {".silk"}

# 硬限制（官方「文件类型与限制」表，超硬限制报错 850031）
HARD_LIMIT_BYTES = 200 * 1024 * 1024
# 软限制：超过会被降级为文件类型（不报错，只影响呈现）
SOFT_LIMIT_BYTES = {
    MEDIA_TYPE_IMAGE: 20 * 1024 * 1024,
    MEDIA_TYPE_VIDEO: 30 * 1024 * 1024,
    MEDIA_TYPE_VOICE: 20 * 1024 * 1024,
    MEDIA_TYPE_FILE: HARD_LIMIT_BYTES,
}


class MediaError(Exception):
    """出站媒体处理失败（文案可直接给用户看）。"""


def infer_file_type(path: str | Path) -> int:
    """扩展名 → QQ ``file_type``（保守映射，见模块 docstring）。"""
    ext = Path(path).suffix.lower()
    if ext in _IMAGE_EXTS:
        return MEDIA_TYPE_IMAGE
    if ext in _VIDEO_EXTS:
        return MEDIA_TYPE_VIDEO
    if ext in _VOICE_EXTS:
        return MEDIA_TYPE_VOICE
    return MEDIA_TYPE_FILE


def validate_media_path(path: str | Path, workspace_root: str | Path) -> Path:
    """路径白名单校验（realpath 双侧比对，防符号链接绕过）。

    ``send_user_file`` 工具已校验过一遍，这里再校验一次是纵深防御——本模块才是真正
    把路径交给 SDK 的地方。
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


def _describe_size(size: int) -> str:
    return f"{size / 1024 / 1024:.1f}MB"


async def upload_file(
    uploader,
    openid: str,
    path: str | Path,
    *,
    file_type: int | None = None,
) -> str:
    """上传本地文件，返回 ``file_info``。异常统一转成 ``MediaError``。"""
    target = Path(path)
    kind = file_type if file_type is not None else infer_file_type(target)

    size = target.stat().st_size
    if size > HARD_LIMIT_BYTES:
        raise MediaError(
            f"文件过大（{_describe_size(size)}，上限 {_describe_size(HARD_LIMIT_BYTES)}）"
        )
    soft = SOFT_LIMIT_BYTES.get(kind, HARD_LIMIT_BYTES)
    if size > soft:
        # 超软限制不报错，但 QQ 会把它降级成文件卡片——提前告知，避免"图没显示"的困惑
        logger.info(
            f"[qq/media] {target.name} 超过类型软限制（{_describe_size(size)} > "
            f"{_describe_size(soft)}），QQ 侧会降级为文件卡片"
        )
        kind = MEDIA_TYPE_FILE

    try:
        return await uploader.upload(
            chat_type="c2c",
            chat_id=openid,
            source=str(target),
            file_type=kind,
            file_name=target.name,
        )
    except Exception as e:  # noqa: BLE001 — 含 SDK 的 UploadFileTooLargeError /
        # UploadDailyLimitExceededError（40093002 每日容量上限）等类型化异常
        raise MediaError(_friendly_upload_error(e)) from e


def _friendly_upload_error(exc: Exception) -> str:
    """把 SDK 的异常翻成给用户看的文案。"""
    name = type(exc).__name__
    text = str(exc)
    if "UploadDailyLimitExceeded" in name or "40093002" in text:
        return "今天的文件发送容量已用完，请明天再试或减小文件体积"
    if "UploadFileTooLarge" in name or "850031" in text:
        return "文件超过 QQ 的大小限制"
    if "850019" in text:
        return "QQ 不支持该文件格式"
    if "850026" in text:
        return "QQ 侧下载原始文件失败，请稍后重试"
    return f"发送文件失败：{text[:120]}"


async def send_user_file(
    api,
    uploader,
    openid: str,
    path: str | Path,
    *,
    msg_id: str,
    note: str = "",
) -> None:
    """上传并发送一个文件（``msg_type=7`` 富媒体消息）。

    ``srv_send_msg`` 一律 **False**：上传即发会占用**主动消息**频次，而主动消息
    配额稀缺（未认证 30/qpm）；走「上传拿 file_info → 单独发消息」只用被动回复配额。
    """
    file_info = await upload_file(uploader, openid, path)

    from qqbot_agent_sdk import MediaInfo, MessageToCreate, QQMessageType

    body: dict = {"msg_type": QQMessageType.RICH_MEDIA, "media": MediaInfo(file_info=file_info)}
    if note:
        body["content"] = note
    msg = MessageToCreate(
        msg_id=msg_id,
        msg_seq=int(api.next_msg_seq()),
        **body,
    )
    await api.post_c2c_message(openid, msg)
    logger.info(f"[qq/media] 已发送文件 {Path(path).name} → {openid}")
