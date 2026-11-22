"""ReadImageTool — 专用图片查看工具。

实现共用 ``core/image_read.py``；read 工具与 read_image 工具走同一份代码。
"""

from __future__ import annotations

import os

from core.image_read import IMAGE_EXTENSIONS, read_image_file
from core.tools.base import Tool, ToolResult


class ReadImageTool(Tool):
    name = "read_image"
    concurrency_safe = True
    description = (
        "View an image file and return it to the model for visual inspection. "
        "Supports PNG, JPG, JPEG, GIF, WebP, BMP, SVG (SVG is rendered to PNG). "
        "Large images are automatically "
        "downscaled to fit the model's context. Use this for screenshots, diagrams, "
        "charts, photos, and other visual content."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the image file to view (relative or absolute).",
            },
        },
        "required": ["file_path"],
    }

    def execute(self, file_path: str) -> ToolResult:
        file_path = self._resolve_path(file_path)
        gate = self._read_gate([file_path])
        if gate is not None:
            return gate
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in IMAGE_EXTENSIONS:
            return ToolResult(
                f"Error: '{file_path}' is not a supported image type "
                f"(supported: {', '.join(sorted(IMAGE_EXTENSIONS))}).",
                error=True,
            )
        if not os.path.isfile(file_path):
            return ToolResult(f"Error: file not found: {file_path}", error=True)
        return read_image_file(file_path)
