"""Message compression utilities shared by RootAgent and SubAgent.

Provides functions to compress message histories to stay within context limits.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.llm import Message

logger = logging.getLogger(__name__)

# Placeholder for compacted tool results (injected when sending to LLM)
MICROCOMPACT_PLACEHOLDER = "[Compacted: content stored externally, use `read` tool to retrieve]"


def microcompact_tool_results(
    messages: list[dict],
    keep_recent: int = 6,
) -> int:
    """标记旧的工具结果为已压缩。

    轻量压缩，不调用 LLM。保留最近 keep_recent 条工具结果消息，更早的：
    - 标记 _compacted = True（不替换内容，内容已保存在外部文件）
    - 发送 LLM 时由 _resolve_tool_results() 注入占位符

    Args:
        messages: 消息列表（会被原地修改）
        keep_recent: 保留最近多少条工具结果消息

    Returns:
        节省的字节数（估算）
    """
    saved = 0

    # 找到所有包含 tool_result 的 user 消息索引
    tool_result_msg_indices: list[int] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        # 跳过 _subagent_ref 等内部消息
        if msg.get("role") == "_subagent_ref":
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
        for block in content:
            if block.get("type") != "tool_result":
                continue
            if block.get("_compacted"):
                continue

            # 跳过小于 200 字符的工具结果（保留原文，不压缩）
            file_size = block.get("_file_size", 0)
            if file_size > 0 and file_size < 200:
                continue

            # 只标记，不替换内容（内容已保存在外部文件）
            # 估算节省的字节数（基于 _file_size 或外部文件大小）
            if file_size > 0:
                saved += file_size
            block["_compacted"] = True

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
                                data = sub.get("source", {}).get("data", "")
                                total += max(750, len(data) // 100)
                    else:
                        total += count_tokens_approx(str(rc))
                elif block.get("type") == "tool_use":
                    total += count_tokens_approx(json.dumps(block.get("input", {}), ensure_ascii=False))
    return total


def summarize_messages_for_compact(
    messages: list[dict],
    llm_client: Any,
    max_chars: int = 50000,
) -> str:
    """使用 LLM 摘要消息列表。

    Args:
        messages: 要摘要的消息列表
        llm_client: LLM 客户端（需要有 chat 方法）
        max_chars: 最大字符数限制

    Returns:
        摘要文本
    """
    # 构建对话文本
    conversation_parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, str):
            conversation_parts.append(f"{role}: {content}")
        elif isinstance(content, list):
            texts = []
            for block in content:
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    texts.append(f"[调用工具: {block.get('name', '')}]")
                elif block.get("type") == "tool_result":
                    texts.append("[工具结果]")
            if texts:
                conversation_parts.append(f"{role}: {' '.join(texts)}")

    conversation_text = "\n".join(conversation_parts)

    # 截断过长内容
    if len(conversation_text) > max_chars:
        conversation_text = conversation_text[:max_chars] + "\n...(内容被截断)"

    summary_prompt = f"""请用中文简洁地总结以下对话的主要内容，包括：
1. 已完成的主要工作
2. 当前进行到哪一步
3. 关键的发现或决策

总结应简洁明了，200 字以内。

对话内容：
{conversation_text}

总结："""

    try:
        response = llm_client.chat(
            messages=[Message(role="user", content=summary_prompt)],
            system="你是一个对话总结助手，负责简洁地总结对话内容。",
        )
        # Use the typed get_text() method
        return response.get_text().strip()
    except Exception as e:
        logger.warning(f"[压缩] LLM 摘要失败: {e}")
        # 失败时返回简单的截断文本
        return conversation_text[:500] + "..."


def compact_messages_with_summary(
    messages: list[dict],
    llm_client: Any,
    keep_recent_count: int = 6,
) -> int:
    """使用 LLM 摘要进行完整压缩。

    保留最近 keep_recent_count 条消息，更早的消息被 LLM 摘要替换。

    Args:
        messages: 消息列表（会被原地修改）
        llm_client: LLM 客户端
        keep_recent_count: 保留最近多少条消息

    Returns:
        节省的 token 数（估算）
    """
    if len(messages) <= keep_recent_count + 1:
        # 消息太少，不需要压缩
        return 0

    # 分离要压缩和要保留的消息
    to_compact = messages[:-keep_recent_count]
    to_keep = messages[-keep_recent_count:]

    # 计算压缩前的 token 数
    tokens_before = count_messages_tokens(to_compact)

    # 生成摘要
    summary = summarize_messages_for_compact(to_compact, llm_client)

    # 替换消息列表
    messages.clear()
    messages.append({
        "role": "user",
        "content": f"[上下文压缩] 以下是之前对话的摘要：\n\n{summary}",
    })
    messages.append({
        "role": "assistant",
        "content": f"好的，我已经理解了之前的对话内容。以下是摘要：{summary}\n\n我将继续基于这个上下文完成任务。",
    })
    messages.extend(to_keep)

    # 计算压缩后的 token 数
    tokens_after = count_messages_tokens(messages)
    saved = tokens_before - tokens_after

    logger.info(f"[压缩] 完整压缩完成，节省约 {saved} tokens")
    return saved
