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
import mimetypes
import re
import secrets
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from core.media_fetch import InboundMedia

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

# 服务端硬限制（以 base64 字符串长度计）：单图 ≤5MB。
MAX_INLINE_BASE64_BYTES = 5 * 1024 * 1024
# DeepSeek 请求图目标（deepseek-harness）：
# - 单图编码目标 2 MiB（DEFAULT_REQUEST_IMAGE_MAX_BYTES）
# - 单请求 ≥15 张图时 provider 单边上限 4096px（REQUEST_IMAGE_MAX_DIMENSION）；
#   统一按 4096 归一化，保证图片数量变化不改变投影结果。
DEFAULT_MAX_DIMENSION = 4096
DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB（原始编码字节）
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
        # 内嵌图片的回合归属挂在父 tool_result 上（image_refs 条目里没有）。
        # 不继承会让当前回合刚读的图片被 _should_reference 判成历史图、
        # 投影成引用文本 —— 模型看不到 read_image 返回的图。
        if not meta.get("turn_id") and parent_meta.get("turn_id"):
            meta["turn_id"] = parent_meta["turn_id"]
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


def collect_current_turn_image_keys(
    messages: list[dict[str, Any]], current_turn_id: str
) -> list[str]:
    """按出现顺序收集当前回合仍内联的图片稳定 key（path 优先，其次 block id）。

    顶层 image 块与 tool_result 内嵌图片统一计入；返回值用于数量预算与
    「最旧优先」降级（与请求投影同一口径）。
    """
    if not current_turn_id:
        return []
    keys: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                meta = _block_meta(block)
                if str(meta.get("turn_id") or "") == current_turn_id:
                    key = image_key(block)
                    if key:
                        keys.append(key)
                continue
            if block.get("type") != "tool_result":
                continue
            parent_meta = _block_meta(block)
            if str(parent_meta.get("turn_id") or "") != current_turn_id:
                continue
            nested = block.get("content")
            if not isinstance(nested, list):
                continue
            image_index = 0
            for sub in nested:
                if not isinstance(sub, dict) or sub.get("type") != "image":
                    continue
                key = image_key(sub, parent_meta, image_index)
                if key:
                    keys.append(key)
                image_index += 1
    return keys


def _base64_len(block: dict[str, Any]) -> int:
    source = block.get("source")
    if not isinstance(source, dict):
        return 0
    data = source.get("data")
    return len(data) if isinstance(data, str) else 0


# ─── 请求级图片预算：含图 tool_result（carrier）成批退役 ──────────────

# DeepSeek：≥15 张图时 provider 单边上限 4096px
_MANY_IMAGE_DIMENSION_LIMIT = DEFAULT_MAX_DIMENSION
_DIMENSION_PROBE_CHARS = 32 * 1024
_JPEG_SOF_MARKERS = frozenset({
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
})


def image_dimensions_from_bytes(raw: bytes) -> tuple[int, int] | None:
    """PNG/JPEG 头部尺寸；读不出返回 None（不整图解码）。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        width, height = struct.unpack(">II", raw[16:24])
        return int(width), int(height)
    if raw.startswith(b"\xff\xd8") and len(raw) > 4:
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 0xFF:
                i += 1
                continue
            marker, i = raw[i + 1], i + 2
            while marker == 0xFF and i < len(raw):
                marker, i = raw[i], i + 1
            if marker in {0xD8, 0xD9}:
                continue
            if marker == 0xDA or i + 2 > len(raw):
                break
            segment_len = int.from_bytes(raw[i:i + 2], "big")
            if segment_len < 2 or i + segment_len > len(raw):
                break
            if marker in _JPEG_SOF_MARKERS and segment_len >= 7:
                return (
                    int.from_bytes(raw[i + 5:i + 7], "big"),
                    int.from_bytes(raw[i + 3:i + 5], "big"),
                )
            i += segment_len
    return None


def image_long_side_from_base64(data_b64: str) -> int | None:
    """只解码 base64 前缀读图片头，返回长边；不可测返回 None。"""
    if not data_b64:
        return None
    prefix = data_b64[:_DIMENSION_PROBE_CHARS]
    prefix = prefix[: len(prefix) - (len(prefix) % 4)]
    if not prefix:
        return None
    try:
        raw = base64.b64decode(prefix, validate=False)
    except Exception:
        return None
    dims = image_dimensions_from_bytes(raw)
    return max(dims) if dims is not None else None


@dataclass
class IndexedImage:
    index: int  # 在 tool_result content 列表中的位置
    block: dict[str, Any]
    meta: dict[str, Any]
    byte_size: int


@dataclass
class ImageCarrier:
    tool_use_id: str
    parent: dict[str, Any]  # tool_result 块
    images: list[IndexedImage]
    byte_size: int

    @property
    def block_count(self) -> int:
        return len(self.images)


@dataclass
class ImageBudgetResult:
    """一次请求投影中的退役结果。

    ``newly_retired_ids`` 只含本次新退役的载体；已 latched 的载体只做静默替换，
    不重复计数、不重复提示。
    """

    newly_retired_ids: list[str] = field(default_factory=list)
    removed_paths: list[str] = field(default_factory=list)
    removed_images: int = 0


def collect_image_carriers(
    messages: list[dict[str, Any]],
) -> tuple[list[IndexedImage], list[ImageCarrier]]:
    """收集用户上传图（reserved）与含图 tool_result（carrier，按出现顺序）。"""
    reserved: list[IndexedImage] = []
    carriers: list[ImageCarrier] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "image":
                reserved.append(IndexedImage(
                    index=-1, block=block, meta=_block_meta(block),
                    byte_size=_base64_len(block),
                ))
                continue
            if btype != "tool_result":
                continue
            nested = block.get("content")
            if not isinstance(nested, list):
                continue
            parent_meta = _block_meta(block)
            images: list[IndexedImage] = []
            for idx, sub in enumerate(nested):
                if not isinstance(sub, dict) or sub.get("type") != "image":
                    continue
                meta = _merged_image_meta(sub, parent_meta, len(images))
                images.append(IndexedImage(
                    index=idx, block=sub, meta=meta, byte_size=_base64_len(sub),
                ))
            if images:
                carriers.append(ImageCarrier(
                    tool_use_id=str(
                        block.get("tool_use_id") or block.get("tool_call_id") or ""
                    ),
                    parent=block,
                    images=images,
                    byte_size=sum(img.byte_size for img in images),
                ))
    return reserved, carriers


def _image_long_side(meta: dict[str, Any], block: dict[str, Any]) -> int | None:
    width, height = meta.get("width"), meta.get("height")
    if (
        isinstance(width, int) and isinstance(height, int)
        and width > 0 and height > 0
    ):
        return max(width, height)
    source = block.get("source")
    data = source.get("data") if isinstance(source, dict) else None
    if not isinstance(data, str) or not data:
        return None
    return image_long_side_from_base64(data)


def _image_removed_block(block: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    path = str(meta.get("path") or "").strip()
    if path:
        text = (
            f'[image removed to save context: {path}; '
            f'use read_image("{path}") to view again]'
        )
    else:
        text = "[image removed to save context]"
    return {"type": "text", "id": block.get("id", ""), "text": text}


def _retire_carrier(
    carrier: ImageCarrier,
    result: ImageBudgetResult,
    *,
    count: bool,
) -> None:
    nested = carrier.parent.get("content")
    if not isinstance(nested, list):
        return
    # 复制一份再改，避免共享会话消息里的嵌套 content 列表被就地改写
    nested = list(nested)
    carrier.parent["content"] = nested
    for img in carrier.images:
        if img.index < len(nested):
            nested[img.index] = _image_removed_block(img.block, img.meta)
        if count:
            result.removed_images += 1
            path = str(img.meta.get("path") or "").strip()
            if path:
                result.removed_paths.append(path)


def apply_image_budget(
    messages: list[dict[str, Any]],
    *,
    max_images: int,
    hard_limit: int,
    max_bytes: int,
    eviction_batch: int = 4,
    eviction_floor: int = 3,
    retired_ids: set[str] | None = None,
) -> ImageBudgetResult:
    """按请求预算退役最旧的含图 tool_result（carrier）。

    - 用户上传图（reserved）计入上限与字节预算，但永不退役。
    - 退役以 carrier 为单位，成批步进（``eviction_batch``），最新
      ``eviction_floor`` 个载体保底。
    - ``retired_ids`` 为已 latched 的 tool_use_id：只静默替换，不重复计数。
    """
    reserved, carriers = collect_image_carriers(messages)
    result = ImageBudgetResult()
    latched = set(retired_ids or ())

    for carrier in carriers:
        if carrier.tool_use_id and carrier.tool_use_id in latched:
            _retire_carrier(carrier, result, count=False)

    active = [
        c for c in carriers
        if not (c.tool_use_id and c.tool_use_id in latched)
    ]
    if not active:
        return result

    # 条件上限：所有图片都可测且 ≤4096px 时用 hard_limit，否则用严格 20
    many_image_safe = True
    for img in reserved:
        side = _image_long_side(img.meta, img.block)
        if side is None or side > _MANY_IMAGE_DIMENSION_LIMIT:
            many_image_safe = False
            break
    if many_image_safe:
        for carrier in active:
            for img in carrier.images:
                side = _image_long_side(img.meta, img.block)
                if side is None or side > _MANY_IMAGE_DIMENSION_LIMIT:
                    many_image_safe = False
                    break
            if not many_image_safe:
                break

    limit = min(max_images, hard_limit) if many_image_safe else min(max_images, 20)
    reserved_blocks = len(reserved)
    reserved_bytes = sum(img.byte_size for img in reserved)

    newest = list(reversed(active))  # newest first
    total = len(newest)
    prefix_blocks = [0] * (total + 1)
    prefix_bytes = [0] * (total + 1)
    for i, carrier in enumerate(newest):
        prefix_blocks[i + 1] = prefix_blocks[i] + carrier.block_count
        prefix_bytes[i + 1] = prefix_bytes[i] + carrier.byte_size

    def fits(kept_count: int) -> bool:
        return (
            reserved_blocks + prefix_blocks[kept_count] <= limit
            and reserved_bytes + prefix_bytes[kept_count] <= max_bytes
        )

    if fits(total):
        return result

    floor = max(0, min(eviction_floor, total))
    quantum = max(1, eviction_batch)
    retire = 0
    if not fits(0):
        # reserved 自己就超限：退役无法修复，尽量保留 floor 个最新载体
        if reserved_bytes + prefix_bytes[floor] <= max_bytes:
            excess = total - floor
            retire = excess - excess % quantum
        else:
            retire = total
    else:
        window = max(k for k in range(total + 1) if fits(k))
        quantum = max(1, min(eviction_batch, max(1, window - floor)))
        while retire < total:
            retire = min(retire + quantum, total)
            if fits(total - retire):
                break

    if retire <= 0:
        return result

    for carrier in newest[total - retire:]:
        _retire_carrier(carrier, result, count=True)
        if carrier.tool_use_id:
            result.newly_retired_ids.append(carrier.tool_use_id)
    return result


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
    单图 base64 超过服务端上限的图片无条件降级（即使仍属当前回合）。
    请求级数量/字节预算由 :func:`apply_image_budget` 单独执行。
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
    # 单图 base64 超过服务端 5MB 上限：内联必然 400，无条件降级为引用。
    if _base64_len(block) > MAX_INLINE_BASE64_BYTES:
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


_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
}

# 各服务端都能直接内联的图片类型；其余（bmp/ico 等）必须转码后再发送
_INLINE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def _canonical_mime(mime: str) -> str:
    base = (mime or "").split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(base, base)


def _mime_for_format(fmt: str | None) -> str:
    if not fmt:
        return ""
    fmt = fmt.lower()
    if fmt in ("jpg", "jpeg"):
        return "image/jpeg"
    return f"image/{fmt}"


def normalize_image_bytes(
    data: bytes,
    media_type: str = "image/png",
    *,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    max_bytes: int = DEFAULT_MAX_BYTES,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> tuple[bytes, str, int, int] | None:
    """应用 EXIF 方向、缩放、编码到目标体积。

    已满足「尺寸 ≤ max_dimension 且原始字节 ≤ max_bytes 且无 EXIF 旋转」的图片
    原样返回（不重编码，保住画质与格式）；否则缩放到上限并用质量阶梯压到
    ``max_bytes`` 以内。

    Returns ``(bytes, media_type, width, height)``；Pillow 不可用或解码失败返回 None。
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as src:
            src_width, src_height = src.size
            orientation = src.getexif().get(0x0112, 1)
            detected_mime = _mime_for_format(src.format)
        source_mime = detected_mime or _canonical_mime(media_type) or "image/png"
        if (
            orientation in (None, 0, 1)
            and src_width <= max_dimension
            and src_height <= max_dimension
            and len(data) <= max_bytes
            and source_mime in _INLINE_MIME_TYPES
        ):
            return data, source_mime, src_width, src_height

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


def normalize_base64_image(
    data_b64: str,
    media_type: str = "image/png",
    *,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    max_bytes: int = DEFAULT_MAX_BYTES,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> tuple[str, str, bytes, int | None, int | None] | None:
    """base64 → 归一化 → base64。

    Returns ``(base64, media_type, payload, width, height)``；
    解码失败或 Pillow 不可用返回 None。
    """
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
    if normalized is None:
        return None
    payload, mime, width, height = normalized
    return base64.b64encode(payload).decode("ascii"), mime, payload, width, height


def normalize_image_block(
    block: Any,
    *,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    max_bytes: int = DEFAULT_MAX_BYTES,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> tuple[bytes, str, int | None, int | None] | None:
    """原地归一化 ImageBlock / dict 图片块，返回 ``(payload, mime, width, height)``。

    用于工具结果与请求投影：保证发往服务端的图片满足尺寸/体积上限。
    """
    if hasattr(block, "data"):
        data_b64 = str(getattr(block, "data", "") or "")
        media_type = str(getattr(block, "mime_type", "") or "image/png")
        is_object = True
    elif isinstance(block, dict):
        source = block.get("source")
        if not isinstance(source, dict):
            return None
        data_b64 = str(source.get("data") or "")
        media_type = str(source.get("media_type") or "image/png")
        is_object = False
    else:
        return None
    normalized = normalize_base64_image(
        data_b64, media_type,
        max_dimension=max_dimension, max_bytes=max_bytes, jpeg_quality=jpeg_quality,
    )
    if normalized is None:
        return None
    b64, mime, payload, width, height = normalized
    if is_object:
        block.data = b64
        block.mime_type = mime
    else:
        block["source"] = {"type": "base64", "media_type": mime, "data": b64}
    return payload, mime, width, height


def save_image_payload(
    payload: bytes,
    mime: str,
    uploads_dir: Path,
    *,
    workspace_dir: Path | None = None,
    prefix: str = "image",
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """把（已归一化的）图片字节写入 uploads；返回 path/media_type/尺寸/attachment_id。"""
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
    *,
    payload: bytes | None = None,
    mime: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any] | None:
    """把工具结果中的 ImageBlock 写成 sidecar 文件并返回引用元数据。

    ``payload`` 已由调用方归一化时直接复用，避免二次解码/重编码。
    """
    if payload is None:
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
    if not payload:
        return None
    mime = mime or "image/png"

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
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    max_bytes: int = DEFAULT_MAX_BYTES,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> dict[str, Any]:
    """补齐用户图片 block 的 id、turn_id、path，并把内联数据归一化。

    关键点：``source.data`` 必须替换为归一化后的 base64（≤4096px / 编码目标 2MiB），
    否则原始照片/截图会原样发给服务端导致 400。归一化只做一次（``_meta.normalized``）。
    ``path`` 缺失时把归一化结果写入 ``{cwd}/uploads``。
    """
    if not isinstance(block, dict) or block.get("type") != "image":
        return block
    meta = block.setdefault("_meta", {})
    if not block.get("id"):
        block["id"] = f"img_{secrets.token_hex(4)}"
    if turn_id:
        meta.setdefault("turn_id", turn_id)
    source = block.get("source")
    if not isinstance(source, dict):
        source = {}
        block["source"] = source
    media_type = str(source.get("media_type") or "image/png")
    meta.setdefault("media_type", media_type)

    data_b64 = source.get("data")
    payload: bytes | None = None
    if data_b64 and not meta.get("normalized"):
        normalized = normalize_base64_image(
            str(data_b64), media_type,
            max_dimension=max_dimension, max_bytes=max_bytes,
            jpeg_quality=jpeg_quality,
        )
        if normalized is not None:
            b64, mime, payload, width, height = normalized
            source["data"] = b64
            source["media_type"] = mime
            media_type = mime
            meta["media_type"] = mime
            if width is not None:
                meta["width"] = width
            if height is not None:
                meta["height"] = height
            meta["attachment_id"] = f"sha256:{hashlib.sha256(payload).hexdigest()}"
            meta["normalized"] = True

    if meta.get("path") or payload is None or not cwd:
        return block
    saved = save_image_payload(
        payload,
        media_type,
        Path(cwd) / "uploads",
        workspace_dir=Path(cwd),
        prefix="upload",
        width=meta.get("width"),
        height=meta.get("height"),
    )
    meta.update(saved)
    meta["normalized"] = True
    return block


def read_image_base64(
    item: "InboundMedia",
    *,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    max_bytes: int = DEFAULT_MAX_BYTES,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> tuple[str, str, bool]:
    """读图片为 base64；返回 (data, mime, 是否完成归一化)。"""
    try:
        raw = Path(item.path).read_bytes()
    except OSError as e:
        logger.warning(f"[multimodal] 读取图片失败 {item.path}: {e}")
        return "", "", False
    mime = item.content_type or mimetypes.guess_type(item.filename)[0] or "image/png"
    normalized = normalize_image_bytes(
        raw, mime,
        max_dimension=max_dimension, max_bytes=max_bytes, jpeg_quality=jpeg_quality,
    )
    if normalized is not None:
        payload, mime, _, _ = normalized
        return base64.b64encode(payload).decode("ascii"), mime, True
    return base64.b64encode(raw).decode("ascii"), mime, False
