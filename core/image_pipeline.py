"""图片归一化、引用化与附件落盘工具。

本模块保持无会话依赖，供 session/runner/tools/web 复用：
- EXIF 方向、尺寸、体积、编码归一化。
- 图片块 -> 引用文本。
- 请求投影：当前回合保留 base64，历史回合/工具图替换为引用。
- 持久化 sanitize：写 jsonl 前把图片块替换为引用文本。
- 用户图片与工具图片 sidecar 落盘。
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_MIME_EXTS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
}

DEFAULT_MAX_DIMENSION = 2048
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_JPEG_QUALITY = 85


def _ext_for_mime(mime: str) -> str:
    return _MIME_EXTS.get((mime or "").split(";", 1)[0].strip().lower(), ".img")


def _block_meta(block: dict[str, Any]) -> dict[str, Any]:
    meta = block.get("_meta")
    return dict(meta) if isinstance(meta, dict) else {}


def _merged_image_meta(
    block: dict[str, Any],
    parent_meta: dict[str, Any] | None = None,
    image_index: int | None = None,
) -> dict[str, Any]:
    meta = _block_meta(block)
    if parent_meta and image_index is not None:
        refs = parent_meta.get("image_refs") or []
        if isinstance(refs, list) and image_index < len(refs) and isinstance(refs[image_index], dict):
            meta = {**meta, **refs[image_index]}
    return meta


def image_key(block: dict[str, Any], parent_meta: dict[str, Any] | None = None,
              image_index: int | None = None) -> str:
    """稳定图片 key：优先 path，其次 block id。"""
    meta = _merged_image_meta(block, parent_meta, image_index)
    return str(meta.get("path") or block.get("id") or "")


def image_ref_text(block_or_meta: dict[str, Any], parent_meta: dict[str, Any] | None = None,
                   image_index: int | None = None) -> str:
    """生成模型可见的图片引用文本。"""
    if "type" in block_or_meta or "source" in block_or_meta:
        meta = _merged_image_meta(block_or_meta, parent_meta, image_index)
    else:
        meta = dict(block_or_meta or {})
    path = str(meta.get("path") or "").strip()
    media_type = str(meta.get("media_type") or "").strip()
    width = meta.get("width")
    height = meta.get("height")
    if not path:
        return "[image ref unavailable: historical image has no saved path; ask the user to re-attach if needed]"
    dims = ""
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        dims = f"; {width}x{height}"
    mt = f"; {media_type}" if media_type else ""
    return f'[image ref: {path}{mt}{dims}; use read_image("{path}") to view again]'


def _text_ref_block(block: dict[str, Any], parent_meta: dict[str, Any] | None = None,
                    image_index: int | None = None) -> dict[str, Any]:
    ref = image_ref_text(block, parent_meta, image_index)
    return {"type": "text", "id": block.get("id", ""), "text": ref}


def project_images_for_request(
    messages: list[dict[str, Any]],
    current_turn_id: str,
    *,
    force_reference: bool = False,
    demoted_paths: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """把请求副本中的历史图片替换为引用文本。

    当前回合的图片保留 base64；``force_reference`` 时全部替换。
    ``demoted_paths`` 用于 3MB/413 时按 path/id 降级当前回合图片。
    """
    demoted = set(demoted_paths or ())
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            msg["content"] = _project_content(
                content, current_turn_id, force_reference, demoted
            )
    return messages


def _should_reference(
    meta: dict[str, Any],
    block: dict[str, Any],
    current_turn_id: str,
    force_reference: bool,
    demoted: set[str],
) -> bool:
    key = str(meta.get("path") or block.get("id") or "")
    if force_reference or key in demoted:
        return True
    if current_turn_id and str(meta.get("turn_id") or "") != current_turn_id:
        return True
    return False


def _project_content(
    content: list[Any],
    current_turn_id: str,
    force_reference: bool,
    demoted: set[str],
) -> list[Any]:
    image_index = 0
    for idx, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "image":
            meta = _merged_image_meta(block)
            if _should_reference(meta, block, current_turn_id, force_reference, demoted):
                content[idx] = _text_ref_block(block)
            image_index += 1
        elif btype == "tool_result":
            parent_meta = _block_meta(block)
            nested = block.get("content")
            if not isinstance(nested, list):
                continue
            nested_image_index = 0
            for sub_idx, sub in enumerate(nested):
                if not isinstance(sub, dict):
                    continue
                if sub.get("type") != "image":
                    continue
                sub_meta = _merged_image_meta(sub, parent_meta, nested_image_index)
                # 把 path 等元数据挂到图片块，供预算/降级使用。
                sub.setdefault("_meta", {}).update({
                    k: v for k, v in sub_meta.items() if k not in ("path", "turn_id")
                })
                if "path" in sub_meta:
                    sub["_meta"]["path"] = sub_meta["path"]
                if "turn_id" in sub_meta:
                    sub["_meta"]["turn_id"] = sub_meta["turn_id"]
                if _should_reference(sub_meta, sub, current_turn_id, force_reference, demoted):
                    nested[sub_idx] = _text_ref_block(sub, parent_meta, nested_image_index)
                nested_image_index += 1
            image_index += nested_image_index
    return content


def sanitize_images_for_persistence(content: Any) -> Any:
    """写 jsonl 前把图片块替换为引用文本；保留 block ``_meta``。"""
    if not isinstance(content, list):
        return content
    out: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
            continue
        btype = block.get("type")
        if btype == "image":
            out.append(_text_ref_block(block))
            continue
        if btype == "tool_result":
            parent_meta = _block_meta(block)
            nested = block.get("content")
            if isinstance(nested, list):
                new_nested: list[Any] = []
                image_index = 0
                for sub in nested:
                    if isinstance(sub, dict) and sub.get("type") == "image":
                        new_nested.append(_text_ref_block(sub, parent_meta, image_index))
                        image_index += 1
                    else:
                        new_nested.append(sub)
                out.append({**block, "content": new_nested})
                continue
        out.append(block)
    return out


def normalize_image_bytes(
    data: bytes,
    media_type: str = "image/png",
    *,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    max_bytes: int = DEFAULT_MAX_BYTES,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> tuple[bytes, str, int, int] | None:
    """应用 EXIF 方向、缩放、编码到目标体积。

    Returns ``(bytes, media_type, width, height)``；Pillow 不可用或解码失败返回 None。
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as src:
            img = ImageOps.exif_transpose(src).copy()
        if img.width > max_dimension or img.height > max_dimension:
            img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

        has_alpha = img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info
        )
        if has_alpha:
            img = img.convert("RGBA")
            png = _encode_png(img, max_bytes)
            if png is not None:
                return png, "image/png", img.width, img.height
            # PNG 太大：白底转 JPEG。
            flat = Image.new("RGB", img.size, (255, 255, 255))
            flat.paste(img, mask=img.getchannel("A"))
            img = flat
        elif img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        encoded = _encode_jpeg_ladder(img, max_bytes, jpeg_quality)
        if encoded is not None:
            return encoded, "image/jpeg", img.width, img.height

        # 质量阶梯仍不够：逐步缩小直到能装下。
        current = img
        while current.width > 1 or current.height > 1:
            current = current.resize(
                (max(1, int(current.width * 0.75)), max(1, int(current.height * 0.75))),
                Image.Resampling.LANCZOS,
            )
            encoded = _encode_jpeg_ladder(current, max_bytes, jpeg_quality)
            if encoded is not None:
                return encoded, "image/jpeg", current.width, current.height
        return None
    except Exception as e:
        logger.warning("[image_pipeline] normalize failed: %s", e)
        return None


def _encode_png(img, max_bytes: int) -> bytes | None:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    out = buf.getvalue()
    return out if len(out) <= max_bytes else None


def _encode_jpeg_ladder(img, max_bytes: int, quality: int) -> bytes | None:
    qualities = [quality, 75, 60, 40]
    for q in qualities:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)
        out = buf.getvalue()
        if len(out) <= max_bytes:
            return out
    return None


def save_base64_image(
    data_b64: str,
    media_type: str,
    uploads_dir: Path,
    *,
    workspace_dir: Path | None = None,
    prefix: str = "image",
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    max_bytes: int = DEFAULT_MAX_BYTES,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> dict[str, Any] | None:
    """解码、归一化并写入 uploads；返回 path/media_type/尺寸/attachment_id。"""
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return None
    if not raw:
        return None

    normalized = normalize_image_bytes(
        raw, media_type, max_dimension=max_dimension, max_bytes=max_bytes,
        jpeg_quality=jpeg_quality,
    )
    if normalized is not None:
        payload, mime, width, height = normalized
    else:
        payload, mime, width, height = raw, media_type or "image/png", None, None

    digest = hashlib.sha256(payload).hexdigest()
    suffix = _ext_for_mime(mime)
    name = f"{prefix}_{time.time_ns()}_{digest[:8]}{suffix}"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    path = uploads_dir / name
    path.write_bytes(payload)
    rel = str(path)
    if workspace_dir is not None:
        try:
            rel = path.relative_to(workspace_dir).as_posix()
        except ValueError:
            rel = str(path)
    return {
        "path": rel,
        "media_type": mime,
        "width": width,
        "height": height,
        "attachment_id": f"sha256:{digest}",
    }


def write_tool_image_sidecar(
    session_dir: Path,
    workspace_dir: Path | None,
    tool_use_id: str,
    image_index: int,
    block: Any,
) -> dict[str, Any] | None:
    """把工具结果中的 ImageBlock 写成 sidecar 文件并返回引用元数据。"""
    if hasattr(block, "data"):
        data_b64 = getattr(block, "data", "")
        media_type = getattr(block, "mime_type", "") or "image/png"
    elif isinstance(block, dict):
        source = block.get("source") or {}
        data_b64 = str(source.get("data") or "")
        media_type = str(source.get("media_type") or "image/png")
    else:
        return None
    if not data_b64:
        return None
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return None

    normalized = normalize_image_bytes(raw, media_type)
    if normalized is not None:
        payload, mime, width, height = normalized
    else:
        payload, mime, width, height = raw, media_type, None, None

    digest = hashlib.sha256(payload).hexdigest()
    suffix = _ext_for_mime(mime)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", tool_use_id or "") or secrets.token_hex(6)
    attachments = session_dir / "attachments"
    attachments.mkdir(parents=True, exist_ok=True)
    path = attachments / f"{safe_id}-{image_index}{suffix}"
    path.write_bytes(payload)
    rel = str(path)
    if workspace_dir is not None:
        try:
            rel = path.relative_to(workspace_dir).as_posix()
        except ValueError:
            rel = str(path)
    return {
        "path": rel,
        "media_type": mime,
        "width": width,
        "height": height,
        "attachment_id": f"sha256:{digest}",
    }


def ensure_user_image_meta(
    block: dict[str, Any],
    *,
    cwd: str | Path | None,
    turn_id: str = "",
) -> dict[str, Any]:
    """补齐用户图片 block 的 id、turn_id、path。

    ``path`` 缺失且 source 是 base64 时，尝试写入 ``{cwd}/uploads``。
    """
    if not isinstance(block, dict) or block.get("type") != "image":
        return block
    meta = block.setdefault("_meta", {})
    if not block.get("id"):
        block["id"] = f"img_{secrets.token_hex(4)}"
    if turn_id:
        meta.setdefault("turn_id", turn_id)
    source = block.get("source") or {}
    media_type = str(source.get("media_type") or "image/png")
    meta.setdefault("media_type", media_type)
    if meta.get("path"):
        return block
    data_b64 = source.get("data")
    if not data_b64 or not cwd:
        return block
    saved = save_base64_image(
        str(data_b64),
        media_type,
        Path(cwd) / "uploads",
        workspace_dir=Path(cwd),
        prefix="upload",
    )
    if saved:
        meta.update(saved)
    return block
