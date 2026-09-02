"""Shared tools - used by both main agent and sub-agent."""

from __future__ import annotations

from core.config import Config
from core.tools.shared.base import Tool
from core.tools.shared.read import ReadTool
from core.tools.shared.write import WriteTool
from core.tools.shared.edit import EditTool
from core.tools.shared.bash import BashTool
from core.tools.shared.grep import GrepTool
from core.tools.shared.find import FindTool
from core.tools.shared.browser import BrowserTool
from core.tools.shared.web_search import WebSearchTool
from core.tools.shared.memory import MemoryTool
from core.tools.shared.llm_tool import LLMTool
from core.tools.shared.python_tool import PythonTool
from core.tools.shared.todo import TodoWriteTool
from core.tools.shared.latex import LatexTool
from core.tools.shared.message_bus_tool import MessageBusTool
from core.tools.shared.cron_tool import CronTool
from core.tools.shared.read_tool_result import ReadToolResultTool
from core.tools.shared.temp import TempTool
from core.tools.shared.loop import LoopTool
from core.tools.shared.pdf2markdown import PDF2MarkdownTool


def create_shared_tools(
    cwd: str = ".",
    workspace_uuid: str = "",
    session_manager=None,
    config: Config | None = None,
    cron_task_id: str = "",
) -> list[Tool]:
    """Create tools available to both main agent and sub-agent.

    Args:
        config: Global config. If llm_model is None, LLMTool is excluded.
        cron_task_id: Cron task ID if triggered by cron (for loop tool).
    """
    tools = [
        ReadTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        WriteTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        EditTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        BashTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        GrepTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        FindTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        BrowserTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        WebSearchTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        MemoryTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        PythonTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager, config=config),
        TodoWriteTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        LatexTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        MessageBusTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        CronTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        ReadToolResultTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        TempTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
        LoopTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager, cron_task_id=cron_task_id),
        PDF2MarkdownTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager, config=config),
    ]
    # Only include LLMTool if llm_model is configured
    if config is None or config.llm_model is not None:
        tools.append(LLMTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager, config=config))
    return tools
