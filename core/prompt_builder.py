"""配置化 prompt 构建：system prompt 块拼装 + user 层注入 + 防连续合并。

角色 JSON 的 ``system_prompt.blocks`` 声明系统提示词由哪些块拼装（按顺序）：
- ``text``：固定文案（block.content，原模板常量迁入 JSON）
- ``tools``：从实际加载的工具实例生成工具列表段
- ``skills``：从角色可见技能生成技能列表段

``user_layers`` 声明需要注入的 user 消息层：
- ``claude_md``：每次从磁盘重读项目指令（agent.md/CLAUDE.md），不持久化
- ``context``：动态环境上下文（日期/workspace/内存等），不持久化
- ``task`` / ``runtime``：autonomous 运行时写入历史（pinned 任务消息、预算/检查/
  超时提示），由 Agent 按 role_cfg 布尔开关处理，不在生成器表内

注入层与消息历史在 ``assemble_context()`` 中合并；连续 user 消息自动合并成一条，
保证角色交替（满足 OpenAI/Bedrock 约束）。
"""

from __future__ import annotations

from typing import Any, Callable


# ─── system prompt 块生成器 ──────────────────────────────────────────


def _gen_text(block: dict, agent) -> str:
    """text 块：直接返回固定文案。content 为字符串或字符串数组（按行拼装）。"""
    content = block.get("content", "")
    if isinstance(content, list):
        return "\n".join(content)
    return content


def _gen_tools(block: dict, agent) -> str:
    """tools 块：从 agent.tools 生成工具列表段。"""
    from core.prompts import _build_tools_section
    return _build_tools_section(agent.tools)


def _gen_skills(block: dict, agent) -> str:
    """skills 块：从角色可见技能生成技能列表段。"""
    from core.prompts import _build_skills_section
    return _build_skills_section(agent.role)


SYSTEM_BLOCK_GENERATORS: dict[str, Callable[[dict, Any], str]] = {
    "text": _gen_text,
    "tools": _gen_tools,
    "skills": _gen_skills,
}


def build_system_prompt(agent) -> str:
    """按角色配置的 blocks 顺序拼装 system prompt（仅启用且非空的块）。"""
    parts = []
    for block in agent.role_cfg.system_prompt.get("blocks", []):
        if not block.get("enabled", True):
            continue
        gen = SYSTEM_BLOCK_GENERATORS.get(block.get("type"))
        if gen is None:
            continue
        content = gen(block, agent)
        if content:
            parts.append(str(content).strip())
    return "\n\n".join(parts)


# ─── user 层注入生成器（返回 "user" 消息 dict 或 None）───────────────


def _gen_claude_md(agent) -> dict | None:
    """claude_md 层：从磁盘重读项目指令文件。"""
    from core.prompts import build_instructions_message
    return build_instructions_message(agent.cwd)


def _gen_context(agent) -> dict | None:
    """context 层：动态环境上下文（每次调用重新生成，含当前时间）。"""
    from core.prompts import build_environment_context
    return {
        "role": "user",
        "content": build_environment_context(agent.workspace_uuid, agent.cwd),
    }


USER_LAYER_GENERATORS: dict[str, Callable[[Any], dict | None]] = {
    "claude_md": _gen_claude_md,
    "context": _gen_context,
}


# ─── 防连续合并 ──────────────────────────────────────────────────────


def _merge_user_messages(a: dict, b: dict) -> dict:
    """合并两条连续 user 消息。str content 拼接为 str；否则归一化为 block 列表拼接。

    保留第一条消息的 _meta（id/pinned 等）。
    """
    a_content = a.get("content", "")
    b_content = b.get("content", "")

    if isinstance(a_content, str) and isinstance(b_content, str):
        merged: dict = {"role": "user", "content": a_content + "\n\n" + b_content}
    else:
        def _to_blocks(content):
            if isinstance(content, str):
                return [{"type": "text", "text": content}]
            if isinstance(content, list):
                return list(content)
            return [{"type": "text", "text": str(content)}]
        merged = {"role": "user", "content": _to_blocks(a_content) + _to_blocks(b_content)}

    meta = a.get("_meta")
    if meta:
        merged["_meta"] = dict(meta)
    return merged


def assemble_context(messages: list[dict], inject_messages: list[dict]) -> list[dict]:
    """把注入型 user 消息与消息历史合并，并合并连续 user 消息。

    注入消息恒排在最前；若历史第一条也是 user（如 pinned 任务消息），
    二者合并为一条。返回新的消息列表，不修改入参。
    """
    if not inject_messages:
        return messages

    result = list(inject_messages) + list(messages)
    merged: list[dict] = []
    for msg in result:
        if merged and merged[-1].get("role") == "user" and msg.get("role") == "user":
            merged[-1] = _merge_user_messages(merged[-1], msg)
        else:
            merged.append(msg)
    return merged
