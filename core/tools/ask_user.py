"""AskUserQuestion tool — Agent 主动向用户提问，收集决策信息。

设计：工具返回 completed=False 的 ToolResult，Agent 循环退出。
前端检测到 ask_user 后渲染选择面板，用户提交后作为新 user message 继续循环。

审批卡与提问卡共用本模块的占位符管道，但语义分开：
- 提问：自由文本/选项回答，`_meta.kind="question"`；
- 审批：固定决策（approve/remember/deny/abort），`_meta.kind="approval"` +
  `_meta.pending_approval`，载荷随消息落盘，超时由看门狗结算。
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import datetime
from typing import Any, Iterable

from core.security.permission_rules import build_granted_rule, implied_path_rules
from core.tools.base import Tool, ToolResult
from core.tools.permission import (
    ABORT_LABEL,
    APPROVE_LABEL,
    REJECT_LABEL,
    REMEMBER_LABEL,
    append_permission_audit,
)

logger = logging.getLogger(__name__)

APPROVAL_META_KEY = "pending_approval"
DECISION_APPROVE = "approve"
DECISION_REMEMBER = "remember"
DECISION_DENY = "deny"
DECISION_ABORT = "abort"
DECISIONS = (DECISION_APPROVE, DECISION_REMEMBER, DECISION_DENY, DECISION_ABORT)

APPROVE_TEXT = "用户已允许该操作（本次会话）。可以继续执行。"
REMEMBER_TEXT = "用户已允许此类操作，并已写入工作区规则。可以继续执行。"
DENY_TEXT = "用户拒绝了该操作。请改用其他方式完成目标，不要原样重发该命令。"
ABORT_TEXT = "用户终止了当前任务。不要继续执行。"
EXPIRED_TEXT = (
    "审批超时（{window}），用户未响应，该操作未执行。"
    "不要原样重试；可换方案，或请用户重新发起。"
)


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


# ─── ask_user / 审批卡占位符流程（接口无关：Web / QQ / 微信共用） ─────────
# 占位符由 AskUserTool.execute 返回 completed=False 的 ToolResult 生成；接入端渲染
# 问题卡，用户提交后经 apply_answer 注入答案并恢复 runner 循环。

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _safe_answer_filename(tool_use_id: str) -> str:
    """ask_user 答案文件的净名（非法 tool_use_id 回退随机名，防路径穿越）。"""
    if tool_use_id and _SAFE_ID_RE.match(tool_use_id):
        return f"{tool_use_id}.txt"
    return f"{secrets.token_hex(4)}.txt"


def _block_tool_id(block: dict) -> str:
    return str(block.get("tool_use_id") or block.get("tool_call_id") or "")


def _iter_tool_result_blocks(
    session, max_messages: int | None = None,
) -> Iterable[tuple[dict, dict]]:
    """逆序产出 (消息, tool_result 块)；消息是 runner 与 session 的共享引用。

    ``max_messages`` 限制只扫描最近 N 条 user 消息——看门狗高频轮询用它避免
    长会话全量扫描；待决占位符总是位于会话尾部（循环退出后不会再追加）。
    """
    scanned = 0
    for msg in reversed(session.messages):
        if msg.get("role") != "user":
            continue
        scanned += 1
        if max_messages is not None and scanned > max_messages:
            return
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                yield msg, block


def _find_pending_block(session, max_messages: int | None = None) -> dict | None:
    """最后一个未完成的占位 tool_result 块（ask_user 或审批卡）。"""
    for _msg, block in _iter_tool_result_blocks(session, max_messages=max_messages):
        meta = block.get("_meta") or {}
        if meta.get("completed") is False:
            return block
    return None


def _find_tool_result_block(session, tool_use_id: str) -> dict | None:
    for _msg, block in _iter_tool_result_blocks(session):
        if _block_tool_id(block) == tool_use_id:
            return block
    return None


def _find_tool_use_block(session, tool_use_id: str) -> dict | None:
    for msg in session.messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("tool_use", "tool_call") and block.get("id") == tool_use_id:
                return block
    return None


def _questions_of(session, tool_use_id: str) -> list[dict]:
    block = _find_tool_use_block(session, tool_use_id)
    if not block:
        return []
    return list((block.get("input") or {}).get("questions") or [])


def classify_pending(session, permission_store=None) -> tuple[str, str] | None:
    """返回 ("approval"|"question", tool_use_id)；无待决占位符时返回 None。

    审批判定优先级：显式 `_meta.kind=="approval"` / `pending_approval` 载荷 →
    旧卡片（无标记）但 PermissionStore 有 pending。
    """
    block = _find_pending_block(session)
    if block is None:
        return None
    tool_use_id = _block_tool_id(block)
    meta = block.get("_meta") or {}
    if meta.get("kind") == "approval" or APPROVAL_META_KEY in meta:
        return "approval", tool_use_id
    questions = _questions_of(session, tool_use_id)
    if questions and str(questions[0].get("header") or "") == "权限确认":
        return "approval", tool_use_id
    if permission_store is not None and getattr(permission_store, "pending", None):
        return "approval", tool_use_id
    if meta.get("tool_name") == "ask_user":
        return "question", tool_use_id
    return None


def find_pending_ask_user(session) -> str | None:
    """查找最后一个待回答的 ask_user/审批占位 tool_result，返回其 tool_use_id。

    占位符由 _execute_tool 生成：user 消息中的 tool_result 块带
    ``_meta.completed=False`` 且 ``_meta.tool_name="ask_user"``。
    """
    block = _find_pending_block(session)
    if block is None:
        return None
    meta = block.get("_meta") or {}
    if meta.get("tool_name") != "ask_user" and meta.get("kind") != "approval":
        return None
    return _block_tool_id(block) or None


def find_pending_approval(session, *, max_messages: int | None = 200) -> dict[str, Any] | None:
    """返回最近一条未结算的审批占位（含载荷/期限/接口），无则 None。

    ``max_messages`` 默认只看最近 200 条 user 消息（看门狗高频调用）；
    调用方需要全量扫描时传 ``None``。
    """
    block = _find_pending_block(session, max_messages=max_messages)
    if block is None:
        return None
    meta = block.get("_meta") or {}
    permission = meta.get(APPROVAL_META_KEY)
    if meta.get("kind") != "approval" and not permission:
        return None
    return {
        "tool_use_id": _block_tool_id(block),
        "permission": dict(permission or {}),
        "meta": meta,
        "block": block,
    }


def pending_deadline(pending: dict[str, Any] | None) -> float:
    if not pending:
        return 0.0
    try:
        return float((pending.get("permission") or {}).get("deadline_ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def is_expired(pending: dict[str, Any] | None, now: float | None = None) -> bool:
    deadline = pending_deadline(pending)
    return deadline > 0 and (time.time() if now is None else now) >= deadline


def _format_window(seconds: int) -> str:
    if seconds and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} 分钟"
    if seconds and seconds > 60:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{int(seconds or 0)} 秒"


def decision_from_text(text: str | None) -> str:
    """旧客户端/IM 文本 → 决策。精确 label 优先，保留 endswith 兼容。"""
    stripped = (text or "").strip()
    if not stripped:
        return DECISION_DENY
    if stripped == ABORT_LABEL or stripped.endswith(ABORT_LABEL):
        return DECISION_ABORT
    if stripped == REMEMBER_LABEL or stripped.endswith(REMEMBER_LABEL):
        return DECISION_REMEMBER
    if stripped == APPROVE_LABEL or stripped.endswith(APPROVE_LABEL):
        return DECISION_APPROVE
    if stripped == REJECT_LABEL or stripped.endswith(REJECT_LABEL):
        return DECISION_DENY
    return DECISION_DENY


def build_other_answer(session, ask_user_tool_use_id: str, content: str) -> str:
    """以用户输入作为 ask_user 的「其他」回复，按卡片 formatAnswers 格式组装（`问题 答案`）。"""
    questions = _questions_of(session, ask_user_tool_use_id)
    if not questions:
        return content
    parts = [f"{q.get('question', '')} {content}" for q in questions if q.get("question")]
    return "\n".join(parts) if parts else content


def format_structured_answers(session, ask_user_tool_use_id: str, answers: list[dict]) -> str:
    """把结构化答案 [{id, selected[], custom}] 还原成模型可读文本（`问题 答案`）。"""
    questions = _questions_of(session, ask_user_tool_use_id)
    if not questions:
        return "\n".join(
            ", ".join([*(a.get("selected") or []), a.get("custom") or ""]).strip(", ")
            for a in answers if isinstance(a, dict)
        ).strip()
    by_id = {str(a.get("id")): a for a in answers if isinstance(a, dict)}
    lines: list[str] = []
    for idx, q in enumerate(questions):
        ans = by_id.get(str(q.get("id") or idx)) or by_id.get(str(idx)) or {}
        selected = [str(s) for s in (ans.get("selected") or [])]
        custom = str(ans.get("custom") or "").strip()
        value = ", ".join([*selected, custom] if custom else selected)
        lines.append(f"{q.get('question', '')} {value}".strip())
    return "\n".join(lines)


def _write_answer_file(session, tool_use_id: str, text: str) -> dict[str, Any]:
    """答案写入外部文件并返回应合并进 _meta 的字段。"""
    output_path = _safe_answer_filename(tool_use_id)
    try:
        ext_file = session.session_dir / output_path
        ext_file.write_text(text, encoding="utf-8")
        logger.info(f"[ask-user] 已写入答案文件: {output_path}")
    except Exception as e:  # noqa: BLE001 —— 写文件失败不阻断内存答复
        logger.warning(f"[ask-user] 写入答案文件失败: {e}")
    return {
        "output_path": output_path,
        "file_size": len(text.encode("utf-8")),
    }


def settle_placeholder(
    session,
    tool_use_id: str,
    text: str,
    *,
    outcome: str,
    extra_meta: dict[str, Any] | None = None,
) -> bool:
    """把占位 tool_result 结算为最终结果并落盘（提问/审批共用）。"""
    block = _find_tool_result_block(session, tool_use_id)
    if block is None:
        logger.error(f"[ask-user] 未找到占位符 tool_result: tool_use_id={tool_use_id}")
        return False

    block["content"] = text
    meta = block.setdefault("_meta", {})
    meta.update({
        "completed": True,
        "outcome": outcome,
        "settled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **_write_answer_file(session, tool_use_id, text),
        **(extra_meta or {}),
    })

    tool_use = _find_tool_use_block(session, tool_use_id)
    if tool_use is not None:
        tu_meta = tool_use.setdefault("_meta", {})
        tu_meta["answered"] = True
        tu_meta["answered_outcome"] = outcome
    else:
        logger.warning(f"[ask-user] 未找到对应的 tool_use/tool_call: id={tool_use_id}")

    # 就地修改了共享消息块（未走 add_message），须置脏否则 save 短路不落盘
    session.mark_dirty()
    session.save()
    return True


def _clear_pending_if_match(permission_store, decision_id: str) -> None:
    """只清与本次结算对应的 pending（单槽并发时避免误清更新的卡）。"""
    if permission_store is None:
        return
    pending = getattr(permission_store, "pending", None)
    if not pending:
        return
    if not decision_id or pending.get("decision_id") == decision_id:
        permission_store.clear_pending()


def _audit_decision(session, permission: dict, decision: str, *, persisted: bool = False,
                    rule_id: str = "", reason: str = "") -> None:
    decision_map = {
        DECISION_APPROVE: "approve",
        DECISION_REMEMBER: "persist" if persisted else "session",
        DECISION_DENY: "reject",
        DECISION_ABORT: "abort",
    }
    append_permission_audit(getattr(session, "session_dir", None), "decided", {
        "source": "user",
        "decision_id": permission.get("decision_id", ""),
        "tool": permission.get("tool", ""),
        "kind": permission.get("kind", "command"),
        "action": str(permission.get("command", ""))[:300],
        "dynamic_kind": permission.get("dynamic_kind", ""),
        "static_prefix": permission.get("static_prefix") or "",
        "decision": decision_map.get(decision, "reject"),
        "persisted": persisted,
        "rule_id": rule_id,
        "reason": reason,
    })


def _apply_permission_side_effects(
    session,
    permission: dict,
    decision: str,
    permission_store=None,
    permission_rules=None,
) -> tuple[bool, str, str]:
    """按决策更新会话批准/规则。返回 (persisted, rule_id, reason)。"""
    store = permission_store
    rules = permission_rules
    did = str(permission.get("decision_id", ""))
    command = str(permission.get("command", ""))
    kind = str(permission.get("kind", "command"))

    if decision == DECISION_REMEMBER:
        persisted = False
        rule_id = ""
        reason = ""
        if rules is not None:
            built = build_granted_rule(
                kind=kind, tool=permission.get("tool", "*"), action=command,
                decision="allow",
                suggested=permission.get("suggested_rule"), granted_by="user",
                summary=permission.get("summary", ""), decision_id=did,
            )
            reason = built.reason
            if built.rule:
                added = rules.add_rule(built.rule, "workspace")
                if added:
                    persisted = True
                    rule_id = str(added.get("id") or "")
                    for extra in implied_path_rules(permission.get("targets") or [],
                                                    "user", did):
                        rules.add_rule(extra, "workspace")
                    logger.info(f"[permission] 用户批准并记住（落盘规则）：{command[:80]}")
            if not persisted:
                logger.info(
                    f"[permission] 未生成持久规则（{reason or '规则未落盘'}），"
                    f"仅会话级放行：{command[:80]}"
                )
        if store is not None:
            store.approve(did, command, kind=kind)
        return persisted, rule_id, reason

    if decision == DECISION_APPROVE:
        if store is not None:
            store.approve(did, command, kind=kind)
            # 会话级放行越界目标：重执行直接通过路径门，无需再判定（设计 §3.4）
            for t in permission.get("targets") or []:
                if isinstance(t, dict) and t.get("decision_id") and not t.get("inside_workspace"):
                    t_kind = "path:read" if t.get("op") == "read" else (
                        "path:delete" if t.get("op") == "delete" else "path:write"
                    )
                    store.approve(t["decision_id"], str(t.get("raw") or ""), kind=t_kind)
        logger.info(f"[approval] 用户批准高风险命令(本次会话): {command[:80]}")
        return False, "", ""

    if decision == DECISION_DENY:
        logger.info(f"[approval] 用户拒绝高风险命令: {command[:80]}")
    else:
        logger.info(f"[approval] 用户终止任务（命令未执行）: {command[:80]}")
    return False, "", ""


def apply_answer(
    session,
    tool_use_id: str,
    *,
    text: str | None = None,
    decision: str | None = None,
    answers: list[dict] | None = None,
    permission_store=None,
    permission_rules=None,
) -> str:
    """结算一次待答占位符。

    返回 ``answered`` / ``aborted`` / ``expired`` / ``not_found``。
    - 审批卡：decision 优先；只有 text 时按 label 映射（未知文本 = 拒绝）。
    - 提问卡：answers 优先，否则用 text。
    """
    block = _find_tool_result_block(session, tool_use_id)
    if block is None:
        logger.error(f"[ask-user] 未找到占位符 tool_result: tool_use_id={tool_use_id}")
        return "not_found"

    meta = block.get("_meta") or {}
    if meta.get("completed") is True:
        # 已结算（用户先答、看门狗先结算、或重复提交）：不再改写历史
        logger.info(f"[ask-user] 占位符已结算，忽略重复提交: tool_use_id={tool_use_id}")
        return "expired" if meta.get("outcome") == "expired" else "not_found"
    permission = meta.get(APPROVAL_META_KEY)
    questions = _questions_of(session, tool_use_id)
    legacy_approval = bool(questions) and str(questions[0].get("header") or "") == "权限确认"
    is_approval = (
        meta.get("kind") == "approval"
        or permission is not None
        or legacy_approval
        or (permission_store is not None and getattr(permission_store, "pending", None))
    )
    # 旧卡片缺失载荷（升级前发出、重启后 pending 已丢）：退化为自由文本答复，
    # 不做任何批准/落盘副作用（fail closed）。
    if is_approval and not permission and not (
            permission_store is not None and getattr(permission_store, "pending", None)):
        is_approval = False

    if is_approval:
        permission = dict(permission or getattr(permission_store, "pending", {}) or {})
        pending = {"tool_use_id": tool_use_id, "permission": permission, "meta": meta,
                   "block": block}
        if decision is None:
            decision = decision_from_text(text)
        if decision not in DECISIONS:
            decision = DECISION_DENY

        if is_expired(pending):
            timeout = int(permission.get("timeout_seconds") or 0)
            settle_placeholder(
                session, tool_use_id,
                EXPIRED_TEXT.format(window=_format_window(timeout)),
                outcome="expired",
                extra_meta={"permission_outcome": "expired"},
            )
            append_permission_audit(getattr(session, "session_dir", None), "expired", {
                "source": "user",
                "decision_id": permission.get("decision_id", ""),
                "tool": permission.get("tool", ""),
                "kind": permission.get("kind", "command"),
                "action": str(permission.get("command", ""))[:300],
            })
            _clear_pending_if_match(permission_store, permission.get("decision_id", ""))
            return "expired"

        persisted, rule_id, reason = _apply_permission_side_effects(
            session, permission, decision, permission_store, permission_rules)
        _audit_decision(session, permission, decision, persisted=persisted,
                        rule_id=rule_id, reason=reason)
        _clear_pending_if_match(permission_store, permission.get("decision_id", ""))

        if decision == DECISION_APPROVE:
            settle_placeholder(session, tool_use_id, APPROVE_TEXT, outcome="approved",
                               extra_meta={"permission_outcome": "approved"})
            return "answered"
        if decision == DECISION_REMEMBER:
            settle_placeholder(session, tool_use_id, REMEMBER_TEXT, outcome="remembered",
                               extra_meta={"permission_outcome": "remembered"})
            return "answered"
        if decision == DECISION_ABORT:
            settle_placeholder(session, tool_use_id, ABORT_TEXT, outcome="aborted",
                               extra_meta={"permission_outcome": "aborted"})
            return "aborted"
        settle_placeholder(session, tool_use_id, DENY_TEXT, outcome="denied",
                           extra_meta={"permission_outcome": "denied"})
        return "answered"

    # 提问卡：结构化答案优先，否则自由文本
    answer_text = text or ""
    if answers is not None:
        answer_text = format_structured_answers(session, tool_use_id, answers)
    settle_placeholder(session, tool_use_id, answer_text, outcome="answered")
    return "answered"


def inject_ask_user_answer(session, ask_user_tool_use_id: str, answer: str,
                           permission_store=None, permission_rules=None) -> bool:
    """兼容入口：把用户答案注入占位 tool_result 并标记 answered，返回是否找到占位符。

    审批卡走结构化决策（文本按 label 映射）；提问卡保持自由文本语义。
    """
    logger.info(f"[ask-user] 注入答案: tool_use_id={ask_user_tool_use_id}")
    outcome = apply_answer(
        session, ask_user_tool_use_id, text=answer,
        permission_store=permission_store, permission_rules=permission_rules,
    )
    return outcome != "not_found"


def settle_expired_approvals(
    session,
    *,
    permission_store=None,
    now: float | None = None,
    audit: bool = True,
) -> list[str]:
    """结算会话内所有已过期的审批占位（只结算，不恢复）。返回 tool_use_id 列表。"""
    settled: list[str] = []
    for _msg, block in list(_iter_tool_result_blocks(session)):
        meta = block.get("_meta") or {}
        if meta.get("completed") is not False:
            continue
        permission = meta.get(APPROVAL_META_KEY)
        if meta.get("kind") != "approval" and not permission:
            continue
        tool_use_id = _block_tool_id(block)
        pending = {
            "tool_use_id": tool_use_id,
            "permission": dict(permission or {}),
            "meta": meta,
            "block": block,
        }
        if not is_expired(pending, now=now):
            continue
        timeout = int((permission or {}).get("timeout_seconds") or 0)
        settle_placeholder(
            session, tool_use_id,
            EXPIRED_TEXT.format(window=_format_window(timeout)),
            outcome="expired",
            extra_meta={"permission_outcome": "expired"},
        )
        if audit:
            append_permission_audit(getattr(session, "session_dir", None), "expired", {
                "source": "watchdog",
                "decision_id": (permission or {}).get("decision_id", ""),
                "tool": (permission or {}).get("tool", ""),
                "kind": (permission or {}).get("kind", "command"),
                "action": str((permission or {}).get("command", ""))[:300],
            })
        _clear_pending_if_match(
            permission_store, (permission or {}).get("decision_id", ""))
        settled.append(tool_use_id)
    return settled
