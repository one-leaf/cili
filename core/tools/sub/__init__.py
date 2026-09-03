"""Sub-agent exclusive tools."""

from __future__ import annotations

from core.config import Config, PROJECT_ROOT
from core.tools.shared import create_shared_tools
from core.tools.shared.skill import SkillTool


def create_sub_tools(
    cwd: str = ".",
    workspace_uuid: str = "",
    session_manager=None,
    config: Config | None = None,
) -> list:
    """Create tools for SubAgent: shared tools + sub-specific Skill tool."""
    return create_shared_tools(
        cwd=cwd,
        workspace_uuid=workspace_uuid,
        session_manager=session_manager,
        config=config,
    ) + [
        SkillTool(
            skills_dir=str(PROJECT_ROOT / "core" / "skills" / "sub"),
            role_label="sub",
            cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager,
        ),
    ]
