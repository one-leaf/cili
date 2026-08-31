"""Skill tool - list and read built-in global skills.

Shared by both RootAgent (scans core/skills/root/) and
SubAgent (scans core/skills/sub/).
Both always include core/skills/shared/ as well.
"""

from __future__ import annotations

import os
import re

from core.config import PROJECT_ROOT
from core.tools.shared.base import Tool, ToolResult

# Shared skills directory (always included)
_SHARED_SKILLS_DIR = str(PROJECT_ROOT / "core" / "skills" / "shared")


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


def list_skills(skills_dir: str) -> list[dict]:
    """Scan *skills_dir* and core/skills/shared/ for skill directories.

    Returns list of dicts with: id, name, description.
    Shared skills have id prefixed with 'shared/' for disambiguation.
    """
    skills = []
    for dir_path, prefix in [(skills_dir, ""), (_SHARED_SKILLS_DIR, "shared/")]:
        if not os.path.isdir(dir_path):
            continue

        for entry in sorted(os.listdir(dir_path)):
            skill_dir = os.path.join(dir_path, entry)
            skill_file = os.path.join(skill_dir, "skill.md")

            if not os.path.isdir(skill_dir) or not os.path.isfile(skill_file):
                continue

            try:
                with open(skill_file, encoding="utf-8") as f:
                    meta = _parse_skill_frontmatter(f.read())
                skills.append({
                    "id": f"{prefix}{entry}",
                    "name": meta.get("name", entry),
                    "description": meta.get("description", ""),
                })
            except Exception:
                continue

    return skills


def read_skill(skills_dir: str, skill_id: str) -> str | None:
    """Read the full content of a skill by its directory name.

    Handles both role-specific skills and shared skills (prefixed 'shared/').
    Returns the full skill.md content, or None if not found.
    """
    if skill_id.startswith("shared/"):
        skill_name = skill_id[7:]
        skill_file = os.path.join(_SHARED_SKILLS_DIR, skill_name, "skill.md")
    else:
        skill_file = os.path.join(skills_dir, skill_id, "skill.md")

    if not os.path.isfile(skill_file):
        return None

    try:
        with open(skill_file, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


class SkillTool(Tool):
    """Access built-in global skills. Parameterised by the role-specific skills directory."""

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

    def __init__(self, skills_dir: str, role_label: str = "built-in", **kwargs):
        """
        Args:
            skills_dir: Absolute path to the role-specific skills directory
                        (e.g. core/skills/root or core/skills/sub).
            role_label: Label used in description and messages ("built-in" or "sub").
        """
        super().__init__(**kwargs)
        self._skills_dir = skills_dir
        self._role_label = role_label
        self.description = (
            f"Access {role_label} skills for specialized tasks.\n\n"
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
        skills = list_skills(self._skills_dir)
        if not skills:
            return ToolResult(f"No {self._role_label} skills available.")

        lines = [f"Available {self._role_label} skills ({len(skills)}):", ""]
        for s in skills:
            lines.append(f"  [{s['id']}] {s['name']}")
            if s["description"]:
                lines.append(f"    {s['description']}")
            lines.append("")
        lines.append("Use skill(action='read', skill_id='...') to read full content.")
        return ToolResult("\n".join(lines))

    def _read_skill(self, skill_id: str) -> ToolResult:
        if not re.match(r'^(shared/)?[a-zA-Z0-9_-]+$', skill_id):
            return ToolResult(f"Error: invalid skill_id '{skill_id}'", error=True)

        content = read_skill(self._skills_dir, skill_id)
        if content is None:
            available = [s["id"] for s in list_skills(self._skills_dir)]
            return ToolResult(
                f"Error: skill '{skill_id}' not found. "
                f"Available: {', '.join(available) if available else 'none'}",
                error=True,
            )
        return ToolResult(content)
