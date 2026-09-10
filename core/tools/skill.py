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


def _visible_for_role(meta: dict, role: str) -> bool:
    """frontmatter roles 过滤：缺省视为全部角色可见。"""
    roles = meta.get("roles")
    if not roles:
        return True
    if isinstance(roles, list):
        return role in roles
    return role in [r.strip() for r in str(roles).split(",")]


def list_skills(role: str) -> list[dict]:
    """Scan core/skills/ for skill directories visible to *role*.

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
            if not _visible_for_role(meta, role):
                continue
            skills.append({
                "id": entry,
                "name": meta.get("name", entry),
                "description": meta.get("description", ""),
            })
        except Exception:
            continue

    return skills


def read_skill(role: str, skill_id: str) -> str | None:
    """Read the full content of a skill by its directory name.

    Returns the full skill.md content, or None if not found or the skill is
    not visible to *role* (frontmatter roles filter).
    """
    if not re.match(r'^[a-zA-Z0-9_-]+$', skill_id):
        return None

    skill_file = os.path.join(_SKILLS_DIR, skill_id, "skill.md")
    if not os.path.isfile(skill_file):
        return None

    try:
        with open(skill_file, encoding="utf-8") as f:
            content = f.read()
        if not _visible_for_role(_parse_skill_frontmatter(content), role):
            return None
        return content
    except Exception:
        return None


class SkillTool(Tool):
    """Access built-in global skills. Parameterised by the agent role."""

    name = "skill"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "read"],
                "description": "Action type: 'list' (show available skills) or 'read' (get full skill content).",
            },
            "skill_id": {
                "type": "string",
                "description": "Skill directory name (e.g., 'large-file-processing'). Required for 'read' action.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, role: str = "master", **kwargs):
        """role: agent 角色名（master/worker/lite），决定可见技能集合。"""
        super().__init__(**kwargs)
        self._role = role
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
            "**Workflow:** User request → skill(action='list') to find matching skill → skill(action='read', skill_id='...') to get instructions → Follow skill instructions"
        )

    def execute(self, action: str = "list", skill_id: str | None = None) -> ToolResult:
        if action == "list":
            return self._list_skills()
        elif action == "read":
            if not skill_id:
                return ToolResult("Error: 'skill_id' is required for 'read' action", error=True)
            return self._read_skill(skill_id)
        else:
            return ToolResult(f"Error: unknown action '{action}'", error=True)

    def _list_skills(self) -> ToolResult:
        skills = list_skills(self._role)
        if not skills:
            return ToolResult(f"No skills available for role '{self._role}'.")

        lines = [f"Available skills for {self._role} ({len(skills)}):", ""]
        for s in skills:
            lines.append(f"  [{s['id']}] {s['name']}")
            if s["description"]:
                lines.append(f"    {s['description']}")
            lines.append("")
        lines.append("Use skill(action='read', skill_id='...') to read full content.")
        return ToolResult("\n".join(lines))

    def _read_skill(self, skill_id: str) -> ToolResult:
        content = read_skill(self._role, skill_id)
        if content is None:
            available = [s["id"] for s in list_skills(self._role)]
            return ToolResult(
                f"Error: skill '{skill_id}' not found. "
                f"Available: {', '.join(available) if available else 'none'}",
                error=True,
            )
        return ToolResult(content)
