"""输入框推荐输入（suggest）生成服务。

单路线：确保 runner（真实会话，下一轮对话直接复用它）→ 复制上一次主请求的
system/tools（没有就按 runner 同一装配现算）→ 复用 ``_prepare_messages_for_llm``
组装消息 + 末尾追加一条合成 user 消息 → 单次 ``chat()`` → 解析过滤 → 丢弃。

不做自建上下文、不做尾部截断；不写会话、不执行工具、不 claim 运行权。
设计见 workspace/default/cili-input-suggest-plan-2026-11-13.md §3.2 / §4。
"""

from __future__ import annotations

import logging

from core.compression import count_tokens_approx
from core.llm.client import _extract_json_dict
from core.llm.types import Message, TextBlock, ToolCallBlock, ToolResultBlock

logger = logging.getLogger(__name__)

_MAX_ITEMS = 4
_MIN_ITEMS = 2
_MAX_ITEM_CHARS = 30
_MIN_ITEM_CHARS = 4

# 模型若仍调用工具时的拒绝文案（轻量版 deny 回调）
_DENY_TEXT = "工具在建议生成中不可用。请直接输出 JSON 建议，不要调用任何工具。"
_STRICT_REMINDER = '请严格只输出 JSON：{"suggestions": ["..."]}，不要输出任何其他文字。'

# 申请者的「助手口吻」前缀：这些不是用户会输的话
_ASSISTANT_PREFIXES = (
    "让我", "我会", "我来", "我可以", "我先",
    "好的，我", "好的，让我", "正在", "已经",
)
# 纯评价语（整句匹配才丢，避免误伤「好的，继续」）
_EVALUATIVE_EXACT = {"谢谢", "多谢", "好的", "明白了", "收到", "很棒", "太棒了"}

# 空会话生成失败时的静态兜底（source=fallback）
_STATIC_FALLBACK = [
    "帮我了解这个工作区有什么",
    "根据项目说明给我一些上手建议",
    "列出现有文件并总结",
]


class SuggestError(Exception):
    """建议生成失败（路由层转 503）。"""


# ─── 入口 ────────────────────────────────────────────────────────────


def generate_suggestions(runner, refresh: bool = False,
                         exclude: list[str] | None = None) -> dict:
    """生成 3–4 条推荐输入。

    Args:
        runner: 会话的真实 SessionRunner（路由层经 ``_get_or_create_runner`` 获取，
            下一轮对话复用同一个）
        refresh: 换一批标记（本方案无结果缓存，仅透传记录）
        exclude: 上一批建议，用于换一批时去重

    Returns:
        {"suggestions": [...], "source": "context"|"cold_start"|"fallback",
         "route": "prefixed"|"fresh"}
    """
    if runner is None or getattr(runner, "client", None) is None:
        raise SuggestError("会话尚未就绪，请稍后再试")

    empty = _is_empty_session(runner)
    route = "fresh"
    try:
        system, tools, session_id, route = _select_prefix(runner)
        items = _request_suggestions(
            runner, system, tools, session_id,
            is_empty=empty, exclude=exclude, route=route,
        )
    except SuggestError:
        if empty:
            logger.warning("[suggest] 空会话生成失败，使用静态兜底")
            return {"suggestions": list(_STATIC_FALLBACK),
                    "source": "fallback", "route": route}
        raise
    except Exception as e:  # noqa: BLE001 —— 统一转成可读失败语义
        logger.warning("[suggest] 生成失败: %s", e)
        if empty:
            return {"suggestions": list(_STATIC_FALLBACK),
                    "source": "fallback", "route": route}
        raise SuggestError("生成建议失败，请稍后再试") from e

    return {
        "suggestions": items,
        "source": "cold_start" if empty else "context",
        "route": route,
    }


# ─── 前缀选择（捕获值优先，否则现算） ─────────────────────────────────


def _select_prefix(runner) -> tuple:
    """返回 (system, tools, session_id, route)。

    prefixed：上一次主请求的 system/tools 原样复用（逐字节保真，吃缓存）。
    fresh：按 runner 自己的装配现算（新会话 / 重启后首次 / runner 重建 / 换模型）。
    """
    prefix = getattr(runner, "_last_request_prefix", None)
    client = getattr(runner, "client", None)
    if isinstance(prefix, dict) and client is not None:
        model_ok = prefix.get("model") == getattr(client, "model_name", "")
        base_ok = (prefix.get("base_url") or "") == (getattr(client, "base_url", "") or "")
        if model_ok and base_ok and prefix.get("system") and isinstance(prefix.get("tools"), list):
            return (prefix["system"], prefix["tools"],
                    prefix.get("session_id") or "", "prefixed")

    system, tools = _render_fresh_prefix(runner)
    return system, tools, getattr(runner, "master_session_id", "") or "", "fresh"


def _render_fresh_prefix(runner) -> tuple:
    """按 runner 自己的装配现算 system/tools。

    回合结束后路由会 ``set_turn_interface(None)``，接口专属工具与 interface_caps
    段会变；这里临时切到 web（本端点仅 Web 调用）后立刻恢复，尽量与上一回合同形。
    runner 运行中不 mutate——宁可缓存 miss，也不破坏进行中的回合。
    """
    prev = getattr(runner, "_turn_interface", None)
    mutated = False
    if not getattr(runner, "_running", False) and prev != "web":
        try:
            runner.set_turn_interface("web")
            mutated = True
        except Exception:  # noqa: BLE001
            logger.debug("[suggest] set_turn_interface('web') 失败", exc_info=True)
    try:
        system = runner._build_system_prompt()
        tools = list(getattr(runner, "tool_schemas", []) or [])
    finally:
        if mutated:
            try:
                runner.set_turn_interface(prev)
            except Exception:  # noqa: BLE001
                logger.debug("[suggest] 恢复 turn_interface 失败", exc_info=True)
    return system, tools


# ─── 单次调用 ─────────────────────────────────────────────────────────


def _request_suggestions(runner, system, tools, session_id, *, is_empty: bool,
                         exclude: list[str] | None, route: str) -> list[str]:
    messages = list(runner._prepare_messages_for_llm(pad_dangling=False))
    if route == "fresh":
        _guard_context_size(runner, messages)
        _guard_dangling_tool_use(messages)
    _append_instruction(messages, _instruction(is_empty, exclude))

    client = runner.client
    response = client.chat(
        messages=messages,
        system=system,
        tools=tools,
        session_id=session_id,
    )
    _log_usage(route, response)

    items, response = _parse_or_retry(
        client, messages, response, system, tools, session_id, route,
    )

    filtered = _filter_items(items, exclude)
    if len(filtered) < _MIN_ITEMS and exclude:
        # 换一批时模型常给出与上一批几乎相同的建议，被 exclude 全数滤掉；
        # 带着明确的负例再要一次（硬上限一轮）
        filtered = _refresh_retry(
            client, messages, response, system, tools, session_id, route, exclude,
        )
    if len(filtered) < _MIN_ITEMS:
        logger.info("[suggest] 条数不足：raw=%r exclude=%r", items, exclude)
        raise SuggestError("没有更多新建议，请稍后再试")
    return filtered


def _parse_or_retry(client, messages, response, system, tools, session_id, route):
    """解析建议；失败时补一轮（deny 工具 / 严格 JSON），返回 (items, 最后一次响应)。"""
    items = _parse_items(response)
    if items is not None:
        return items, response
    tool_calls = [b for b in response.content if isinstance(b, ToolCallBlock)]
    if tool_calls:
        followup = [Message(role="assistant", content=response.content),
                    Message(role="user", content=[
                        ToolResultBlock(tool_use_id=b.id, content=_DENY_TEXT, is_error=True)
                        for b in tool_calls
                    ])]
        error_msg = "模型持续请求工具，未产出建议"
    else:
        followup = [Message(role="assistant", content=response.content),
                    Message(role="user", content=[TextBlock(text=_STRICT_REMINDER)])]
        error_msg = "解析建议失败"

    response = client.chat(
        messages=messages + followup,
        system=system,
        tools=tools,
        session_id=session_id,
    )
    _log_usage(route + "+retry", response)
    items = _parse_items(response)
    if items is None:
        raise SuggestError(error_msg)
    return items, response


def _refresh_retry(client, messages, response, system, tools, session_id,
                   route, exclude):
    """换一批时若新建议全被去重，带负例再要一轮；返回过滤后的结果（可能仍不足）。"""
    previous = "；".join(f"“{x}”" for x in exclude[:4])
    reminder = (
        f"你给出的建议与上一批重复（{previous}）。请换完全不同的方向和说法，"
        "输出 3–4 条新建议，不要与上一批相同或近义。"
        '只输出 JSON：{"suggestions": ["..."]}，不要输出任何其他文字。'
    )
    followup = [Message(role="assistant", content=response.content),
                Message(role="user", content=[TextBlock(text=reminder)])]
    response = client.chat(
        messages=messages + followup,
        system=system,
        tools=tools,
        session_id=session_id,
    )
    _log_usage(route + "+refresh", response)
    items = _parse_items(response)
    return _filter_items(items or [], exclude)


def _append_instruction(messages: list[Message], text: str) -> None:
    """在消息副本末尾放合成指令，保证角色交替。

    正常回合收尾最后一条是 assistant → 直接追加（保持与下一回合主请求的断点对齐）。
    空会话的 prepared 消息只有一条注入的 user（claude_md）→ 合并进该条，避免连续
    同角色消息（OpenAI/Bedrock 约束，主循环 ``assemble_context`` 同款处理）。
    """
    blocks = [TextBlock(text=text)]
    if messages and messages[-1].role == "user":
        last = messages[-1]
        content = last.content
        if isinstance(content, str):
            merged = [TextBlock(text=content)] + blocks
        else:
            merged = list(content) + blocks
        messages[-1] = Message(role="user", content=merged)
    else:
        messages.append(Message(role="user", content=blocks))


def _log_usage(route: str, response) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    logger.info(
        "[suggest] route=%s in=%s out=%s cache_read=%s cache_write=%s",
        route, usage.input_tokens, usage.output_tokens,
        usage.cache_read_tokens, usage.cache_write_tokens,
    )


def _instruction(is_empty: bool, exclude: list[str] | None = None) -> str:
    lines = [
        "[SUGGESTION MODE]",
        "你是这个编码会话的助手本身。请推测用户接下来最可能输入什么，"
        "给出 3–4 条可直接发送的候选消息。",
        "要求：中文；口语化、具体；每条 4–30 字；覆盖不同意图"
        "（继续推进 / 追问细节 / 换个方向）；不要复述已完成的内容；"
        "不要提问；不要评价；不要调用任何工具。",
    ]
    if exclude:
        previous = "；".join(f"“{x}”" for x in exclude[:4])
        lines.append(
            f"上一批建议：{previous}。请务必换不同的角度和说法，"
            "不要与它们相同或近义。"
        )
    if is_empty:
        lines.append("这是新会话，还没有任何对话，请给出冷启动建议。")
    lines.append('只输出 JSON：{"suggestions": ["..."]}，不要输出任何其他文字。')
    return "\n".join(lines)


# ─── 解析 / 过滤 ──────────────────────────────────────────────────────


def _parse_items(response) -> list[str] | None:
    texts = [b.text for b in response.content
             if isinstance(b, TextBlock) and b.text]
    if not texts:
        return None
    data = _extract_json_dict("\n".join(texts))
    if not isinstance(data, dict):
        return None
    raw = data.get("suggestions")
    if not isinstance(raw, list):
        return None
    items = [str(x).strip() for x in raw if str(x).strip()]
    return items or None


def _filter_items(items: list[str], exclude: list[str] | None) -> list[str]:
    """输出后置过滤（代码层，不只靠 prompt）。"""
    excluded = {_norm_key(x) for x in (exclude or []) if isinstance(x, str)}
    result: list[str] = []
    seen: set[str] = set()
    for raw in items:
        text = " ".join(str(raw).split())
        if not (_MIN_ITEM_CHARS <= len(text) <= _MAX_ITEM_CHARS):
            continue
        if "？" in text or "?" in text:
            continue
        if any(text.startswith(p) for p in _ASSISTANT_PREFIXES):
            continue
        if text.rstrip("。！!") in _EVALUATIVE_EXACT:
            continue
        # 单句：句号/感叹号只允许出现在末尾，分号一律拒绝
        if "；" in text or "\n" in text:
            continue
        if "。" in text[:-1] or "！" in text[:-1] or "!" in text[:-1]:
            continue
        key = _norm_key(text)
        if key in excluded or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= _MAX_ITEMS:
            break
    return result


def _norm_key(text: str) -> str:
    return "".join(str(text).split()).rstrip("。！!？?").lower()


# ─── 上下文规模 / 空会话判定 ───────────────────────────────────────────


def _guard_context_size(runner, messages: list[Message]) -> None:
    """fresh 路径下请求可能远超上下文（历史会话未压缩）。超限直接失败，不裁剪。"""
    max_ctx = int(getattr(getattr(runner, "model", None),
                          "max_context_tokens", 200000) or 200000)
    estimate = 0
    for msg in messages:
        content = msg.content
        if isinstance(content, str):
            estimate += count_tokens_approx(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, TextBlock):
                    estimate += count_tokens_approx(block.text)
    if estimate > max_ctx * 0.9:
        raise SuggestError("会话过长，请先发送一条消息后重试")


def _guard_dangling_tool_use(messages: list[Message]) -> None:
    """fresh 路径若存在未配对的 tool_use（崩溃/中断残留），请求会被 API 拒绝。

    不在建议生成里补占位（会写会话），直接提示用户先发一条消息让主循环修复。
    """
    pending: set[str] = set()
    for msg in messages:
        content = msg.content
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, ToolCallBlock):
                pending.add(block.id)
            elif isinstance(block, ToolResultBlock):
                pending.discard(block.tool_use_id)
    if pending:
        raise SuggestError("会话状态不完整，请先发送一条消息后重试")


def _is_empty_session(runner) -> bool:
    """会话是否为空（没有任何进入 LLM 上下文的消息）。"""
    try:
        from core.session_runner_runtime.context import ConversationStore

        messages = getattr(getattr(runner, "session", None), "messages", None) or []
        return not any(ConversationStore.is_message_in_context(m) for m in messages)
    except Exception:  # noqa: BLE001
        return False
