"""Message compression utilities shared by the unified Agent class.

Provides functions to compress message histories to stay within context limits.
"""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# Placeholder for compacted tool results (injected when sending to LLM)
MICROCOMPACT_PLACEHOLDER = "[Compacted: content stored externally, use `read` tool to retrieve]"

# 仅允许安全字符，防止用 tool_use_id 拼文件名时路径遍历
_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def microcompact_tool_results(
    messages: list[dict],
    keep_recent: int = 6,
    output_dir: str | None = None,
) -> int:
    """标记旧的工具结果为已压缩。

    轻量压缩，不调用 LLM。保留最近 keep_recent 条工具结果消息，更早的：
    - 标记 _meta.compacted = True（消息级别）
    - 发送 LLM 时由 _resolve_tool_results() 注入占位符

    外部文件结果（file_size > 0）：内容已在文件里，仅标记 compacted。
    内联结果（file_size = 0）：内容只在消息内，压缩前先 spill 到
    {output_dir}/{tool_use_id}.txt 并写 _meta.output_path，再清 content，
    保证可经 read_tool_result 恢复；无 output_dir 时跳过（避免数据丢失）。

    向后兼容：检测旧格式的 block._compacted 并自动迁移。

    Args:
        messages: 消息列表（会被原地修改）
        keep_recent: 保留最近多少条工具结果消息
        output_dir: 内联结果 spill 的目标目录（session_dir）；为 None 时内联结果不压缩

    Returns:
        节省的字节数（估算）
    """
    saved = 0

    # 找到所有包含 tool_result 的 user 消息索引
    tool_result_msg_indices: list[int] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        if any(b.get("type") == "tool_result" for b in content):
            tool_result_msg_indices.append(i)

    # 保留最近 keep_recent 条，标记更早的
    if len(tool_result_msg_indices) <= keep_recent:
        return 0

    indices_to_compact = tool_result_msg_indices[:-keep_recent]

    for idx in indices_to_compact:
        msg = messages[idx]
        content = msg["content"]

        # 处理每个 tool_result block
        for block in content:
            if block.get("type") != "tool_result":
                continue

            # 从 block 级别的 _meta 读取
            block_meta = block.get("_meta", {})
            if block_meta.get("compacted", False):
                continue

            # 跳过小于 200 字符的工具结果（保留原文，不压缩）
            file_size = block_meta.get("file_size", 0)
            if file_size > 0 and file_size < 200:
                continue

            if "_meta" not in block:
                block["_meta"] = {}
            block_meta = block["_meta"]

            # 外部文件结果：内容已在文件里，仅标记
            if file_size > 0:
                block_meta["compacted"] = True
                saved += file_size
                continue

            # 内联结果：内容只在消息内，压缩前先 spill 到文件，保证可恢复。
            # 无 output_dir 或 tool_use_id 非法时跳过，避免不可恢复的数据丢失。
            tool_use_id = block.get("tool_use_id", "")
            content = block.get("content", "")
            if not output_dir or not _SAFE_ID_PATTERN.match(tool_use_id):
                continue
            if not isinstance(content, str) or not content:
                continue

            filename = f"{tool_use_id}.txt"
            try:
                file_path = os.path.join(output_dir, filename)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                logger.warning(f"[Microcompact] 内联结果 spill 失败，跳过压缩: {e}")
                continue

            block_meta["output_path"] = filename
            block_meta["compacted"] = True
            block["content"] = None  # 清空内联内容（_resolve_tool_results 注入占位符）
            saved += len(content)

    return saved


def count_tokens_approx(text: str) -> int:
    """估算文本的 token 数量（粗略近似）。

    中文约 2.5 字符/token，英文约 4 字符/token。
    """
    chinese_chars = sum(1 for c in text if '一' <= c <= '鿿')
    other_chars = len(text) - chinese_chars
    # 中文: ~2.5 chars/token, 英文: ~4 chars/token
    return int(chinese_chars / 2.5 + other_chars / 4)


def count_messages_tokens(messages: list[dict]) -> int:
    """估算消息列表的总 token 数量。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += count_tokens_approx(content)
        elif isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    total += count_tokens_approx(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    rc = block.get("content", "")
                    if isinstance(rc, list):
                        for sub in rc:
                            if sub.get("type") == "text":
                                total += count_tokens_approx(sub.get("text", ""))
                            elif sub.get("type") == "image":
                                # 图片约 750-1000 tokens
                                src = sub.get("source", {})
                                data = src.get("data", "") if isinstance(src, dict) else ""
                                total += max(750, len(data) // 100)
                    else:
                        total += count_tokens_approx(str(rc))
                elif block.get("type") == "tool_use":
                    total += count_tokens_approx(json.dumps(block.get("input", {}), ensure_ascii=False))
                elif block.get("type") in ("reasoning", "thinking"):
                    total += count_tokens_approx(block.get("thinking", "") or block.get("text", ""))
    return total


