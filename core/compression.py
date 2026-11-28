"""消息压缩工具：microcompact 与 token 估算，供 SessionRunner 使用。

LLM 摘要（full compact / handoff）在 ``core/session_runner_runtime/executor.py``。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# ─── 压缩阈值与 token 估算常量 ────────────────────────────────────────
_ERROR_RESULT_KEEP_RECENT = 10  # 错误结果对保留最近 10 条
_IMAGE_BASE_TOKENS = 750  # 单张图片的基准 token 估算
_IMAGE_DATA_BYTES_PER_TOKEN = 100  # 图片 base64 数据每 100 字节折合 1 token
_CHINESE_CHARS_PER_TOKEN = 2.5  # 中文约 2.5 字符/token
_OTHER_CHARS_PER_TOKEN = 4  # 英文等其他约 4 字符/token

# Token 预算常量（4 字节 ≈ 1 token）
BYTES_PER_TOKEN = 4


def approx_token_count(text: str) -> int:
    """估算文本的 token 数（4 字节 ≈ 1 token）。"""
    byte_len = len(text.encode("utf-8", errors="replace"))
    return (byte_len + BYTES_PER_TOKEN - 1) // BYTES_PER_TOKEN


def truncate_middle(text: str, max_tokens: int) -> str:
    """Token 预算截断：保留开头和结尾，删除中间内容。

    - 若文本在预算内，直接返回
    - 否则保留前 40% + 后 40%，中间用标记替代
    """
    tokens = approx_token_count(text)
    if tokens <= max_tokens:
        return text

    if max_tokens <= 0:
        return f"\n\n…{tokens:,} tokens truncated…\n\n"

    max_bytes = max_tokens * BYTES_PER_TOKEN
    head_bytes = int(max_bytes * 0.4)
    tail_bytes = int(max_bytes * 0.4)

    head = text.encode("utf-8", errors="replace")[:head_bytes].decode("utf-8", errors="ignore")
    tail = text.encode("utf-8", errors="replace")[-tail_bytes:].decode("utf-8", errors="ignore")

    removed_tokens = tokens - max_tokens
    marker = f"\n\n…{removed_tokens:,} tokens truncated…\n\n"
    return head + marker + tail


def truncate_result(text: str, max_chars: int) -> str:
    """按字符数截断工具结果（保留开头，末尾加提示）。"""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_newline = truncated.rfind('\n')
    if last_newline > max_chars * 0.9:
        truncated = truncated[:last_newline]
    return truncated + f"\n\n... (truncated from {len(text):,} to {max_chars:,} chars)"


def truncate_tail(text: str, max_chars: int) -> str:
    """按字符数截断工具输出（保留尾部，开头加提示）。

    命令输出/测试日志的关键信息通常在末尾（错误、退出原因），shell 工具
    使用该函数替代 head 截断。
    """
    if len(text) <= max_chars:
        return text
    kept = text[-max_chars:]
    first_newline = kept.find("\n")
    if first_newline >= 0:
        kept = kept[first_newline + 1:]
    return (
        f"... (truncated from {len(text):,} to {len(kept):,} chars; showing tail)\n\n"
        f"{kept}"
    )


def _image_block_tokens(block: dict) -> int:
    """图片块的 token 估算：base64 数据按字节/token 折算，并保底 _IMAGE_BASE_TOKENS。"""
    src = block.get("source", {})
    data = src.get("data", "") if isinstance(src, dict) else ""
    return max(_IMAGE_BASE_TOKENS, len(data) // _IMAGE_DATA_BYTES_PER_TOKEN)


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

    invalidated_count += _enforce_tool_pairing(messages)
    return invalidated_count


def _invalidate(msg: dict) -> bool:
    if msg.get("_meta", {}).get("valid") is False:
        return False
    msg.setdefault("_meta", {})["valid"] = False
    return True


def _enforce_tool_pairing(messages: list[dict]) -> int:
    """保证 tool_use / tool_result 成对：孤立结果、缺失结果的 tool_use 整批失效。

    Anthropic 要求每个 tool_result 都能在前一条 assistant 消息找到 tool_use；
    多工具批量调用被部分失效时，必须整批剔除，否则请求会 400。
    """
    fixed = 0
    for _ in range(3):
        changed = 0
        valid_assistant: dict[int, set[str]] = {}
        valid_ids: set[str] = set()
        for i, msg in enumerate(messages):
            if msg.get("_meta", {}).get("valid") is False:
                continue
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            ids = {
                block.get("id")
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id")
            }
            if ids:
                valid_assistant[i] = ids
                valid_ids |= ids

        valid_results: dict[int, set[str]] = {}
        for i, msg in enumerate(messages):
            if msg.get("_meta", {}).get("valid") is False:
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            ids = {
                block.get("tool_use_id")
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id")
            }
            if not ids:
                continue
            if ids - valid_ids:
                if _invalidate(msg):
                    changed += 1
                continue
            valid_results[i] = ids

        if changed:
            fixed += changed
            continue

        covered: set[str] = set()
        for ids in valid_results.values():
            covered |= ids
        for idx, ids in valid_assistant.items():
            if ids <= covered:
                continue
            if _invalidate(messages[idx]):
                changed += 1
            for result_idx, result_ids in valid_results.items():
                if result_ids & ids:
                    if _invalidate(messages[result_idx]):
                        changed += 1
        fixed += changed
        if not changed:
            break
    return fixed


def _is_wide_char(c: str) -> bool:
    """是否按「宽字符」计价（约 2.5 字符/token）。

    覆盖 CJK 统一表意文字及其扩展、兼容表意文字、平假名/片假名、
    CJK 符号与标点、全角字符。此前只判 U+4E00–9FFF，中文标点与全角符号
    被当成 4 字符/token 的窄字符，整体低估。
    """
    o = ord(c)
    return (
        0x3000 <= o <= 0x303F      # CJK 符号与标点（。、「」等）
        or 0x3040 <= o <= 0x30FF   # 平假名 / 片假名
        or 0x3400 <= o <= 0x4DBF   # CJK 扩展 A
        or 0x4E00 <= o <= 0x9FFF   # CJK 统一表意文字
        or 0xF900 <= o <= 0xFAFF   # CJK 兼容表意文字
        or 0xFF00 <= o <= 0xFFEF   # 全角字符
    )


def count_tokens_approx(text: str) -> int:
    """估算文本的 token 数量（粗略近似）。

    中文约 2.5 字符/token，英文约 4 字符/token。
    """
    wide_chars = sum(1 for c in text if _is_wide_char(c))
    other_chars = len(text) - wide_chars
    return int(wide_chars / _CHINESE_CHARS_PER_TOKEN + other_chars / _OTHER_CHARS_PER_TOKEN)


def count_prompt_overhead(system_prompt: str, tool_schemas: list[dict] | None) -> dict[str, int]:
    """估算系统提示词 + 工具定义的 token 开销（与消息体分开统计）。"""
    system_tokens = count_tokens_approx(system_prompt or "")
    tool_tokens = (
        count_tokens_approx(json.dumps(tool_schemas, ensure_ascii=False))
        if tool_schemas else 0
    )
    return {
        "system_prompt_tokens": system_tokens,
        "tool_schema_tokens": tool_tokens,
        "overhead_tokens": system_tokens + tool_tokens,
    }


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
                                total += _image_block_tokens(sub)
                    else:
                        total += count_tokens_approx(str(rc))
                elif block.get("type") == "image":
                    # 顶层图片（用户上传的多模态消息）
                    total += _image_block_tokens(block)
                elif block.get("type") == "tool_use":
                    total += count_tokens_approx(json.dumps(block.get("input", {}), ensure_ascii=False))
                elif block.get("type") in ("reasoning", "thinking"):
                    total += count_tokens_approx(block.get("thinking", "") or block.get("text", ""))
    return total
