"""统一工具注册表：角色白名单驱动的工具实例化。

TOOL_REGISTRY 把工具名映射到工厂函数；create_tools() 按角色配置中的
tools 白名单顺序实例化。取代旧的 shared/root/sub 分层硬编码工厂。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from core.config import Config
from core.role_config import RoleConfig, load_role
from core.tools.agent import AgentTool
from core.tools.ask_user import AskUserTool
from core.tools.base import Tool
from core.tools.bash import BashTool
from core.tools.browser import BrowserTool
from core.tools.clock import ClockTool
from core.tools.cron import CronTool
from core.tools.deepseek_web import DeepSeekReadUrlTool, DeepSeekWebSearchTool
from core.tools.edit import EditTool
from core.tools.glob import GlobTool
from core.tools.grep import GrepTool
from core.tools.latex import LatexTool
from core.tools.loop import LoopTool
from core.tools.mail import MailTool, usable_accounts
from core.tools.memory import MemoryTool
from core.tools.message_bus import MessageBusTool
from core.tools.office2pdf import Office2PDFTool
from core.tools.pdf2markdown import PDF2MarkdownTool
from core.tools.pwsh import PwshTool
from core.tools.python import PythonTool
from core.tools.read import ReadTool
from core.tools.read_tool_result import ReadToolResultTool
from core.tools.send_notification import SendNotificationTool
from core.tools.send_user_file import SendUserFileTool
from core.tools.session_search import SessionSearchTool
from core.tools.skill import SkillTool
from core.tools.temp import TempTool
from core.tools.textbook_find import TextbookFindTool
from core.tools.textbook_original import TextbookOriginalTool
from core.tools.textbook_outline import TextbookOutlineTool
from core.tools.textbook_read import TextbookReadTool
from core.tools.textbook_search import TextbookSearchTool
from core.tools.todo import TodoWriteTool
from core.tools.tool_search import ToolSearchTool
from core.tools.web_search import WebSearchTool
from core.tools.workflow import WorkflowTool
from core.tools.write import WriteTool

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class ToolBuildContext:
    """工具实例化的统一入参（新增依赖只改这里）。"""

    role_cfg: RoleConfig
    cwd: str = "."
    workspace_uuid: str = ""
    session: Any = None
    config: Config | None = None
    permission_store: Any = None
    permission_rules: Any = None
    permission_policy: Any = None


Factory = Callable[[ToolBuildContext], Tool]


def _make_skill(ctx: ToolBuildContext) -> Tool:
    return SkillTool(
        role=ctx.role_cfg.name,
        cwd=ctx.cwd, workspace_uuid=ctx.workspace_uuid, session=ctx.session,
    )


def _factory(cls: type, *, needs_config: bool = False, needs_permission: bool = False) -> Factory:
    def factory(ctx: ToolBuildContext) -> Tool:
        kwargs: dict[str, Any] = dict(
            cwd=ctx.cwd, workspace_uuid=ctx.workspace_uuid, session=ctx.session,
            permission_rules=ctx.permission_rules,
        )
        if needs_config:
            kwargs["config"] = ctx.config
        if needs_permission:
            kwargs["permission_store"] = ctx.permission_store
            kwargs["permission_policy"] = ctx.permission_policy
        return cls(**kwargs)
    return factory


# 全部工具类（能力集合与注册表共用同一清单）
TOOL_CLASSES: tuple[type[Tool], ...] = (
    AgentTool, AskUserTool, BashTool, BrowserTool, ClockTool, CronTool,
    DeepSeekReadUrlTool, DeepSeekWebSearchTool, EditTool, GlobTool, GrepTool,
    LatexTool, LoopTool, MailTool, MemoryTool, MessageBusTool, Office2PDFTool,
    PDF2MarkdownTool, PwshTool, PythonTool, ReadTool,
    ReadToolResultTool, SendNotificationTool, SendUserFileTool, SessionSearchTool,
    SkillTool, TempTool,
    TextbookFindTool, TextbookOriginalTool, TextbookOutlineTool, TextbookReadTool,
    TextbookSearchTool, TodoWriteTool, ToolSearchTool, WebSearchTool, WorkflowTool,
    WriteTool,
)


def capability_tool_names(attr: str) -> frozenset[str]:
    """按类属性汇总工具名（如 ``mutates_workspace`` / ``streams_output``）。"""
    return frozenset(
        cls.name for cls in TOOL_CLASSES if getattr(cls, attr, False)
    )


TOOL_REGISTRY: dict[str, Factory] = {
    "read": _factory(ReadTool, needs_permission=True),
    "write": _factory(WriteTool, needs_permission=True),
    "edit": _factory(EditTool, needs_permission=True),
    "bash": _factory(BashTool, needs_permission=True),
    "pwsh": _factory(PwshTool, needs_permission=True),
    "grep": _factory(GrepTool, needs_permission=True),
    "glob": _factory(GlobTool, needs_permission=True),
    "browser": _factory(BrowserTool, needs_permission=True),
    "web_search": _factory(WebSearchTool),
    # DeepSeek 服务端联网工具（客户端包装；按模型门控，见 core/llm/deepseek_capabilities.py）
    "deepseek_web_search": _factory(DeepSeekWebSearchTool),
    "deepseek_read_url": _factory(DeepSeekReadUrlTool),
    "memory": _factory(MemoryTool),
    "python": _factory(PythonTool, needs_config=True, needs_permission=True),
    "todo_write": _factory(TodoWriteTool),
    "latex": _factory(LatexTool, needs_permission=True),
    "message_bus": _factory(MessageBusTool),
    "cron": _factory(CronTool),
    "clock": _factory(ClockTool),
    "read_tool_result": _factory(ReadToolResultTool),
    "session_search": _factory(SessionSearchTool),
    "temp": _factory(TempTool),
    "loop": _factory(LoopTool),
    # 收发邮件：无可用账号时在 create_tools 里整体裁掉（见下方门控）
    "mail": _factory(MailTool, needs_permission=True),
    "pdf2markdown": _factory(PDF2MarkdownTool, needs_config=True, needs_permission=True),
    "office2pdf": _factory(Office2PDFTool, needs_permission=True),
    "skill": _make_skill,
    "agent": _factory(AgentTool, needs_config=True, needs_permission=True),
    "ask_user": _factory(AskUserTool),
    "workflow": _factory(WorkflowTool),
    "tool_search": _factory(ToolSearchTool),
    "textbook_find": _factory(TextbookFindTool),
    "textbook_outline": _factory(TextbookOutlineTool),
    "textbook_read": _factory(TextbookReadTool),
    "textbook_search": _factory(TextbookSearchTool),
    "textbook_original": _factory(TextbookOriginalTool),
    # 接口专属（按回合门控，仅声明它的接入端回合可见）：把工作区内文件发给用户
    "send_user_file": _factory(SendUserFileTool),
    # 全局收件箱通知（master / worker / workflow 可用）
    "send_notification": _factory(SendNotificationTool),
}


def _workflow_tool_allowed(session) -> bool:
    """工作流工具：维护会话、结果会话、普通 master 可用；步骤会话不可用。

    结果会话只开放运行类动作（run/run_step/stop/status/send_to_user 等），
    create/update/delete 由工具自身按面板会话门控。
    """
    metadata = getattr(session, "metadata", None)
    if not isinstance(metadata, dict):
        return True
    if isinstance(metadata.get("workflow"), dict):
        return False
    return True


def create_tools(
    role_cfg: RoleConfig | None = None,
    cwd: str = ".",
    workspace_uuid: str = "",
    session=None,
    config: Config | None = None,
    permission_store=None,
    permission_rules=None,
    permission_policy=None,
    role: str | None = None,
) -> list[Tool]:
    """按角色工具白名单实例化工具。"""
    if role_cfg is None:
        role_cfg = load_role(role or "master", config)

    ctx = ToolBuildContext(
        role_cfg=role_cfg, cwd=cwd, workspace_uuid=workspace_uuid, session=session,
        config=config, permission_store=permission_store,
        permission_rules=permission_rules, permission_policy=permission_policy,
    )
    tools: list[Tool] = []
    for name in role_cfg.tools:
        factory = TOOL_REGISTRY.get(name)
        if factory is None:
            logger.warning(f"[registry] 角色 {role_cfg.name!r} 白名单中的工具 {name!r} 未注册，已跳过")
            continue
        if name == "workflow" and not _workflow_tool_allowed(session):
            continue
        # 没有任何可用邮箱账号时工具整体不出现（有账号但都绑定其他工作区仍显示，
        # 调用时再返回「当前工作区无可用账号」，让模型能向用户解释原因）
        if name == "mail" and not usable_accounts():
            continue
        try:
            tool = factory(ctx)
            tool.agent_role = role_cfg.name
            tools.append(tool)
        except Exception:
            # 用 exception 保留堆栈：工具构造失败（如子类 __init__ 未透传 **kwargs）
            # 会让该工具静默缺席，只有堆栈能定位根因。
            logger.exception(f"[registry] 实例化工具 {name!r} 失败，已跳过")
    return tools


def get_tool_by_name(tools: list[Tool], name: str) -> Tool | None:
    """在角色工具列表中按 name 查找（工具数少，线性扫描即可）。"""
    for tool in tools:
        if tool.name == name:
            return tool
    return None
