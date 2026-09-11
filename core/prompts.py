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

from core.config import PROJECT_ROOT, get_user_profile_path

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
    tmp_dir = os.environ.get("CILI_TMP", str(PROJECT_ROOT / "data" / "tmp"))

    parts = [
        "## Workspace",
        "",
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
        f"Temporary directory: `{tmp_dir}`",
        "",
        "Environment variables TEMP, TMP, TMPDIR are all set to this directory.",
        "Use this directory for all intermediate files, temp outputs, downloads, and program state files.",
        "In bash: use `$TEMP` or `$TMPDIR`. In Python: `tempfile` module is auto-configured.",
        "Agent can also use `CILI_TMP` env var to reference this path.",
        "",
        "## Memory",
        "",
        f"Memory directory: `{memory_dir}`",
        "Subdirectories: `knowledge/` (facts) and `skills/` (reusable techniques).",
        "",
        "Search examples:",
        "```",
        "memory(action=\"find\", query=\"keyword\")",
        "read(file_path=\"...matched path from find results...\")",
        "```",
    ]

    # User Profile（自动从 user-profile.md 加载）
    profile_path = get_user_profile_path(workspace_uuid)
    if profile_path.exists():
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 解析 YAML frontmatter（如果有）
            if content.startswith("---"):
                parts_end = content.find("---", 3)
                if parts_end != -1:
                    content = content[parts_end + 3:].strip()

            if content:
                parts.extend([
                    "",
                    "## User Profile",
                    "",
                    "The following describes the person you are currently chatting with, "
                    "inferred from their past conversations. "
                    "Use these insights naturally to personalize your responses — "
                    "match their communication style, anticipate their needs, and adapt to their preferences. "
                    "Never recite, echo, or explicitly mention these observations unless they bring it up first.",
                    "",
                    content,
                ])
        except Exception:
            pass  # Silently skip if file is corrupted

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
