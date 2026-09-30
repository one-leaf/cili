"""Message compression utilities shared by the unified Agent class.

Provides functions to compress message histories to stay within context limits.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# ─── 压缩阈值与 token 估算常量 ────────────────────────────────────────
_ERROR_RESULT_KEEP_RECENT = 10  # 错误结果对保留最近 10 条
_IMAGE_KEEP_RECENT = 3  # Emergency 层保留最近 3 张图片
_IMAGE_BASE_TOKENS = 750  # 单张图片的基准 token 估算
_IMAGE_DATA_BYTES_PER_TOKEN = 100  # 图片 base64 数据每 100 字节折合 1 token
_CHINESE_CHARS_PER_TOKEN = 2.5  # 中文约 2.5 字符/token
_OTHER_CHARS_PER_TOKEN = 4  # 英文等其他约 4 字符/token


def microcompact_mark_orphans_and_errors(
    messages: list[dict],
) -> int:
    """标记孤立 tool_result 和旧错误结果对为无效。

    轻量压缩，不调用 LLM。

    任务 1 - 孤立 tool_result 标记：
    扫描所有 role=user 消息中的 tool_result 块，若其 tool_use_id 在有效消息中
    找不到对应的 tool_use，则将该 user 消息标记为 _meta.valid=False。
    孤立检测依赖前序 Layer 2/3 已标记的消息（跨轮生效）。

    任务 2 - 错误结果对标记：
    统计所有含 is_error=True 的 tool_result，保留最近 ERROR_RESULT_KEEP_RECENT 条，
    更早的错误结果对（包含 tool_use 的 assistant 消息 + 包含 tool_result 的 user 消息）
    整对标记为 _meta.valid=False。

    Args:
        messages: 消息列表（会被原地修改）

    Returns:
        标记失效的消息数
    """
    invalidated_count = 0

    # ─── 任务 1：孤立 tool_result 标记 ─────────────────────────────────
    # 收集所有有效消息中的 tool_use_id
    valid_tool_use_ids: set[str] = set()
    for msg in messages:
        # 跳过已标记失效的消息
        if msg.get("_meta", {}).get("valid") is False:
            continue
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                tool_id = block.get("id", "")
                if tool_id:
                    valid_tool_use_ids.add(tool_id)

    # 检查所有 user 消息中的 tool_result，标记孤立的
    for msg in messages:
        if msg.get("_meta", {}).get("valid") is False:
            continue  # 已标记失效，跳过
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue

        has_orphan = False
        for block in content:
            if block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id", "")
            if tool_use_id and tool_use_id not in valid_tool_use_ids:
                has_orphan = True
                break

        if has_orphan:
            if "_meta" not in msg:
                msg["_meta"] = {}
            msg["_meta"]["valid"] = False
            invalidated_count += 1

    # ─── 任务 2：错误结果对标记 ─────────────────────────────────────────
    # 收集所有错误 tool_result 及其对应的 tool_use 消息索引
    error_pairs: list[tuple[int, int]] = []  # (tool_use_msg_idx, tool_result_msg_idx)

    # 先建立 tool_use_id → msg_idx 的映射（只遍历有效 assistant 消息）
    tool_use_id_to_msg_idx: dict[str, int] = {}
    for i, msg in enumerate(messages):
        if msg.get("_meta", {}).get("valid") is False:
            continue
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                tool_id = block.get("id", "")
                if tool_id:
                    tool_use_id_to_msg_idx[tool_id] = i

    # 再遍历 user 消息找错误结果
    for i, msg in enumerate(messages):
        if msg.get("_meta", {}).get("valid") is False:
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_result":
                continue
            if not block.get("is_error"):
                continue
            tool_use_id = block.get("tool_use_id", "")
            if tool_use_id in tool_use_id_to_msg_idx:
                tool_use_idx = tool_use_id_to_msg_idx[tool_use_id]
                error_pairs.append((tool_use_idx, i))

    # 保留最近 ERROR_RESULT_KEEP_RECENT 条，标记更早的
    if len(error_pairs) > _ERROR_RESULT_KEEP_RECENT:
        pairs_to_invalidate = error_pairs[:-_ERROR_RESULT_KEEP_RECENT]
        for tool_use_idx, tool_result_idx in pairs_to_invalidate:
            # 标记 assistant 消息（tool_use）
            msg_use = messages[tool_use_idx]
            if msg_use.get("_meta", {}).get("valid") is not False:
                if "_meta" not in msg_use:
                    msg_use["_meta"] = {}
                msg_use["_meta"]["valid"] = False
                invalidated_count += 1

            # 标记 user 消息（tool_result）
            msg_result = messages[tool_result_idx]
            if msg_result.get("_meta", {}).get("valid") is not False:
                if "_meta" not in msg_result:
                    msg_result["_meta"] = {}
                msg_result["_meta"]["valid"] = False
                invalidated_count += 1

    return invalidated_count


def count_tokens_approx(text: str) -> int:
    """估算文本的 token 数量（粗略近似）。

    中文约 2.5 字符/token，英文约 4 字符/token。
    """
    chinese_chars = sum(1 for c in text if '一' <= c <= '鿿')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / _CHINESE_CHARS_PER_TOKEN + other_chars / _OTHER_CHARS_PER_TOKEN)


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
                                # 图片约 _IMAGE_BASE_TOKENS+ tokens
                                src = sub.get("source", {})
                                data = src.get("data", "") if isinstance(src, dict) else ""
                                total += max(_IMAGE_BASE_TOKENS, len(data) // _IMAGE_DATA_BYTES_PER_TOKEN)
                    else:
                        total += count_tokens_approx(str(rc))
                elif block.get("type") == "tool_use":
                    total += count_tokens_approx(json.dumps(block.get("input", {}), ensure_ascii=False))
                elif block.get("type") in ("reasoning", "thinking"):
                    total += count_tokens_approx(block.get("thinking", "") or block.get("text", ""))
    return total
