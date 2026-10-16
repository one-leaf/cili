"""Skill tool - list and read built-in global skills.

Skills live flat in core/skills/{name}/skill.md. Each skill's frontmatter
declares its applicable roles via ``roles: [master, worker, lite]``; a skill
without ``roles`` is visible to every role.
"""

from __future__ import annotations

import os
import re

from core.config import PROJECT_ROOT
from core.tools.base import Tool, ToolResult

# 统一技能目录（平铺，取代原 shared/root/sub 分层）
_SKILLS_DIR = str(PROJECT_ROOT / "core" / "skills")


def _parse_skill_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter from skill.md content.

    Returns dict with name, description, tags, etc.
    Returns empty dict if no valid frontmatter found.
    """
    if not content.startswith("---"):
        return {}

    end_idx = content.find("---", 3)
    if end_idx == -1:
        return {}

    frontmatter_text = content[3:end_idx].strip()
    result = {}

    for line in frontmatter_text.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()

        # Remove quotes
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        # Parse tags array
        elif value.startswith("[") and value.endswith("]"):
            value = [t.strip().strip('"').strip("'") for t in value[1:-1].split(",") if t.strip()]

        result[key] = value

    return result


def _as_frontmatter_list(value) -> list[str]:
    """frontmatter 列表字段归一化：list 或逗号分隔字符串 → str 列表。"""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()]


def _visible_for(meta: dict, role: str, interface: str | None = None) -> bool:
    """frontmatter roles + interfaces 过滤：缺省视为全部可见。

    ``interfaces`` 语义：声明了 ``interfaces`` 的技能只在列出的接口可见。
    接口未声明（None：cron 回合 / 未设置接口的交互回合 / 子代理）时一律
    不可见——宁缺勿猜，与 ``interface_caps`` 的未声明语义一致。

    roles 维度语义与原 ``_visible_for_role`` 相同。
    """
    roles = meta.get("roles")
    if roles and role not in _as_frontmatter_list(roles):
        return False
    interfaces = meta.get("interfaces")
    if interfaces and (interface is None or interface not in _as_frontmatter_list(interfaces)):
        return False
    return True


def list_skills(role: str, interface: str | None = None) -> list[dict]:
    """Scan core/skills/ for skill directories visible to *role* + *interface*.

    Returns list of dicts with: id, name, description.
    """
    skills = []
    if not os.path.isdir(_SKILLS_DIR):
        return skills

    for entry in sorted(os.listdir(_SKILLS_DIR)):
        skill_dir = os.path.join(_SKILLS_DIR, entry)
        skill_file = os.path.join(skill_dir, "skill.md")

        if not os.path.isdir(skill_dir) or not os.path.isfile(skill_file):
            continue

        try:
            with open(skill_file, encoding="utf-8") as f:
                meta = _parse_skill_frontmatter(f.read())
            if not _visible_for(meta, role, interface):
                continue
            skills.append({
                "id": entry,
                "name": meta.get("name", entry),
                "description": meta.get("description", ""),
                "when_to_use": meta.get("when_to_use", ""),
            })
        except Exception:
            continue

    return skills


def read_skill(role: str, skill_id: str, interface: str | None = None) -> str | None:
    """Read the full content of a skill by its directory name.

    Returns the full skill.md content, or None if not found or the skill is
    not visible to *role*/*interface* (frontmatter roles + interfaces filter).
    """
    if not re.match(r'^[a-zA-Z0-9_-]+$', skill_id):
        return None

    skill_file = os.path.join(_SKILLS_DIR, skill_id, "skill.md")
    if not os.path.isfile(skill_file):
        return None

    try:
        with open(skill_file, encoding="utf-8") as f:
            content = f.read()
        if not _visible_for(_parse_skill_frontmatter(content), role, interface):
            return None
        return content
    except Exception:
        return None


class SkillTool(Tool):
    """Access built-in global skills. Parameterised by the agent role."""

    name = "skill"
    concurrency_safe = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "read"],
                "description": "Action type: 'list' (show available skills) or 'read' (get skill content).",
            },
            "skill_id": {
                "type": "string",
                "description": "Skill directory name (e.g., 'large-file-processing'). Required for 'read' action.",
            },
            "file": {
                "type": "string",
                "description": (
                    "Optional sub-file path relative to the skill directory (e.g. 'references/repair.md') "
                    "for progressive disclosure — read only the phase you need. Without it, 'read' returns "
                    "the full skill.md. Only .md files inside the skill directory are allowed."
                ),
            },
        },
        "required": ["action"],
    }

    def __init__(self, role: str = "master", **kwargs):
        """role: agent 角色名（master/worker/lite），决定可见技能集合。"""
        super().__init__(**kwargs)
        self._role = role
        self._interface: str | None = None  # 当前驱动接口，由 set_interface_profile 推送
        self.description = (
            f"Access built-in skills for the {role} role.\n\n"
            "**IMPORTANT: Always check available skills FIRST when user requests match these patterns:**\n"
            "- Learning/studying: 我想学习、学习、深入了解、怎么学\n"
            "- Research/fact-check: 帮我查查、查一下、研究、调查、了解、事实核查、验证\n"
            "- Code review: 审查、review、检查代码、代码质量\n"
            "- File processing: 大文件、翻译文件、批量处理\n"
            "- Task delegation: 复杂任务、多步骤、委派给子代理\n\n"
            "## Actions:\n"
            "- **list**: Show all available skills with descriptions (use this FIRST to find matching skill)\n"
            "- **read**: Read full skill content by skill_id (after finding matching skill from list)\n\n"
            "**Workflow:** User request → skill(action='list') to find matching skill → skill(action='read', skill_id='...') to get instructions → Follow skill instructions. "
            "Large skills split procedures into `references/*.md` sub-files, with a phase map in the skill body naming which sub-file each phase needs. "
            "Read one with skill(action='read', skill_id='...', file='references/<name>.md') exactly at the phase that needs it — never preload all sub-files."
        )

    def execute(self, action: str = "list", skill_id: str | None = None,
                file: str | None = None) -> ToolResult:
        if action == "list":
            return self._list_skills()
        elif action == "read":
            if not skill_id:
                return ToolResult("Error: 'skill_id' is required for 'read' action", error=True)
            return self._read_skill(skill_id, file)
        else:
            return ToolResult(f"Error: unknown action '{action}'", error=True)

    def set_interface_profile(self, profile) -> None:
        """接收当前回合的能力档案：技能按档案的接口名过滤。"""
        self._interface = profile.name if profile is not None else None

    def _list_skills(self) -> ToolResult:
        skills = list_skills(self._role, self._interface)
        if not skills:
            return ToolResult(f"No skills available for role '{self._role}'.")

        lines = [f"Available skills for {self._role} ({len(skills)}):", ""]
        for s in skills:
            lines.append(f"  [{s['id']}] {s['name']}")
            if s["description"]:
                lines.append(f"    {s['description']}")
            if s.get("when_to_use"):
                lines.append(f"    Use when: {s['when_to_use']}")
            lines.append("")
        lines.append("Use skill(action='read', skill_id='...') to read full content.")
        return ToolResult("\n".join(lines))

    def _read_skill(self, skill_id: str, file: str | None = None) -> ToolResult:
        content = read_skill(self._role, skill_id, self._interface)
        if content is None:
            available = [s["id"] for s in list_skills(self._role, self._interface)]
            return ToolResult(
                f"Error: skill '{skill_id}' not found. "
                f"Available: {', '.join(available) if available else 'none'}",
                error=True,
            )
        # 前置技能目录：agent 由此得知子文件与捆绑资源的落点，可用 read
        # 工具直接访问；正文里的 $SKILL_DIR 占位符一并替换。
        skill_dir = os.path.join(_SKILLS_DIR, skill_id)
        header = f"Base directory for this skill: {skill_dir}\n\n"
        content = content.replace("${SKILL_DIR}", skill_dir).replace("$SKILL_DIR", skill_dir)
        if not file:
            return ToolResult(header + content)
        return self._read_sub_file(skill_id, file, header)

    @staticmethod
    def _read_sub_file(skill_id: str, file: str, header: str = "") -> ToolResult:
        """读取技能目录内的子文件（渐进式披露）。

        仅允许相对 .md 路径：禁绝对路径、反斜杠、``..`` 与隐藏文件，
        越界一律拒绝（fail-closed）。
        """
        if not re.match(r'^[a-zA-Z0-9_][a-zA-Z0-9\-/]*\.md$', file) or '..' in file:
            return ToolResult(
                f"Error: invalid sub-file path '{file}' "
                f"(relative .md path inside the skill directory only)",
                error=True,
            )
        path = os.path.join(_SKILLS_DIR, skill_id, *file.split("/"))
        if not os.path.isfile(path):
            return ToolResult(
                f"Error: sub-file '{file}' not found in skill '{skill_id}'", error=True
            )
        try:
            with open(path, encoding="utf-8") as f:
                return ToolResult(header + f.read())
        except Exception:
            return ToolResult(f"Error: failed to read sub-file '{file}'", error=True)
