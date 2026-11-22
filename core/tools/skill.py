"""Skill tool - list and read built-in global skills.

Skills live flat in core/skills/{name}/skill.md. Each skill's frontmatter
declares its applicable roles via ``roles: [master, worker, lite]``; a skill
without ``roles`` is visible to every role.
"""

from __future__ import annotations

import os
import re

from core import skills_catalog
from core.tools.base import Tool, ToolResult


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
        skills = skills_catalog.list_skills(self._role, self._interface)
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
        content = skills_catalog.read_skill(self._role, skill_id, self._interface)
        if content is None:
            available = [s["id"] for s in skills_catalog.list_skills(self._role, self._interface)]
            return ToolResult(
                f"Error: skill '{skill_id}' not found. "
                f"Available: {', '.join(available) if available else 'none'}",
                error=True,
            )
        # 前置技能目录：agent 由此得知子文件与捆绑资源的落点，可用 read
        # 工具直接访问；正文里的 $SKILL_DIR 占位符一并替换。
        skill_dir = os.path.join(skills_catalog.SKILLS_DIR, skill_id)
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
        path = os.path.join(skills_catalog.SKILLS_DIR, skill_id, *file.split("/"))
        if not os.path.isfile(path):
            return ToolResult(
                f"Error: sub-file '{file}' not found in skill '{skill_id}'", error=True
            )
        try:
            with open(path, encoding="utf-8") as f:
                return ToolResult(header + f.read())
        except Exception:
            return ToolResult(f"Error: failed to read sub-file '{file}'", error=True)
