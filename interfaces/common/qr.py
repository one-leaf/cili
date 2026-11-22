"""二维码渲染：内容串 → PNG data URL（QQ / 微信扫码流程共用）。

``qrcode`` 与 Pillow 都在 requirements 里（main.py 启动时自动装），但这里仍然
容错——扫码流程不该因为一个可选库缺失就整体不可用，调用方拿到空串时降级
展示原始内容串。
"""

from __future__ import annotations

import base64
import io
import logging

logger = logging.getLogger(__name__)


def qr_png_data_url(content: str) -> str:
    """把二维码内容渲染成 PNG data URL；``qrcode`` 不可用时返回空串。"""
    if not content:
        return ""
    try:
        import qrcode
    except ImportError:
        logger.warning("[qr] 未安装 qrcode，二维码降级为原始内容串")
        return ""
    try:
        image = qrcode.make(content)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 — 渲染失败不该让扫码流程不可用
        logger.exception("[qr] 生成二维码 PNG 失败")
        return ""
