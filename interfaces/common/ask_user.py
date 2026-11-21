"""QQ/微信共用的 ask_user 编号渲染与回映射。

渲染与回映射是**一对**：``render_questions`` 的编号必须和 ``resolve_answer``
的解析一致，所以放在同一模块。
"""

from __future__ import annotations


def questions_of(session, tool_use_id: str) -> list[dict]:
    """从会话里取某个 ask_user 工具调用的 questions。"""
    for msg in session.messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") in ("tool_use", "tool_call") and block.get("id") == tool_use_id:
                return list((block.get("input") or {}).get("questions") or [])
    return []


def resolve_answer(runner, content: str) -> str:
    """ask_user 用编号文本渲染；用户回 "1" 时映射回选项 label。

    审批卡的三档中文 label 也走这条路——``inject_ask_user_answer`` 靠
    ``endswith(APPROVE_LABEL/REMEMBER_LABEL)`` 判定，所以必须先还原成 label。

    **只在「恰好一个问题」时做映射**：多问题时编号在每个问题内重新开始，
    单个数字是歧义的——与其猜错，不如原样透传（模型会看到用户就回了 "1"，
    可以据此再问一次）。
    """
    if not content or not content.strip().isdigit():
        return content
    from core.approval_cards import find_pending_ask_user

    pending = find_pending_ask_user(runner.session)
    if not pending:
        return content
    questions = questions_of(runner.session, pending)
    if len(questions) != 1:
        return content
    idx = int(content.strip()) - 1
    options = questions[0].get("options") or []
    if 0 <= idx < len(options):
        return str(options[idx].get("label") or content)
    return content


def render_questions(tool_input: dict) -> str:
    """把 ask_user 的 questions 渲染成编号 markdown（用户回 "1"/"2" 即可）。"""
    questions = (tool_input or {}).get("questions") or []
    if not questions:
        return ""
    single = len(questions) == 1
    lines = ["", "---", ""]
    for qi, q in enumerate(questions, start=1):
        text = q.get("question") or ""
        lines.append(f"**{text}**" if single else f"**{qi}. {text}**")
        options = q.get("options") or []
        for oi, opt in enumerate(options, start=1):
            label = opt.get("label") or ""
            desc = opt.get("description") or ""
            lines.append(f"{oi}. {label} — {desc}" if desc else f"{oi}. {label}")
        lines.append("")
    if single:
        lines.append("_回复序号即可（如 `1`），也可直接输入其他内容。_")
    else:
        # 多问题时编号在每个问题内重新开始，单个数字有歧义——让用户按
        # 「问题序号: 选项」回答，inbound 侧只在单问题时才自动映射。
        lines.append("_请按 `问题序号: 选项序号` 回复（如 `1: 2`），也可直接输入其他内容。_")
    return "\n".join(lines) + "\n"
