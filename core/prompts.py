"""System prompt 动态段落生成与环境上下文。

统一 Agent 架构下，固定模板文案（原 ROOT/SUB_PROMPT_TEMPLATE）迁入各角色
JSON 的 text block（core/agents/{role}.json）；本模块仅保留动态生成函数，
供 core/prompt_builder.py 的块/层生成器调用：

- _build_tools_section() — 从工具实例生成工具列表段（tools 块）
- _build_skills_section() — 从角色可见技能生成技能列表段（skills 块）
- build_instructions_message() — 项目指令文件注入消息（claude_md 层）
- build_environment_context() — 动态环境上下文（context 层）
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from core.config import get_user_profile_path

logger = logging.getLogger(__name__)


# ─── 动态段落构建 ────────────────────────────────────────────────────

def _build_tools_section(tools: list) -> str:
    """从工具实例生成工具列表段落。"""
    if not tools:
        return ""
    lines = ["## Tools", "", "You have access to the following tools:"]
    for tool in tools:
        desc = tool.description.split("\n")[0].strip()
        lines.append(f"- **{tool.name}** — {desc}")
    return "\n".join(lines)


def _build_deferred_tools_section(deferred_tools: list) -> str:
    """生成延迟工具摘要段：列出名称和简介，引导模型用 tool_search 加载。"""
    if not deferred_tools:
        return ""
    lines = [
        "## Deferred Tools",
        "",
        "The following tools are available but not loaded by default.",
        "Use `tool_search(query='...')` to load a tool's full schema before using it.",
        "Once loaded, the tool becomes active for the rest of the session.",
        "",
    ]
    for tool in deferred_tools:
        desc = tool.description.split("\n")[0].strip()
        lines.append(f"- **{tool.name}** — {desc}")
    return "\n".join(lines)


def _build_skills_section(role: str) -> str:
    """按角色可见技能生成技能列表段落（通用）。"""
    from core.tools.skill import list_skills

    skills = list_skills(role)
    if not skills:
        return ""

    lines = [
        "## Available Skills", "",
        "Built-in skills provide detailed instructions for common workflows.",
        "Use `skill(action='list')` to see all, `skill(action='read', skill_id='...')` to read full content.",
    ]
    for s in skills:
        lines.append(f"- **{s['name']}** (`{s['id']}`): {s['description']}")
    return "\n".join(lines)


# ─── 动态环境上下文（context 层）──────────────────────────────────────

def build_environment_context(workspace_uuid: str = "", cwd: str = "") -> str:
    """构建动态环境变量，作为独立 user 消息段注入。

    每次请求都不同（datetime 变化等），作为单独段发送不影响 system prompt 的缓存。
    包含：Workspace、OS、Shell、Python、Temporary Files、Memory、User Profile、Current Time。
    """
    import platform
    current_date = datetime.now().strftime("%Y-%m-%d")
    from core.config import get_workspace_data_dir
    memory_dir = str(get_workspace_data_dir(workspace_uuid) / "memory")
    # 工作区临时目录：写在 workspace/.tmp 内，受统一路径权限（写/删限工作区）约束
    tmp_dir = os.path.join(cwd, ".tmp") if cwd else ".tmp"

    parts = [
        "## Workspace",
        "",
        f"Workspace ID: `{workspace_uuid}`",
        f"Workspace directory (CWD): `{cwd}`",
        "",
        "**This directory is the CWD for all tool executions** (python, bash, etc.). All relative paths resolve against this directory.",
        "",
        "## Operating System",
        "",
        f"`{platform.system()} {platform.release()}`",
        "",
        "## Shell Environment",
        "",
        "Three separate tools for three environments — **do NOT cross-invoke**:",
        "",
        "| Tool | Environment | Use For |",
        "|------|-------------|---------|",
        "| `bash` | Git Bash (MSYS2) | ls, git, npm, curl, Unix commands |",
        "| `pwsh` | PowerShell | Get-*, Set-*, Windows APIs, registry |",
        "| `python` | Python interpreter | Python code, pip install |",
        "",
        "**Path format for bash**: Windows paths must be converted:",
        "- `E:\\path\\to\\file` → `/e/path/to/file`",
        "- `C:\\Users\\name` → `/c/Users/name`",
        "",
        "**Path format for pwsh**: Use native Windows format (e.g., `C:\\Users`).",
        "",
        "**Python**: Use the `python` tool — do NOT run python from bash or pwsh.",
        "",
        "## Python Environment",
        "",
        "Use the `python` tool for ALL Python execution — do NOT invoke python from bash or pwsh.",
        "The agent's virtual environment is automatically activated in the python tool.",
        "",
        "## Temporary Files",
        "",
        f"Workspace temp directory: `{tmp_dir}` (inside the workspace).",
        "",
        "Use this directory for all intermediate files, temp outputs, downloads, and program state files.",
        "Writes/deletes are only allowed inside the workspace; anything outside requires approval.",
        "For session-scoped temp storage use the `temp` tool — it creates `{cwd}/.tmp/{{session_id}}/`.",
        "In Python, `tempfile` module is auto-configured to the system temp.",
        "",
        "## Memory",
        "",
        "The workspace's persisted memory is injected below: always-on preferences, "
        "the MEMORY.md index, and the global summary. Apply relevant facts, preferences "
        "and skills instead of relying on model memory.",
        "",
    ]

    # 记忆注入（v3 三层：preference 常驻 + MEMORY.md 索引 + summary.md 摘要）
    parts.extend(_build_memory_sections(memory_dir, workspace_uuid))

    # 当前时间（放在最后）
    parts.extend([
        "",
        "## Current Time",
        "",
        f"**{current_date}**",
        "",
        "Use this time when interpreting relative or time-sensitive requests such as \"today\", \"latest\", \"current\", \"this year\", or version/documentation freshness. When the user asks for the latest information, verify it with available web tools rather than relying on model knowledge.",
    ])

    return "\n".join(parts)


# ─── 记忆注入（v3 三层：preference 常驻 + MEMORY.md 索引 + summary.md 摘要）────────

_MEMORY_PREFERENCE_CAP = 10
_MEMORY_SUMMARY_MAX_BYTES = 2 * 1024


def _build_memory_sections(memory_dir: str, workspace_uuid: str = "") -> list[str]:
    """构建记忆注入段（设计 §5 三层注入；记忆系统不可用时降级为提示语，绝不阻塞请求）。

    迁移前 preference 为空时回退注入 user-profile.md，避免丢失原有用户画像。
    """
    lines: list[str] = []
    try:
        from core.memory_store import MemoryStore
        store = MemoryStore(memory_dir)

        prefs = store.list(type_="preference")
        if prefs:
            lines.append("### User Preferences (always-on)")
            for p in prefs[:_MEMORY_PREFERENCE_CAP]:
                stale_note = " ⚠ stale, verify before applying" if store.is_stale(p) else ""
                lines.append(f"- {p.get('title', p['name'])}: {p.get('description', '')}{stale_note}")
            lines.append("")
        else:
            # 迁移前回退：user-profile.md → 旧用户画像
            profile_path = get_user_profile_path(workspace_uuid)
            if profile_path.is_file():
                try:
                    content = profile_path.read_text(encoding="utf-8").strip()
                    if content.startswith("---"):
                        end = content.find("---", 3)
                        if end != -1:
                            content = content[end + 3:].strip()
                    if content:
                        lines.append("### User Preferences (from user-profile.md)")
                        lines.append(content)
                        lines.append("")
                except OSError:
                    pass

        index_path = os.path.join(memory_dir, "MEMORY.md")
        if os.path.isfile(index_path):
            with open(index_path, encoding="utf-8") as f:
                index_text = f.read().strip()
            if index_text:
                lines.append("### Memory Index (descriptions of all entries)")
                lines.append(index_text)
                lines.append("")

        summary_path = os.path.join(memory_dir, "summary.md")
        if os.path.isfile(summary_path):
            with open(summary_path, encoding="utf-8") as f:
                summary_text = f.read().strip()[:_MEMORY_SUMMARY_MAX_BYTES]
            if summary_text:
                lines.append("### Memory Summary")
                lines.append(summary_text)
                lines.append("")

        lines.extend([
            "Search/recall: `memory(action=\"find\", query=\"keyword\")` lists matching entries "
            "with descriptions; then `memory(action=\"read\", name=\"<name>\")` reads the full body.",
        ])
    except Exception:
        # 记忆系统初始化失败：给出降级提示，不阻塞任何请求
        lines.extend([
            f"Memory directory: `{memory_dir}`",
            'Search: `memory(action="find", query="keyword")`, then `memory(action="read", name="...")`.',
        ])
    return lines


# ─── 工作区指令文件加载（claude_md 层）────────────────────────────────

# 支持的项目指令文件（按优先级排序）
_PROJECT_INSTRUCTION_FILES = ["agent.md", "CLAUDE.md", "claude.md"]


def find_project_instructions(cwd: str) -> str | None:
    """在工作区根目录搜索项目指令文件。

    按优先级搜索：agent.md > CLAUDE.md > claude.md
    找到第一个即返回。

    Args:
        cwd: 工作区根目录路径

    Returns:
        文件内容字符串，若未找到返回 None
    """
    if not cwd or not os.path.isdir(cwd):
        return None

    for filename in _PROJECT_INSTRUCTION_FILES:
        filepath = os.path.join(cwd, filename)
        if os.path.isfile(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                logger.warning(f"Failed to read {filepath}: {e}")
                continue
    return None


def build_instructions_message(cwd: str) -> dict | None:
    """构建项目指令消息（作为注入 user 消息）。

    使用 <system-reminder> 标签包装，与 claude-code 保持一致。

    Args:
        cwd: 工作区根目录路径

    Returns:
        消息字典 {"role": "user", "content": "..."}，若未找到指令文件返回 None
    """
    content = find_project_instructions(cwd)
    if not content:
        return None

    return {
        "role": "user",
        "content": (
            "<system-reminder>\n"
            "Codebase and user instructions are shown below. "
            "Be sure to adhere to these instructions. "
            "IMPORTANT: These instructions OVERRIDE any default behavior and you MUST follow them exactly as written.\n\n"
            f"{content.strip()}\n"
            "</system-reminder>"
        )
    }
