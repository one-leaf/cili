"""图片读取共享实现：归一化、SVG 光栅化、multimodal ToolResult。

read 工具与 read_image 工具共用；工具类定义在 ``core/tools/read_image.py``。
"""

from __future__ import annotations

import base64
import io
import os

from core.tools.base import ToolResult

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

def image_bytes_to_result(file_path: str, raw: bytes, media_type: str) -> ToolResult:
    """原始图片字节 → multimodal ToolResult。

    归一化策略与请求图目标对齐：≤4096px、编码目标 2MiB。未超限的图片
    原样返回（不重编码）；超限的自动缩放/转码。Pillow 不可用时只能
    按 base64 体积兜底，超限则明确报错（发出去必然 400）。
    """
    from core.image_pipeline import MAX_INLINE_BASE64_BYTES, normalize_image_bytes

    normalized = normalize_image_bytes(raw, media_type)
    if normalized is not None:
        payload, mime, _width, _height = normalized
    else:
        # None 有两种原因：Pillow 不可用（只能按体积兜底）或图片损坏（明确报错）
        try:
            from PIL import Image as _Image

            with _Image.open(io.BytesIO(raw)) as probe:
                probe.verify()
        except ImportError:
            pass
        except Exception as e:  # noqa: BLE001 —— 损坏图片给出可读错误
            return ToolResult(
                f"Error: Failed to process image '{file_path}': {e}", error=True
            )
        payload, mime = raw, media_type or "image/png"

    base64_data = base64.b64encode(payload).decode("utf-8")
    if len(base64_data) > MAX_INLINE_BASE64_BYTES:
        return ToolResult(
            output=(
                f"Error: Image '{file_path}' base64 size "
                f"{len(base64_data) / 1024 / 1024:.1f}MB exceeds the "
                f"{MAX_INLINE_BASE64_BYTES // 1024 // 1024}MB service limit "
                "and could not be resized (install Pillow for automatic downscaling)."
            ),
            error=True,
        )

    return ToolResult(
        output=f"[Image: {file_path} ({mime})]",
        content=[{
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": base64_data,
            }
        }]
    )

def read_svg(file_path: str) -> ToolResult:
    """SVG → PNG 光栅化后按位图处理。

    接口层不收 image/svg+xml（Anthropic/OpenAI 只认 jpeg/png/gif/webp），
    所以必须先用 PyMuPDF 渲染成位图，再走与普通图片同一条编码路径。
    """
    try:
        import pymupdf
    except ImportError:
        return ToolResult(
            "Error: reading SVG requires PyMuPDF (pip install pymupdf).",
            error=True,
        )
    try:
        with open(file_path, "rb") as f:
            data = f.read()
        doc = pymupdf.open(stream=data, filetype="svg")
        try:
            png_bytes = doc[0].get_pixmap().tobytes("png")
        finally:
            doc.close()
    except Exception as e:
        return ToolResult(
            f"Error: failed to render SVG '{file_path}': {e}",
            error=True,
        )
    return image_bytes_to_result(file_path, png_bytes, "image/png")

def read_image_file(file_path: str) -> ToolResult:
    """Return an image as multimodal content for vision models.

    read 工具与 read_image 工具共用此实现：SVG 先用 PyMuPDF 光栅化成位图；
    位图交给 ``normalize_image_bytes`` 统一归一化（EXIF、≤4096px、
    base64 ≤5MB，未超限不重编码）。
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".svg":
        return read_svg(file_path)
    media_type = MEDIA_TYPES.get(ext, "image/png")
    try:
        with open(file_path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return ToolResult(f"Error: Failed to read image '{file_path}': {e}", error=True)
    return image_bytes_to_result(file_path, raw, media_type)
