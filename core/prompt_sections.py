"""System prompt 动态段落生成与环境上下文。

统一 Agent 架构下，固定模板文案（原 ROOT/SUB_PROMPT_TEMPLATE）迁入各角色
JSON 的 text block（core/agents/{role}.json）；本模块仅保留动态生成函数，
供 core/prompt_builder.py 的块/层生成器调用：

- _build_tools_section() — 从工具实例生成工具列表段（tools 块）
- _build_skills_section() — 从角色可见技能生成技能列表段（skills 块）
- build_instructions_message() — 项目指令文件注入消息（项目指令层）
- build_environment_context() — 动态环境上下文（context 层）
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# 技能清单段预算：单条描述截断至 _SKILL_DESC_MAX_CHARS；
# 整段超过 _SKILLS_SECTION_BUDGET_CHARS 时降级为仅名称+id 清单。
_SKILL_DESC_MAX_CHARS = 200
_SKILLS_SECTION_BUDGET_CHARS = 4000

# 工具索引摘要上限：完整说明仍在 description / schema 里，这里只做导航
_TOOL_SNIPPET_MAX_CHARS = 120


# ─── 动态段落构建 ────────────────────────────────────────────────────

def _tool_snippet(tool) -> str:
    """工具索引的一句话摘要：优先 prompt_snippet，否则取 description 首句并截断。"""
    snippet = (getattr(tool, "prompt_snippet", "") or "").strip()
    if not snippet:
        snippet = (tool.description or "").strip().split("\n", 1)[0].strip()
        for sep in (". ", "。", "！", "？"):
            idx = snippet.find(sep)
            if 20 <= idx <= _TOOL_SNIPPET_MAX_CHARS:
                snippet = snippet[: idx + len(sep)].strip()
                break
    if len(snippet) > _TOOL_SNIPPET_MAX_CHARS:
        snippet = snippet[:_TOOL_SNIPPET_MAX_CHARS].rstrip() + "…"
    return snippet or tool.name


def _build_tools_section(tools: list) -> str:
    """从工具实例生成工具列表段落（name + 一句话摘要）。"""
    if not tools:
        return ""
    lines = ["## Tools", "", "You have access to the following tools:"]
    for tool in tools:
        lines.append(f"- **{tool.name}** — {_tool_snippet(tool)}")
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
        lines.append(f"- **{tool.name}** — {_tool_snippet(tool)}")
    return "\n".join(lines)


def _build_skills_section(role: str, interface: str | None = None) -> str:
    """按角色 + 接口可见技能生成技能列表段落（通用）。

    ``interface`` 为当前驱动接口（解析后的档案名）；None 表示未声明——
    声明了 ``interfaces`` 的技能在此情形一律不可见。

    预算控制：单条描述截断 ``_SKILL_DESC_MAX_CHARS``；整段超出
    ``_SKILLS_SECTION_BUDGET_CHARS`` 时降级为仅名称+id 清单，
    保证所有技能仍可被发现。
    """
    from core.skills_catalog import list_skills

    skills = list_skills(role, interface)
    if not skills:
        return ""

    lines = [
        "## Available Skills", "",
        "Built-in skills provide detailed instructions for common workflows.",
        "Use `skill(action='list')` to see all, `skill(action='read', skill_id='...')` to read full content.",
        "If a skill description matches the task, read it before implementing.",
    ]

    def _entry(s: dict, with_desc: bool) -> str:
        head = f"- **{s['name']}** (`{s['id']}`)"
        if not with_desc:
            return head
        desc = s.get("description", "")
        if len(desc) > _SKILL_DESC_MAX_CHARS:
            desc = desc[:_SKILL_DESC_MAX_CHARS] + "…"
        wtu = s.get("when_to_use") or ""
        if wtu:
            desc = f"{desc} Use when: {wtu}" if desc else f"Use when: {wtu}"
        return f"{head}: {desc}" if desc else head

    entries = [_entry(s, True) for s in skills]
    if sum(map(len, entries)) + sum(map(len, lines)) > _SKILLS_SECTION_BUDGET_CHARS:
        entries = [_entry(s, False) for s in skills]
        entries.append(
            "(Descriptions omitted: skill metadata exceeded the prompt budget — "
            "use `skill(action='read', skill_id='...')` for details.)"
        )
    lines.extend(entries)
    return "\n".join(lines)


# ─── 动态环境上下文（context 层）──────────────────────────────────────

def build_environment_context(workspace_uuid: str = "", cwd: str = "", *,
                              tools: list[str] | None = None) -> str:
    """构建动态环境变量，作为独立 user 消息段注入。

    环境事实在会话内缓存；当前时间不注入 prompt，需要时由模型调用 clock。
    包含：Workspace、OS、Shell、Python、Temporary Files、Memory、Current Time。
    """
    import platform
    available = set(tools) if tools is not None else {"bash", "pwsh", "python"}
    from core.config import get_workspace_data_dir
    memory_dir = str(get_workspace_data_dir(workspace_uuid) / "memory")
    # 工作区临时目录：写在 {workspace}/.cili/tmp 内，受统一路径权限（写/删限工作区）约束
    tmp_dir = str(get_workspace_data_dir(workspace_uuid) / "tmp")

    try:
        from core.node_identity import get_node_identity
        node = get_node_identity()
        node_line = f"Node: `{node['id']}` ({node['name']})"
    except Exception:
        node_line = "Node: unavailable"
    parts = [
        "## Workspace",
        "",
        f"Workspace ID: `{workspace_uuid}`",
        f"Workspace directory (CWD): `{cwd}`",
        node_line,
        "",
        "**This directory is the CWD for all tool executions** (python, bash, etc.). All relative paths resolve against this directory.",
        "",
        "**Path convention**: pass workspace-relative paths to every tool (e.g. `src/app.py`) rather than absolute paths. Do not use `../` or any relative path that escapes the workspace boundary. If a file outside the workspace is genuinely required, pass its absolute path (subject to the permission policy) or ask the user first.",
        "",
        "## Operating System",
        "",
        f"`{platform.system()} {platform.release()}`",
        "",
        "## Shell Environment",
        "",
        "Use the dedicated tool for each environment. Cross-invoking another shell may be denied by the permission policy.",
        "",
        "| Tool | Environment | Use For |",
        "|------|-------------|---------|",
    ]
    if "bash" in available:
        parts.append("| `bash` | Git Bash (MSYS2) | ls, git, npm, curl, Unix commands |")
    if "pwsh" in available:
        parts.append("| `pwsh` | PowerShell | Get-*, Set-*, Windows APIs, registry |")
    if "python" in available:
        parts.append("| `python` | Python interpreter | Python code, pip install |")
    parts.append("")
    if "bash" in available:
        parts.extend([
            "**Path format for bash**: Windows paths must be converted:",
            "- `E:\\path\\to\\file` → `/e/path/to/file`",
            "- `C:\\Users\\name` → `/c/Users/name`",
            "",
        ])
    if "pwsh" in available:
        parts.extend([
            "**Path format for pwsh**: Use native Windows format (e.g., `C:\\Users`).",
            "",
        ])
    if "python" in available:
        parts.extend([
            "**Python execution**: Prefer the `python` tool for Python code and scripts. "
            "Running Python from `bash` bypasses the python tool's static analysis and may require permission approval.",
            "",
            "## Python Environment",
            "",
            "Use the `python` tool for Python execution. The agent's virtual environment is automatically activated there.",
            "",
        ])
    parts.extend([
        "## Temporary Files",
        "",
        f"Workspace temp directory: `{tmp_dir}` (inside the workspace).",
        "",
        "Use this directory for all intermediate files, temp outputs, downloads, and program state files. Do not scatter temp files in the working directory or write them to the system temp directory.",
        "Writes/deletes inside the workspace are allowed by default; outside actions follow the workspace permission settings (allow / AI judgment / user confirmation / deny). Reads outside follow the read policy.",
    ])
    if "temp" in available:
        parts.extend([
            "For session-scoped temp storage use the `temp` tool — it creates "
            "`{workspace}/.cili/tmp/{session_id}/`.",
        ])
    if "python" in available:
        parts.append(
            "In Python, `tempfile` and the `TEMP`/`TMP`/`TMPDIR` environment variables "
            "are redirected to the workspace temp directory, so `tempfile.gettempdir()` "
            "resolves inside the workspace."
        )
    parts.extend([
        "",
        "## Memory",
        "",
        "The workspace's persisted memory is injected below: always-on preferences, "
        "the MEMORY.md index, and the global summary. Apply relevant facts, preferences "
        "and skills instead of relying on model memory.",
        "",
    ])

    # 记忆注入（v3 三层：preference 常驻 + MEMORY.md 索引 + summary.md 摘要）
    parts.extend(_build_memory_sections(memory_dir, workspace_uuid))

    # 当前时间不注入（环境段按会话缓存，日期会过期）；需要时由模型调用 clock
    parts.extend([
        "",
        "## Current Time",
        "",
        "For the current date/time, call the `clock` tool (action='now').",
    ])

    return "\n".join(parts)


# ─── 记忆注入（v3 三层：preference 常驻 + MEMORY.md 索引 + summary.md 摘要）────────

# 记忆注入限额：控制 system prompt 体积，避免无限膨胀
_MEMORY_PREFERENCE_CAP = 10  # 最多注入 10 条 preference
_MEMORY_SUMMARY_MAX_CHARS = 2 * 1024  # summary.md 最多截取 2048 个字符（中文时约 6KB）
_MEMORY_INDEX_MAX_CHARS = 8 * 1024  # MEMORY.md 索引注入预算（按完整行截断）


def _build_memory_sections(memory_dir: str, workspace_uuid: str = "") -> list[str]:
    """构建记忆注入段（设计 §5 三层注入；记忆系统不可用时降级为提示语，绝不阻塞请求）。

    user-profile.md 已废弃（用户画像由 memory/preference 承载），不再回退读取。
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

        index_path = os.path.join(memory_dir, "MEMORY.md")
        if os.path.isfile(index_path):
            with open(index_path, encoding="utf-8") as f:
                index_text = f.read().strip()
            if index_text:
                lines.append("### Memory Index (descriptions of all entries)")
                try:
                    by_type = store.stat().get("by_type", {})
                    counts = " / ".join(
                        f"{t} {by_type.get(t, 0)}"
                        for t in ("fact", "preference", "skill", "reference")
                    )
                    lines.append(f"Entries: {counts}")
                except Exception:
                    pass
                truncated = len(index_text) > _MEMORY_INDEX_MAX_CHARS
                if truncated:
                    cut = index_text[:_MEMORY_INDEX_MAX_CHARS]
                    newline = cut.rfind("\n")
                    if newline > 0:
                        cut = cut[:newline]
                    index_text = cut.rstrip()
                lines.append(index_text)
                if truncated:
                    lines.append(
                        f"(Index truncated at {_MEMORY_INDEX_MAX_CHARS} chars — "
                        'use memory(action="find", query="...") for older entries.)'
                    )
                lines.append("")

        summary_path = os.path.join(memory_dir, "summary.md")
        if os.path.isfile(summary_path):
            with open(summary_path, encoding="utf-8") as f:
                summary_text = f.read().strip()[:_MEMORY_SUMMARY_MAX_CHARS]
            if summary_text:
                lines.append("### Memory Summary")
                lines.append(summary_text)
                lines.append("")

        lines.extend([
            "Search/recall: `memory(action=\"find\", query=\"keyword\")` lists matching entries "
            "with descriptions; then `memory(action=\"read\", name=\"<name>\")` reads the full body.",
        ])
    except Exception:
        logger.debug("Memory system unavailable, using fallback", exc_info=True)
        # 记忆系统初始化失败：给出降级提示，不阻塞任何请求
        lines.extend([
            f"Memory directory: `{memory_dir}`",
            'Search: `memory(action="find", query="keyword")`, then `memory(action="read", name="...")`.',
        ])
    return lines
