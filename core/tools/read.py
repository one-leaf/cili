"""Read tool - read file contents."""

from __future__ import annotations

import itertools
import os

from core.image_read import IMAGE_EXTENSIONS, image_bytes_to_result, read_image_file
from core.tool_limits import READ_MAX_CHARS_PER_LINE, READ_MAX_OUTPUT_TOKENS
from core.tools.base import Tool, ToolResult


class ReadTool(Tool):
    name = "read"
    concurrency_safe = True
    description = (
        "Read file contents. Supports text files, images (PNG, JPG, GIF, WebP, BMP), and PDFs. "
        "Use offset/limit for pagination on large text files. "
        "Use pages (e.g. '3' or '1-5') for PDF page ranges (max 20 pages, default: page 1). "
        "Images are returned as base64-encoded data for the model to view. "
        "SVG files are returned as text (their source markup), not as rendered images. "
        "PDF pages without an extractable text layer are rendered as images."
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

    # Token 预算上限
    # 可通过环境变量 CILI_FILE_READ_MAX_OUTPUT_TOKENS 覆盖
    MAX_OUTPUT_TOKENS = READ_MAX_OUTPUT_TOKENS
    MAX_LINES_PER_READ = 2000   # 单次读取行数硬上限（不可配置，只能 offset+limit 分片）
    MAX_CHARS_PER_LINE = READ_MAX_CHARS_PER_LINE   # 单行字符上限，超限截断该行

    MAX_PAGES_PER_READ = 20   # PDF 单次读取页数上限
    PDF_READ_TIMEOUT = 60  # PDF 解析/取文本超时（秒），防损坏/超大 PDF 挂起；可用 CILI_PDF_READ_TIMEOUT 覆盖
    PDF_TEXT_FALLBACK_MIN_CHARS = 20   # 请求页文本总量低于此值视为无文本层，回退为页面渲染
    PDF_IMAGE_PAGES_PER_READ = 4   # 回退渲染时单次最多渲染的页数（图片比文本贵）
    PDF_RENDER_DPI = 150   # 页面渲染分辨率；再由图片归一化压到 4096px/2MiB 内

    def execute(
        self,
        file_path: str,
        offset: int | None = None,
        limit: int | None = None,
        pages: str | None = None,
    ) -> ToolResult:
        escape = self._relative_escape_guard(file_path)
        if escape is not None:
            return escape
        file_path = self._resolve_path(file_path)
        gate = self._read_gate([file_path])
        if gate is not None:
            return gate
        ext = os.path.splitext(file_path)[1].lower()

        # Handle PDFs
        if ext == ".pdf":
            return self._read_pdf(file_path, pages)

        # Handle images - return as multimodal content for vision models
        if ext in IMAGE_EXTENSIONS:
            if not os.path.isfile(file_path):
                return ToolResult(f"Error: file not found: {file_path}", error=True)
            return read_image_file(file_path)

        # Handle text files - direct I/O
        try:
            # First pass: count total lines efficiently
            total = 0
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for _ in f:
                    total += 1

            start = max(0, offset - 1) if offset else 0
            if offset and start >= total:
                return ToolResult(
                    f"Error: offset {offset} is beyond the end of file "
                    f"({total} lines)",
                    error=True,
                )
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
            max_tokens = int(os.environ.get("CILI_FILE_READ_MAX_OUTPUT_TOKENS", str(self.MAX_OUTPUT_TOKENS)))
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
        def _extract() -> str | ToolResult:
            with pdfplumber.open(file_path) as pdf:
                total_pages = len(pdf.pages)
                for p in page_list:
                    if p < 1 or p > total_pages:
                        raise ValueError(
                            f"Page {p} out of range (PDF has {total_pages} pages)."
                        )
                texts = [
                    (page_num, pdf.pages[page_num - 1].extract_text() or "")
                    for page_num in page_list
                ]

            # 扫描版 PDF：没有文本层时改为渲染页面；渲染失败则退回文本提示
            if sum(len(text.strip()) for _, text in texts) < self.PDF_TEXT_FALLBACK_MIN_CHARS:
                rendered = self._render_pdf_pages(file_path, page_list, total_pages)
                if rendered is not None:
                    return rendered

            return self._format_pdf_text(file_path, total_pages, texts)

        try:
            from concurrent.futures import ThreadPoolExecutor
            try:
                timeout = float(os.environ.get("CILI_PDF_READ_TIMEOUT", ""))
            except ValueError:
                timeout = float(self.PDF_READ_TIMEOUT)
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                future = pool.submit(_extract)
                result = future.result(timeout=timeout)
            finally:
                # wait=False：超时后不阻塞等待可能卡死的线程，让其随进程自然回收
                pool.shutdown(wait=False)

            if isinstance(result, ToolResult):
                return result

            # Token budget truncation
            max_tokens = int(os.environ.get("CILI_FILE_READ_MAX_OUTPUT_TOKENS", str(self.MAX_OUTPUT_TOKENS)))
            return ToolResult(self.truncate_middle(result, max_tokens))
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

    def _format_pdf_text(
        self, file_path: str, total_pages: int, texts: list[tuple[int, str]]
    ) -> str:
        """把逐页文本拼成工具输出；无文本页附上按页渲染的提示。"""
        parts = [
            f"PDF: {file_path} ({total_pages} pages, "
            f"showing pages: {', '.join(str(n) for n, _ in texts)})\n\n"
        ]
        blank_pages: list[int] = []
        for page_num, text in texts:
            if text.strip():
                parts.append(f"--- Page {page_num} ---\n{text}\n\n")
            else:
                blank_pages.append(page_num)
                parts.append(f"--- Page {page_num} ---\n(no text extracted)\n\n")
        if blank_pages:
            pages_arg = ", ".join(str(n) for n in blank_pages[:self.PDF_IMAGE_PAGES_PER_READ])
            parts.append(
                f"Page(s) {pages_arg} have no extractable text layer. "
                f'Re-read with pages="{blank_pages[0]}" to render them as image(s).'
            )
        return "".join(parts)

    def _render_pdf_pages(
        self, file_path: str, page_list: list[int], total_pages: int
    ) -> ToolResult | None:
        """扫描版 PDF 回退：用 PyMuPDF 渲染页面为图片。失败返回 None。"""
        try:
            import pymupdf
            from core.llm.types import TextBlock
        except ImportError:
            return None

        render_pages = page_list[:self.PDF_IMAGE_PAGES_PER_READ]
        try:
            doc = pymupdf.open(file_path)
            try:
                blocks: list = [TextBlock(text=(
                    f"PDF: {file_path} ({total_pages} pages; no extractable text layer)\n"
                    f"Rendered pages: {', '.join(str(n) for n in render_pages)}"
                    + (
                        f" (first {len(render_pages)} of {len(page_list)} requested pages)"
                        if len(page_list) > len(render_pages) else ""
                    )
                    + "\n\n"
                ))]
                for page_num in render_pages:
                    png = doc[page_num - 1].get_pixmap(dpi=self.PDF_RENDER_DPI).tobytes("png")
                    rendered = image_bytes_to_result(
                        f"{file_path} (page {page_num})", png, "image/png"
                    )
                    if rendered.is_error:
                        return None
                    blocks.extend(rendered.blocks)
                return ToolResult(blocks=blocks)
            finally:
                doc.close()
        except Exception:
            return None

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
