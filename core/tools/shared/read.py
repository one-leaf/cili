"""Read tool - read file contents."""

from __future__ import annotations

import base64
import io
import itertools
import os

from core.tools.shared.base import Tool, ToolResult


class ReadTool(Tool):
    name = "read"
    description = (
        "Read file contents. Supports text files and images (PNG, JPG, GIF, WebP). "
        "Use offset/limit for pagination on large files. "
        "Images are returned as base64-encoded data for the model to view."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to read (relative or absolute).",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (1-indexed).",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read.",
            },
        },
        "required": ["file_path"],
    }

    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    MEDIA_TYPES = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }

    # Token 预算上限
    # 可通过环境变量 CILI_FILE_READ_MAX_OUTPUT_TOKENS 覆盖
    MAX_OUTPUT_TOKENS = 10_000
    MAX_LINES_PER_READ = 2000   # 单次读取行数硬上限（不可配置，只能 offset+limit 分片）
    MAX_CHARS_PER_LINE = 2000   # 单行字符上限，超限截断该行

    def execute(self, file_path: str, offset: int | None = None, limit: int | None = None) -> ToolResult:
        file_path = self._resolve_path(file_path)
        ext = os.path.splitext(file_path)[1].lower()

        # Handle images - return as multimodal content for vision models
        if ext in self.IMAGE_EXTENSIONS:
            max_pixels = 16_000_000  # 16 megapixels
            try:
                from PIL import Image

                with Image.open(file_path) as img:
                    width, height = img.size
                    current_pixels = width * height

                    if current_pixels > max_pixels:
                        scale = (max_pixels / current_pixels) ** 0.5
                        new_width = int(width * scale)
                        new_height = int(height * scale)
                        img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

                    if img.mode in ('RGBA', 'P', 'LA'):
                        pass
                    elif img.mode != 'RGB':
                        img = img.convert('RGB')

                    img_byte_arr = io.BytesIO()
                    img.save(img_byte_arr, format='PNG')
                    img_bytes = img_byte_arr.getvalue()

                base64_data = base64.b64encode(img_bytes).decode('utf-8')
                media_type = 'image/png'

                return ToolResult(
                    output=f"[Image: {file_path} ({media_type})]",
                    content=[{
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64_data,
                        }
                    }]
                )
            except ImportError:
                # PIL not available, fallback to base64 without resize
                with open(file_path, 'rb') as f:
                    raw_data = f.read()
                base64_data = base64.b64encode(raw_data).decode('utf-8')
                media_type = self.MEDIA_TYPES.get(ext, "image/png")
                return ToolResult(
                    output=f"[Image: {file_path} ({media_type})]",
                    content=[{
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64_data,
                        }
                    }]
                )
            except Exception as e:
                # PIL 异常（如损坏图片 UnidentifiedImageError, OSError 等）
                return ToolResult(
                    output=f"Error: Failed to process image '{file_path}': {e}",
                    error=True
                )

        # Handle text files - direct I/O
        try:
            # First pass: count total lines efficiently
            total = 0
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for _ in f:
                    total += 1

            start = max(0, offset - 1) if offset else 0
            end = min(total, start + (limit or self.MAX_LINES_PER_READ))

            # Second pass: read only the requested slice using islice
            parts = [f"File: {file_path} ({total} lines, showing {start+1}-{end})\n\n"]
            truncated_lines = 0
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(itertools.islice(f, start, end), start=start+1):
                    if len(line) > self.MAX_CHARS_PER_LINE:
                        line = line[:self.MAX_CHARS_PER_LINE] + "...(truncated)\n"
                        truncated_lines += 1
                    parts.append(f"{i:>6}\t{line}")

            # 提示有多少行被截断
            if truncated_lines > 0:
                parts.append(f"\n\n({truncated_lines} line(s) truncated, each line max {self.MAX_CHARS_PER_LINE} chars)\n")

            # 提示还有更多行未读
            if end < total:
                remaining = total - end
                parts.append(f"\n\n... ({remaining:,} more lines). Use offset={end+1} to continue reading.\n")

            output = "".join(parts)

            # Token 预算截断（保留开头+结尾，中间用标记替代）
            import os as _os
            max_tokens = int(_os.environ.get("CILI_FILE_READ_MAX_OUTPUT_TOKENS", str(self.MAX_OUTPUT_TOKENS)))
            output = self.truncate_middle(output, max_tokens)

            # 如果仍然超长（极端情况），再按字符截断
            max_chars = max_tokens * self.BYTES_PER_TOKEN
            if len(output) > max_chars:
                truncated = output[:max_chars]
                last_newline = truncated.rfind('\n')
                if last_newline > max_chars * 0.9:
                    truncated = truncated[:last_newline]
                lines_shown = truncated.count('\n') - 1
                output = truncated + f"\n\n... (truncated from {len(output):,} to {max_chars:,} chars, ~{lines_shown} lines shown)"

            return ToolResult(output)
        except FileNotFoundError:
            return ToolResult(f"Error: file not found: {file_path}", error=True)
        except IsADirectoryError:
            return ToolResult(f"Error: not a file: {file_path}", error=True)
        except Exception as e:
            return ToolResult(f"Error reading file: {e}", error=True)
