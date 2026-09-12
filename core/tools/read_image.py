"""ReadImageTool — 专用图片查看工具。

read 工具已内置图片读取，此工具提供语义更明确的独立入口，
实现复用 ReadTool.read_image()（缩放、格式转换、体积限制逻辑一致）。
"""

from __future__ import annotations

import os

from core.tools.base import Tool, ToolResult
from core.tools.read import ReadTool


class ReadImageTool(Tool):
    name = "read_image"
    description = (
        "View an image file and return it to the model for visual inspection. "
        "Supports PNG, JPG, JPEG, GIF, WebP, BMP. Large images are automatically "
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
        file_path = self._resolve_path(file_path, read_only=True)
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in ReadTool.IMAGE_EXTENSIONS:
            return ToolResult(
                f"Error: '{file_path}' is not a supported image type "
                f"(supported: {', '.join(sorted(ReadTool.IMAGE_EXTENSIONS))}).",
                error=True,
            )
        if not os.path.isfile(file_path):
            return ToolResult(f"Error: file not found: {file_path}", error=True)
        return ReadTool.read_image(file_path)
