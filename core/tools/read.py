"""Read tool - read file contents."""

from __future__ import annotations

import base64
import io
import itertools
import os
import time

from core.tools.base import Tool, ToolResult


class ReadTool(Tool):
    name = "read"
    description = (
        "Read file contents. Supports text files, images (PNG, JPG, GIF, WebP), and PDFs. "
        "Use offset/limit for pagination on large text files. "
        "Use pages (e.g. '3' or '1-5') for PDF page ranges (max 20 pages, default: page 1). "
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
                "description": "Line number to start reading from (1-indexed). For text files only.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read. For text files only.",
            },
            "pages": {
                "type": "string",
                "description": "PDF page number or range, e.g. '3' or '1-5' or '1-3,7'. Max 20 pages. Default: page 1.",
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

    MAX_PAGES_PER_READ = 20   # PDF 单次读取页数上限
    MAX_RAW_IMAGE_BYTES = 5 * 1024 * 1024  # 无 PIL 时原始图片读取上限（5MB）
    PDF_READ_TIMEOUT = 60  # PDF 解析/取文本超时（秒），防损坏/超大 PDF 挂起；可用 CILI_PDF_READ_TIMEOUT 覆盖

    def execute(
        self,
        file_path: str,
        offset: int | None = None,
        limit: int | None = None,
        pages: str | None = None,
    ) -> ToolResult:
        file_path = self._resolve_path(file_path, read_only=True)
        ext = os.path.splitext(file_path)[1].lower()

        # Handle PDFs
        if ext == ".pdf":
            return self._read_pdf(file_path, pages)

        # Handle images - return as multimodal content for vision models
        if ext in self.IMAGE_EXTENSIONS:
            return ReadTool.read_image(file_path)

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

            # Touch memory knowledge files to update mtime (for access tracking)
            self._touch_memory_mtime(file_path)

            return ToolResult(output)
        except FileNotFoundError:
            return ToolResult(f"Error: file not found: {file_path}", error=True)
        except IsADirectoryError:
            return ToolResult(f"Error: not a file: {file_path}", error=True)
        except Exception as e:
            return ToolResult(f"Error reading file: {e}", error=True)

    @staticmethod
    def read_image(file_path: str) -> ToolResult:
        """Return an image as multimodal content for vision models.

        read 工具与 read_image 工具共用此实现：PIL 可用时自动缩放超过 16MP 的
        图片并转 PNG，无 PIL 时回退原始 base64（限制 5MB）。
        """
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
            # 编码后字节上限：PNG 对噪点图压缩率低，16MP 像素上限不足以保证
            # 体积，超大 base64 会撑爆 LLM 上下文（T24）
            if len(base64_data) > ReadTool.MAX_RAW_IMAGE_BYTES:
                return ToolResult(
                    output=(
                        f"Error: Image '{file_path}' encoded size {len(base64_data) / 1024 / 1024:.1f}MB "
                        f"exceeds the {ReadTool.MAX_RAW_IMAGE_BYTES // 1024 // 1024}MB limit. "
                        "Image too large to fit in LLM context."
                    ),
                    error=True,
                )
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
            # PIL not available, fallback to base64 without resize.
            # 无 PIL 时无法缩放，限制原始文件大小防止超大图片撑爆上下文
            file_size = os.path.getsize(file_path)
            if file_size > ReadTool.MAX_RAW_IMAGE_BYTES:
                return ToolResult(
                    output=(
                        f"Error: Image '{file_path}' is {file_size / 1024 / 1024:.1f}MB, "
                        f"exceeding the {ReadTool.MAX_RAW_IMAGE_BYTES // 1024 // 1024}MB limit for raw reads. "
                        f"Install Pillow to enable automatic downscaling, or convert the image to a smaller format."
                    ),
                    error=True,
                )
            with open(file_path, 'rb') as f:
                raw_data = f.read()
            base64_data = base64.b64encode(raw_data).decode('utf-8')
            media_type = ReadTool.MEDIA_TYPES.get(os.path.splitext(file_path)[1].lower(), "image/png")
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

    def _read_pdf(self, file_path: str, pages: str | None = None) -> ToolResult:
        """Read PDF file with page range support."""
        try:
            import pdfplumber
        except ImportError:
            return ToolResult(
                "Error: pdfplumber is not installed. Run: pip install pdfplumber",
                error=True,
            )

        try:
            page_list = self._parse_pages(pages)
        except ValueError as e:
            return ToolResult(f"Error: {e}", error=True)

        # 防御损坏/超大 PDF 导致 extract_text 无限挂起（A44 PDF timeout）：
        # 解析放进工作线程并限时，超时放弃该线程直接报错返回
        def _extract() -> str:
            with pdfplumber.open(file_path) as pdf:
                total_pages = len(pdf.pages)
                for p in page_list:
                    if p < 1 or p > total_pages:
                        raise ValueError(
                            f"Page {p} out of range (PDF has {total_pages} pages)."
                        )
                parts = [f"PDF: {file_path} ({total_pages} pages, showing pages: {', '.join(map(str, page_list))})\n\n"]
                for page_num in page_list:
                    page = pdf.pages[page_num - 1]  # 0-indexed
                    text = page.extract_text() or "(no text extracted)"
                    parts.append(f"--- Page {page_num} ---\n{text}\n\n")
                return "".join(parts)

        try:
            from concurrent.futures import ThreadPoolExecutor
            try:
                timeout = float(os.environ.get("CILI_PDF_READ_TIMEOUT", ""))
            except ValueError:
                timeout = float(self.PDF_READ_TIMEOUT)
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                future = pool.submit(_extract)
                output = future.result(timeout=timeout)
            finally:
                # wait=False：超时后不阻塞等待可能卡死的线程，让其随进程自然回收
                pool.shutdown(wait=False)

            # Token budget truncation
            import os as _os
            max_tokens = int(_os.environ.get("CILI_FILE_READ_MAX_OUTPUT_TOKENS", str(self.MAX_OUTPUT_TOKENS)))
            output = self.truncate_middle(output, max_tokens)

            return ToolResult(output)
        except ValueError as e:
            return ToolResult(f"Error: {e}", error=True)
        except TimeoutError:
            return ToolResult(
                f"Error: PDF extraction timed out (exceeded {timeout:.0f}s; "
                "possible corrupted or heavily scanned PDF). "
                "Try converting the PDF to text first.",
                error=True,
            )
        except Exception as e:
            return ToolResult(f"Error reading PDF: {e}", error=True)

    @staticmethod
    def _parse_pages(pages: str | None) -> list[int]:
        """Parse page specification like '3', '1-5', '1-3,7,9-11' into sorted list of page numbers.

        Returns list of 1-indexed page numbers.
        Default (None or empty) returns [1] (first page only).
        """
        if not pages or not pages.strip():
            return [1]

        result = set()
        for part in pages.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                bounds = part.split("-", 1)
                try:
                    start = int(bounds[0].strip())
                    end = int(bounds[1].strip())
                except ValueError:
                    raise ValueError(f"Invalid page range: '{part}'")
                if start < 1 or end < start:
                    raise ValueError(f"Invalid page range: '{part}' (must be start >= 1, end >= start)")
                # 先校验区间大小再展开，避免 `1-1000000000` 展开出数十亿整数导致 OOM
                if end - start + 1 > ReadTool.MAX_PAGES_PER_READ:
                    raise ValueError(f"Page range '{part}' too large. Maximum is {ReadTool.MAX_PAGES_PER_READ} per read.")
                result.update(range(start, end + 1))
            else:
                try:
                    page_num = int(part)
                except ValueError:
                    raise ValueError(f"Invalid page number: '{part}'")
                if page_num < 1:
                    raise ValueError(f"Page number must be >= 1, got {page_num}")
                result.add(page_num)

        if not result:
            return [1]

        page_list = sorted(result)
        if len(page_list) > ReadTool.MAX_PAGES_PER_READ:
            raise ValueError(f"Too many pages ({len(page_list)}). Maximum is {ReadTool.MAX_PAGES_PER_READ} per read.")
        return page_list

    def _touch_memory_mtime(self, file_path: str) -> None:
        """Update mtime of memory knowledge files to track last access time.

        Only touches files under memory/knowledge/ directory.
        Silently ignores errors (e.g. read-only filesystem).
        """
        normalized = file_path.replace("\\", "/")
        if "/memory/knowledge/" not in normalized:
            return
        try:
            now = time.time()
            os.utime(file_path, (now, now))
        except Exception:
            pass
