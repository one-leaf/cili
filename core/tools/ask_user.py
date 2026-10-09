"""AskUserQuestion tool — Agent 主动向用户提问，收集决策信息。

设计：工具返回 completed=False 的 ToolResult，Agent 循环退出。
前端检测到 ask_user 后渲染选择面板，用户提交后作为新 user message 继续循环。
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Any

from core.tools.approval import APPROVE_LABEL, REMEMBER_LABEL
from core.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


class AskUserTool(Tool):
    """主动向用户提问，支持多选和自由输入。"""

    name = "ask_user"
    description = (
        "Ask the user one or more multiple-choice questions to gather information, "
        "clarify ambiguity, understand preferences, or make decisions. "
        "Use this tool when you need user input before proceeding. "
        "Each question supports 2-6 options plus a free-text 'Other' input."
    )
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "description": "Questions to ask the user (1-4 questions).",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The complete question text, ending with a question mark.",
                        },
                        "header": {
                            "type": "string",
                            "description": "Short label (max 12 chars) shown as a tag, e.g. 'Auth method', 'Library'.",
                        },
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 6,
                            "description": "Available choices (2-6). No 'Other' needed — added automatically.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "Display text for the option (1-5 words).",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "Explanation of what this option means or its tradeoffs.",
                                    },
                                },
                                "required": ["label", "description"],
                            },
                        },
                        "multi_select": {
                            "type": "boolean",
                            "description": "Set to true to allow selecting multiple options. Default: false.",
                        },
                    },
                    "required": ["question", "header", "options"],
                },
            }
        },
        "required": ["questions"],
    }

    def execute(self, **kwargs: Any) -> ToolResult:
        questions = kwargs.get("questions", [])
        if not questions:
            return ToolResult("Error: at least one question is required", error=True)

        # Return with completed=False to exit agent loop
        # Frontend will render question card, user submits as new message
        return ToolResult(
            output="Waiting for user input...",
            completed=False,
        )


# ─── ask_user 占位符流程（接口无关：Web / QQ 等接入端共用） ───────────────
# 占位符由 AskUserTool.execute 返回 completed=False 的 ToolResult 生成；接入端渲染
# 问题卡，用户提交后经 inject_ask_user_answer 注入答案并恢复 runner 循环。
# 原先这些逻辑住在 web/routes_ask_user.py，第二个接入端只能复制一份。

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _safe_answer_filename(tool_use_id: str) -> str:
    """ask_user 答案文件的净名（非法 tool_use_id 回退随机名，防路径穿越）。"""
    if tool_use_id and _SAFE_ID_RE.match(tool_use_id):
        return f"{tool_use_id}.txt"
    return f"{secrets.token_hex(4)}.txt"


def find_pending_ask_user(session) -> str | None:
    """查找最后一个待回答的 ask_user 占位 tool_result，返回其 tool_use_id（无则 None）。

    占位符由 _execute_tool 生成：user 消息中的 tool_result 块带
    ``_meta.completed=False`` 且 ``_meta.tool_name="ask_user"``。
    """
    for msg in reversed(session.messages):
        if msg["role"] != "user":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_result":
                meta = block.get("_meta") or {}
                if meta.get("completed") is False and meta.get("tool_name") == "ask_user":
                    return block.get("tool_use_id") or block.get("tool_call_id")
    return None


def build_other_answer(session, ask_user_tool_use_id: str, content: str) -> str:
    """以用户输入作为 ask_user 的「其他」回复，按卡片 formatAnswers 格式组装（`问题 答案`）。"""
    for msg in session.messages:
        if msg["role"] != "assistant":
            continue
        blocks = msg.get("content", [])
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if block.get("type") in ("tool_use", "tool_call") and block.get("id") == ask_user_tool_use_id:
                questions = (block.get("input") or {}).get("questions", [])
                parts = [f"{q.get('question', '')} {content}" for q in questions if q.get("question")]
                return "\n".join(parts) if parts else content
    return content


def inject_ask_user_answer(session, ask_user_tool_use_id: str, answer: str,
                           approval_store=None) -> bool:
    """把用户答案注入 ask_user 占位 tool_result 并标记 answered/approval 后持久化。

    返回是否找到占位符。消息块是 runner 与 session 的共享引用，就地修改对两者都生效
    （answer_ask_user 与 send_message 共用）。
    """
    logger.info(f"[ask-user] 注入答案: tool_use_id={ask_user_tool_use_id}")
    found_placeholder = False
    for msg in reversed(session.messages):
        if msg["role"] != "user":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            # 检查两种字段名（tool_use_id 或 tool_call_id）
            block_tool_id = block.get("tool_use_id") or block.get("tool_call_id")
            if block.get("type") == "tool_result" and block_tool_id == ask_user_tool_use_id:
                # 替换占位符内容（内存视图；jsonl 行保持占位）
                block["content"] = answer
                found_placeholder = True
                logger.info(f"[ask-user] 找到并替换 tool_result: tool_use_id={ask_user_tool_use_id}")

                # 总是写答案到外部文件并置 completed/output_path/file_size：
                # 消息已有 seq 不会被 jsonl 重写，reload 时模型/UI 从文件恢复答案
                output_path = _safe_answer_filename(ask_user_tool_use_id)
                if "_meta" not in block:
                    block["_meta"] = {}
                block["_meta"].update({
                    "completed": True,
                    "output_path": output_path,
                    "file_size": len(answer.encode("utf-8")),
                })
                try:
                    ext_file = session.session_dir / output_path
                    ext_file.write_text(answer, encoding="utf-8")
                    logger.info(f"[ask-user] 已写入答案文件: {output_path}")
                except Exception as e:
                    logger.warning(f"[ask-user] 写入答案文件失败: {e}")
                break
        if found_placeholder:
            break

    if not found_placeholder:
        logger.error(f"[ask-user] 未找到占位符 tool_result: tool_use_id={ask_user_tool_use_id}")
        return False

    # 在对应的 tool_use/tool_call 块上添加 _answered 标记
    found_tool_use = False
    for msg in session.messages:
        if msg["role"] != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            # 同时检查 tool_use 和 tool_call 两种类型
            if block.get("type") in ("tool_use", "tool_call") and block.get("id") == ask_user_tool_use_id:
                if "_meta" not in block:
                    block["_meta"] = {}
                block["_meta"]["answered"] = True
                found_tool_use = True
                logger.info(f"[ask-user] 在 {block.get('type')} 上添加 _meta.answered=true: id={ask_user_tool_use_id}")
                break
        if found_tool_use:
            break

    if not found_tool_use:
        logger.warning(f"[ask-user] 未找到对应的 tool_use/tool_call: id={ask_user_tool_use_id}")

    # 会话级审批：若本次 ask_user 是高风险命令批准卡，按答案记录批准/拒绝并清空待批槽
    # 答案格式为 "{question} {label}"，用 label 后缀精确匹配区分三档（避免子串误判）
    store = approval_store
    if store and store.pending:
        stripped = answer.rstrip()
        kind = store.pending.get("kind", "command")
        if stripped.endswith(REMEMBER_LABEL):
            store.approve(
                store.pending["decision_id"],
                store.pending["command"],
                persist=True,
                reason=store.pending.get("reason", ""),
                kind=kind,
            )
            logger.info(f"[approval] 用户批准并记住高风险命令: {store.pending['command']}")
        elif stripped.endswith(APPROVE_LABEL):
            store.approve(store.pending["decision_id"], store.pending["command"], kind=kind)
            logger.info(f"[approval] 用户批准高风险命令(本次会话): {store.pending['command']}")
        else:
            logger.info(f"[approval] 用户拒绝高风险命令: {store.pending['command']}")
        store.clear_pending()

    # 就地修改了共享消息块（未走 add_message），须置脏否则 save 短路不落盘
    session.mark_dirty()
    session.save()
    return True
