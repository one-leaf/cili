"""统一工具注册表：角色白名单驱动的工具实例化。

TOOL_REGISTRY 把工具名映射到工厂函数；create_tools() 按角色配置中的
tools 白名单顺序实例化。取代旧的 shared/root/sub 分层硬编码工厂。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from core.agent_config import AgentRoleConfig, load_agent_role
from core.config import Config
from core.tools.base import Tool
from core.tools.ask_user import AskUserTool
from core.tools.bash import BashTool
from core.tools.browser import BrowserTool
from core.tools.cron_tool import CronTool
from core.tools.edit import EditTool
from core.tools.find import FindTool
from core.tools.grep import GrepTool
from core.tools.latex import LatexTool
from core.tools.loop import LoopTool
from core.tools.memory import MemoryTool
from core.tools.message_bus_tool import MessageBusTool
from core.tools.pdf2markdown import PDF2MarkdownTool
from core.tools.pwsh import PwshTool
from core.tools.python_tool import PythonTool
from core.tools.read import ReadTool
from core.tools.read_tool_result import ReadToolResultTool
from core.tools.skill import SkillTool
from core.tools.agent_tool import AgentTool
from core.tools.temp import TempTool
from core.tools.todo import TodoWriteTool
from core.tools.web_search import WebSearchTool
from core.tools.write import WriteTool

logger = logging.getLogger(__name__)

Factory = Callable[[AgentRoleConfig, str, str, Any, Config | None, Any], Tool]


def _make_skill(role_cfg, cwd, workspace_uuid, session_manager, config, approval_store) -> Tool:
    return SkillTool(
        role=role_cfg.name,
        cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager,
    )


def _factory(cls: type, *, needs_config: bool = False, needs_approval: bool = False) -> Factory:
    def factory(role_cfg, cwd, workspace_uuid, session_manager, config, approval_store) -> Tool:
        kwargs: dict[str, Any] = dict(
            cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager,
        )
        if needs_config:
            kwargs["config"] = config
        if needs_approval:
            kwargs["approval_store"] = approval_store
        return cls(**kwargs)
    return factory


TOOL_REGISTRY: dict[str, Factory] = {
    "read": _factory(ReadTool),
    "write": _factory(WriteTool),
    "edit": _factory(EditTool),
    "bash": _factory(BashTool, needs_approval=True),
    "pwsh": _factory(PwshTool, needs_approval=True),
    "grep": _factory(GrepTool),
    "find": _factory(FindTool),
    "browser": _factory(BrowserTool),
    "web_search": _factory(WebSearchTool),
    "memory": _factory(MemoryTool),
    "python": _factory(PythonTool, needs_config=True),
    "todo": _factory(TodoWriteTool),
    "latex": _factory(LatexTool),
    "message_bus": _factory(MessageBusTool),
    "cron": _factory(CronTool),
    "read_tool_result": _factory(ReadToolResultTool),
    "temp": _factory(TempTool),
    "loop": _factory(LoopTool),
    "pdf2markdown": _factory(PDF2MarkdownTool, needs_config=True),
    "skill": _make_skill,
    "agent": _factory(AgentTool, needs_config=True, needs_approval=True),
    "ask_user": _factory(AskUserTool),
}


def create_tools(
    role_cfg: AgentRoleConfig | None = None,
    cwd: str = ".",
    workspace_uuid: str = "",
    session_manager=None,
    config: Config | None = None,
    approval_store=None,
    role: str | None = None,
) -> list[Tool]:
    """按角色工具白名单实例化工具。

    role_cfg 缺省时回退到 load_agent_role(role or "master")，便于旧调用点
    （conftest / prompts）不显式传角色配置即可获得 master 全量工具。
    """
    if role_cfg is None:
        role_cfg = load_agent_role(role or "master", config)

    tools: list[Tool] = []
    for name in role_cfg.tools:
        factory = TOOL_REGISTRY.get(name)
        if factory is None:
            logger.warning(f"[registry] 角色 {role_cfg.name!r} 白名单中的工具 {name!r} 未注册，已跳过")
            continue
        try:
            tools.append(factory(role_cfg, cwd, workspace_uuid, session_manager, config, approval_store))
        except Exception as e:
            logger.warning(f"[registry] 实例化工具 {name!r} 失败: {e}")
    return tools
