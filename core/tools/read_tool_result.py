"""Tool for reading compacted tool results from external storage."""

import re

from core.tool_limits import READ_TOOL_RESULT_MAX_BYTES, READ_TOOL_RESULT_MAX_LIMIT
from core.tools.base import Tool, ToolResult


class ReadToolResultTool(Tool):
    """Retrieve compacted tool output from external storage."""

    name = "read_tool_result"
    concurrency_safe = True
    description = (
        "Retrieve the full text behind a truncated tool result by its tool_use_id. "
        "Use when a tool result says '[提示] 工具输出过长…已截断显示' (truncated). "
        "Large results support offset/limit pagination (character offsets); the reply "
        "tells you the next offset to call."
    )

    parameters = {
        "type": "object",
        "properties": {
            "tool_use_id": {
                "type": "string",
                "description": "The tool_use_id from the compacted result (e.g., 'toolu_01ABC123')",
            },
            "offset": {
                "type": "integer",
                "description": "Character offset to start from (default: 0).",
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Maximum characters to return (default: 4000, max: 20000). "
                    "Output is also capped by a byte budget so it stays inline."
                ),
            },
        },
        "required": ["tool_use_id"],
    }

    # 只允许安全字符，防止路径遍历
    _SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

    # 单次输出字节预算：低于 runner 的 10,000 字节外置阈值，避免取回的内容
    # 再次被截断落盘（否则 offset/limit 分页就失去意义）
    MAX_OUTPUT_BYTES = READ_TOOL_RESULT_MAX_BYTES
    DEFAULT_LIMIT = 4_000
    MAX_LIMIT = READ_TOOL_RESULT_MAX_LIMIT

    @staticmethod
    def _trim_to_bytes(text: str, budget: int) -> tuple[str, bool]:
        """按 UTF-8 字节预算截断（不切断多字节字符），返回 (text, trimmed)。"""
        if budget <= 0:
            return "", bool(text)
        if len(text.encode("utf-8")) <= budget:
            return text, False
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(text[:mid].encode("utf-8")) <= budget:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo], True

    def execute(self, tool_use_id: str, offset: int = 0, limit: int = DEFAULT_LIMIT,
                **kwargs) -> ToolResult:
        # 校验 tool_use_id，防止路径遍历
        if not self._SAFE_ID_PATTERN.match(tool_use_id):
            return ToolResult(f"Error: invalid tool_use_id format: {tool_use_id}", error=True)

        if not self.session:
            return ToolResult("Error: session manager not available", error=True)

        try:
            offset = int(offset or 0)
            limit = int(limit or self.DEFAULT_LIMIT)
        except (TypeError, ValueError):
            return ToolResult("Error: offset and limit must be integers", error=True)
        if offset < 0:
            return ToolResult("Error: offset must be >= 0", error=True)
        if limit <= 0:
            return ToolResult("Error: limit must be > 0", error=True)
        limit = min(limit, self.MAX_LIMIT)

        session_dir = self.session.session_dir

        filename = f"{tool_use_id}.txt"
        file_path = session_dir / filename

        if not file_path.exists():
            # Try agent execution directories
            for exec_dir in session_dir.glob("exec_*"):
                candidate = exec_dir / filename
                if candidate.exists():
                    file_path = candidate
                    break
            else:
                # M3 兼容：新路径在 cache/tool_output/{session|exec}/。
                from core.tool_output import find_tool_output_file
                found = find_tool_output_file(
                    session_dir,
                    filename,
                    workspace_uuid=getattr(self, "workspace_uuid", ""),
                    key=str(getattr(self.session, "session_id", "") or ""),
                )
                if found is None:
                    return ToolResult(f"Tool result file not found: {filename}", error=True)
                file_path = found

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(f"Error reading file: {e}", error=True)

        if not content:
            return ToolResult("(empty output)")

        total = len(content)
        if offset >= total:
            return ToolResult(
                f"Error: offset {offset} is beyond the end of the result "
                f"(total {total} characters).",
                error=True,
            )

        raw_chunk = content[offset:offset + limit]
        upper_end = offset + len(raw_chunk)

        # 先按「最长的页头/页脚」预留字节，再截正文，保证整段输出不超过预算
        # （否则页脚会被二次截断，模型就拿不到下一段 offset）
        header = f"[read_tool_result] {filename} | chars {offset}-{upper_end} of {total}"
        # 按最坏情况预留页脚（字节截断后一定需要分页指引）与 byte-trimmed 后缀
        reserve = len(header.encode("utf-8")) + 240
        budget = max(1, self.MAX_OUTPUT_BYTES - reserve)
        chunk, byte_trimmed = self._trim_to_bytes(raw_chunk, budget)
        end = offset + len(chunk)

        # 首屏完整覆盖且未被字节预算截断：保持原来的纯文本返回形态
        if offset == 0 and end >= total and not byte_trimmed:
            return ToolResult(chunk)

        header = f"[read_tool_result] {filename} | chars {offset}-{end} of {total}"
        if byte_trimmed:
            header += f" | byte-trimmed to {len(chunk.encode('utf-8'))}B"
        parts = [header, "", chunk]
        if end < total:
            parts.append("")
            parts.append(
                f"[分页] 还有 {total - end} 字符；继续读取请调用 "
                f'read_tool_result(tool_use_id="{tool_use_id}", offset={end})'
            )
        return ToolResult("\n".join(parts))
