"""Handoff/continuation document templates for worker continuation and fork fallback."""

from __future__ import annotations

import re
from typing import Any

HANDOFF_TEMPLATE = """请把上面的对话压缩成一份交接文档，供一个新的 Agent 接手继续执行。
只输出结构化内容，不要附上原始对话或额外解释。用中文输出。
每节用简短条目；没有内容的写「无」。

## 当前目标
（原任务的主要需求和期望结果）

## 成功标准
（怎样算完成）

## 用户明确要求 / 偏好
（约束、风格偏好、禁止事项）

## 已确认上下文
（已验证的事实、关键信息、已有产物位置）

## 已完成工作
（已完成的关键操作和结论）

## 当前状态
（进行到哪里，卡在哪里）

## 验证状态
- 已验证：（已运行的命令/测试及结果）
- 未验证：（计划但尚未执行的检查）

## 相关文件 / 目录 / 工具
（路径与用途）

## 已做决策
（做过的选择及仍然重要的理由）

## 不要重复的尝试
（失败过的方法，避免新 agent 踩坑）

## 未决问题
（待解决的疑问或分歧）

## 下一阶段任务（立即执行）
{next_steps}

## 交接原因
{reason}
"""


def build_handoff_document(next_steps: str, reason: str = "") -> str:
    """Fill the handoff template with replace (user text may contain braces)."""
    doc = HANDOFF_TEMPLATE.replace("{next_steps}", (next_steps or "无").strip())
    return doc.replace("{reason}", (reason or "无").strip())


build_handoff_instruction = build_handoff_document


def user_input_text(user_input: Any) -> str:
    """Extract plain text from a string or multimodal block list."""
    if isinstance(user_input, str):
        return user_input.strip()
    if isinstance(user_input, list):
        parts: list[str] = []
        for block in user_input:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    parts.append(str(text))
        return "\n".join(parts).strip()
    return ""


def default_branch_name(
    parent_name: str,
    label: str | None,
    existing_names: list[str] | None = None,
) -> str:
    """Pick a UI-safe branch name, deduplicated against existing session names."""
    base = (label or "").strip() or f"{(parent_name or '会话').strip()}·分支"
    base = re.sub(r"\s+", " ", base).strip()[:40] or "分支"
    existing = set(existing_names or [])
    if base not in existing:
        return base
    for i in range(2, 100):
        candidate = f"{base}-{i}"[:48]
        if candidate not in existing:
            return candidate
    return base


