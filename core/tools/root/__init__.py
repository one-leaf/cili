"""RootAgent-only tools - main agent exclusive (not available in sub-agent)."""

from __future__ import annotations

from core.config import PROJECT_ROOT
from core.tools.shared.skill import SkillTool
from core.tools.root.subagent_tool import SubAgentTool
from core.tools.root.ask_user import AskUserTool


def create_root_tools(
    cwd: str = ".", workspace_uuid: str = "", session_manager=None
) -> list:
    """Create tools exclusive to the root agent."""
    return [
        SkillTool(
            skills_dir=str(PROJECT_ROOT / "core" / "skills" / "root"),
            role_label="built-in",
            cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager,
        ),
        SubAgentTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        AskUserTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
    ]
