"""统一 SessionRunner 类：合并原交互式/自主式执行引擎。

行为差异由角色 JSON（core/agents/{role}.json）驱动：
- ``mode=interactive``（master）→ 会话持久化、流式输出、ask_user/审批、可恢复循环
- ``mode=autonomous``（worker/lite）→ pinned 任务消息、自主执行、检查阶段/预算预警/进度持久化

系统提示词由 ``core/prompt_builder.build_system_prompt()`` 按角色 JSON 的
blocks 拼装；注入型 user 层（claude_md/context）在 ``_get_messages_with_header``
中合并并做防连续 user 合并。模型取 ``config.{role}_model or config.model``。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from core.config import Config, PROJECT_ROOT
from core.llm import create_llm_client, format_llm_error
from core.fs_utils import atomic_write_json
from core.session import build_exec_log_data
from core.base_session_runner import BaseSessionRunner
from core.output_sink import OutputSink, layer_sink
from core.session import SessionStore, generate_short_id
from core.role_config import load_role
from core.session_runner_runtime.loop import (
    BUDGET_FINAL_PROMPT,
    BUDGET_FINAL_RATIO,
    BUDGET_WARN_PROMPT,
    BUDGET_WARN_RATIO,
    CHECK_PROMPT,
    TIMEOUT_WRAPUP_PROMPT,
    Loop,
    LoopPolicy,
)
from core.prompt_builder import USER_LAYER_GENERATORS, assemble_context, build_system_prompt
from core.tools import create_tools, get_tool_by_name
from core.tools.approval import (
    APPROVE_LABEL,
    META_KEY,
    REJECT_LABEL,
    REMEMBER_LABEL,
    ApprovalStore,
    build_approved_commands_section,
    build_approval_question,
)

logger = logging.getLogger(__name__)


class _SessionRef:
    """简单的 session 引用，供工具获取 session 标识和目录。

    autonomous 模式使用 exec_id 作为 session 标识。
    """

    def __init__(self, session_id: str, session_dir: Path | None = None):
        self.session_id = session_id
        self.session_dir = session_dir


class SessionRunner(BaseSessionRunner):
    """统一 SessionRunner：interactive（master）/ autonomous（worker/lite）由角色配置分叉。"""

    def __init__(
        self,
        config: Config,
        role: str = "master",
        cwd: str | None = None,
        workspace_uuid: str = "",
        task: str = "",
        plan: list[str] | None = None,
        session_dir: Path | None = None,
        stop_check: Callable[[], bool] | None = None,
        exec_id: str = "",
        temperature: float | None = None,
        approval_store=None,
        max_consecutive_failures: int | None = None,
        delegation_depth: int = 0,
        run_mode: str | None = None,
    ):
        """Initialize SessionRunner.

        Args:
            config: 全局配置
            role: 角色名（master/worker/lite），决定工具白名单/行为开关/prompt
            cwd: 工作目录
            workspace_uuid: 工作区 UUID
            task: autonomous 模式的任务描述
            plan: autonomous 模式的可选执行计划
            session_dir: autonomous 模式的消息持久化目录
            stop_check: 返回 True 表示父代理已停止
            exec_id: autonomous 模式的执行 ID（作为 session 标识）
            temperature: 可选的 LLM temperature 覆盖（0.0~1.0）
            approval_store: autonomous 模式共享的会话级审批存储
            max_consecutive_failures: 最大连续失败次数，None 时取角色配置
            delegation_depth: 委派深度（master=0；depth1 子代理仅可委派 lite；depth≥2 不可再委派）
            run_mode: 运行模式覆盖（interactive/autonomous），None 时取角色 JSON 的 mode
        """
        self.role = role
        self.role_cfg = load_role(role, config)
        self.model = config.model_for_role(role)
        self._cwd_init = os.path.abspath(cwd or os.getcwd())
        self._temperature = temperature
        # run_mode 与 role 解耦：默认取角色 JSON 的 mode，接口层可显式覆盖
        # （例如将来 QQ 接口用 interactive 行为但非 master 画像）。
        self._mode = run_mode or self.role_cfg.mode
        self.delegation_depth = delegation_depth
        self._turn_iterations = 0  # 单个用户轮次内的累计迭代（跨 ask_user/session resume 不重置）
        # 工具调用累计计数（每批工具调用数累加）
        self._tool_call_count = 0
        # 事件发布回调（由 session_tool / background 注入，用于广播 session_progress）
        self._event_publisher = None
        # session_progress 事件节流：距上次发送不足 0.5s 则跳过，避免高频 SSE 事件堵塞前端
        self._last_progress_event_time: float = 0.0

        if self._mode == "interactive":
            self._init_interactive(workspace_uuid)
            init_session_dir = self.sessions_dir / self.current_session_id
        else:
            self._init_autonomous(task, plan, exec_id, session_dir, stop_check)
            init_session_dir = session_dir

        super().__init__(
            config=config,
            workspace_uuid=workspace_uuid,
            cwd=self._cwd_init,
            session_dir=init_session_dir,
            stop_check=stop_check,
            max_iterations=self.role_cfg.max_iterations,
        )

        self.model = config.model_for_role(role)

        # 统一循环编排层（Step 4）：交互/自主共用同一骨架，参数实时读 runner/role_cfg
        self.loop = Loop(
            self,
            LoopPolicy(
                runner=self,
                mode=self._mode,
                on_max_iterations="soft" if self._mode == "interactive" else "hard",
            ),
        )

        # Create LLM client（worker/lite 用角色模型，未配置继承 master model）
        self.client = create_llm_client(self.model)
        if temperature is not None:
            self.client.temperature = temperature
        # 角色级 max_tokens 上限：min(角色配置, 模型上限)，避免超过模型实际能力
        # （isinstance 守卫兼容测试中的 MagicMock 模型）
        role_max_tokens = self.role_cfg.max_tokens
        if role_max_tokens and isinstance(self.model.max_tokens, int):
            self.client.max_tokens = min(role_max_tokens, self.model.max_tokens)

        if self._mode == "interactive":
            # 高风险命令审批存储：会话级内存 + workspace 持久化规则（启动回灌），根/子代理共享
            from core.config import get_workspace_data_dir
            self.approval_store = ApprovalStore(
                rules_path=get_workspace_data_dir(self.workspace_uuid) / "approvals.json"
            )
            # IMPORTANT: Share messages list with session (not copy!)
            self.messages = self.session.messages
            self._usage = self.session.get_usage()
            # default_sink 由 web 层在创建 runner 时设置（工具实时输出 + 事件总线广播），
            # 用于后台 session 完成后自动恢复循环。sink 为本次运行的活动 sink。
            self.default_sink = OutputSink()
            self.sink = self.default_sink
        else:
            self.approval_store = approval_store
            self.max_consecutive_failures = (
                max_consecutive_failures
                if max_consecutive_failures is not None
                else self.role_cfg.max_consecutive_failures
            )
            self._budget_warn_triggered = False
            self._budget_final_triggered = False
            self._started_at: datetime | None = None

        self._streaming = self.role_cfg.streaming
        self._rebuild_tools()
        # prompt section 缓存：context 块等动态内容 session 内只算一次
        self._prompt_section_cache: dict[str, str] = {}
        # autonomous 用缓存的 system prompt（interactive 每轮在 run 入口重建）
        self._system_prompt = self._build_system_prompt()

    # ─── mode 专属初始化 ────────────────────────────────────────────

    def _init_interactive(self, workspace_uuid: str) -> None:
        """interactive（master）：会话持久化 + SessionStore。"""
        from core.config import get_workspace_data_dir
        self.sessions_dir = get_workspace_data_dir(workspace_uuid) / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        self.session = SessionStore("", self.sessions_dir)
        self.current_session_id: str = ""

        # Load or create session
        sessions = SessionStore.list_sessions(self.sessions_dir)
        if sessions:
            latest = max(sessions, key=lambda s: s.get("updated_at", ""))
            sid = latest["session_id"]
            loaded = SessionStore.load_session(sid, self.sessions_dir)
            if loaded:
                self.session = loaded
                self.current_session_id = sid
            else:
                self._create_default_session()
        else:
            self._create_default_session()

        self._session_id = self.current_session_id

        # Register master with message_bus so sub-agents can send messages to it
        try:
            from core.agent_mailbox import get_agent_mailbox
            mbus = get_agent_mailbox()
            mbus.register_agent(self.current_session_id, self.current_session_id)
        except Exception as e:
            logger.warning(f"Failed to register master with AgentMailbox: {e}")

    def _init_autonomous(
        self,
        task: str,
        plan: list[str] | None,
        exec_id: str,
        session_dir: Path | None,
        stop_check: Callable[[], bool] | None,
    ) -> None:
        """autonomous（worker/lite）：task/plan/exec_id 独立执行。"""
        self.task = task
        self.plan = plan
        self._exec_id = exec_id
        self._session_id = exec_id
        # 创建 session 引用，供工具获取 session_id 和 session_dir
        self._session_ref = _SessionRef(exec_id, session_dir)
        logger.debug(f"[SessionRunner:{self.role}] Session ID set to: {exec_id}")

    def _create_default_session(self) -> None:
        """Create a new default session."""
        session = SessionStore.create_new_session(self.sessions_dir, "Default")
        self.session = session
        self.current_session_id = session.session_id

    def _rebuild_tools(self) -> None:
        """Create tool instances from role whitelist and wire callbacks."""
        sm = self.session if self._mode == "interactive" else self._session_ref
        self.tools = create_tools(
            self.role_cfg,
            cwd=self.cwd,
            workspace_uuid=self.workspace_uuid,
            session=sm,
            config=self.config,
            approval_store=self.approval_store,
        )

        # MCP 动态工具（默认 deferred，tool_search 按需激活）：
        # 仅当角色开启 mcp 且配置了服务器时注入。provider 内部对配置签名
        # 做 diff，签名未变化不重连，因此此处每次 rebuild 开销很小。
        from core.tools.mcp import MCPToolWrapper
        if getattr(self.role_cfg, "mcp", False) and self.config.mcp_servers:
            from core.tools.mcp import get_provider
            provider = get_provider()
            provider.ensure_connected(self.config.mcp_servers)
            self.tools.extend(provider.get_wrappers())

        # Split into active (schema sent to LLM) vs deferred (schema hidden)
        self._deferred_names: set[str] = set(self.role_cfg.deferred_tools) | {
            t.name for t in self.tools if isinstance(t, MCPToolWrapper)
        }
        self._deferred_tools = [t for t in self.tools if t.name in self._deferred_names]
        self._active_tools = [t for t in self.tools if t.name not in self._deferred_names]
        self.tool_schemas = [t.to_schema() for t in self._active_tools]

        # 校验 deferred_tools 白名单：deferred_tools 按「工具 name」匹配（非注册键），
        # 未匹配到任何已加载工具的条目会静默失效，故显式告警。
        # 注意：role JSON 的 tools 用注册键（如 "todo"），deferred_tools 用工具名（如 "todo_write"）。
        matched_deferred = {t.name for t in self._deferred_tools}
        for name in self.role_cfg.deferred_tools:
            if name not in matched_deferred:
                logger.warning(
                    f"[SessionRunner:{self.role}] deferred_tools 中的 {name!r} 未匹配到任何已加载工具"
                    f"（按工具 name 匹配；注册键与工具名可能不同，如 'todo' vs 'todo_write'）"
                )

        # Wire tool_search: inject deferred list + activation callback
        ts = get_tool_by_name(self.tools, "tool_search")
        if ts:
            ts.deferred_tools = self._deferred_tools
            ts.on_load = self._activate_tools

        # Wire session tool: set delegation depth for all modes
        session_tool = get_tool_by_name(self.tools, "session")
        if session_tool:
            session_tool.delegation_depth = self.delegation_depth

        if self._mode == "interactive":
            # Wire session callbacks
            if session_tool:
                session_tool.stop_check = lambda: self._stopped
                session_tool.on_session_start = (
                    lambda exec_id, task_summary: self.sink.on_session_start(exec_id, task_summary)
                )
                session_tool.on_session_complete = (
                    lambda exec_id: self.sink.on_session_complete(exec_id)
                )
                # 后台 session 完成后自动恢复 master 循环
                session_tool.on_background_complete = self._handle_background_session_complete

    def _activate_tools(self, names: list[str]) -> None:
        """Move named tools from deferred to active and rebuild tool_schemas."""
        newly = [n for n in names if n in self._deferred_names]
        if not newly:
            return
        self._deferred_names -= set(newly)
        self._deferred_tools = [t for t in self._deferred_tools if t.name in self._deferred_names]
        self._active_tools = [t for t in self.tools if t.name not in self._deferred_names]
        self.tool_schemas = [t.to_schema() for t in self._active_tools]
        # Update tool_search's deferred list
        ts = get_tool_by_name(self.tools, "tool_search")
        if ts:
            ts.deferred_tools = self._deferred_tools

    def _execute_tool(self, name: str, input_data: dict, tool_use_id: str) -> dict:
        """Override: auto-activate deferred tools if called directly."""
        if name in getattr(self, "_deferred_names", set()):
            self._activate_tools([name])
        return super()._execute_tool(name, input_data, tool_use_id)

    # ─── system prompt / 上下文 ─────────────────────────────────────

    def _build_system_prompt(self) -> list[str]:
        """按角色 JSON 的 blocks 拼装 system prompt，返回字符串列表。

        列表含 DYNAMIC_BOUNDARY 分隔标记，adapter 层按此分割设置 cache_control。
        """
        return build_system_prompt(self)

    def _get_messages_with_header(self) -> list[dict]:
        """BaseSessionRunner 版 + 注入型 user 层（claude_md）+ 防连续合并。

        注入层每次从磁盘动态生成（claude_md），不持久化到 self.messages（会话文件保持干净）。
        context 已迁移至 system prompt 的动态区（session 内缓存），不再在此注入。
        合并后连续 user 消息自动合成一条，保证角色交替（OpenAI/Bedrock 约束）。
        """
        messages = self.context.get_messages_with_header()

        inject: list[dict] = []
        for layer in self.role_cfg.user_layers:
            if not layer.get("enabled", True):
                continue
            gen = USER_LAYER_GENERATORS.get(layer.get("type"))
            if gen is None:
                continue  # task/runtime 层运行时写入历史，不在此注入
            msg = gen(self)
            if msg:
                inject.append(msg)

        return assemble_context(messages, inject)

    # ─── interactive（master）：会话管理 ────────────────────────────

    def _sync_to_session(self) -> None:
        """Sync metadata and usage to session (delegated to ConversationStore).

        Note: messages are shared (same reference), no need to sync them.
        """
        self.context.sync_to_session()

    def reload_config(self) -> None:
        """Reload config from disk and recreate LLM client."""
        from core.config import load_config
        try:
            new_config = load_config()
            self.role_cfg = load_role(self.role, new_config)
            self.max_iterations = self.role_cfg.max_iterations
            # 先创建新客户端，成功后再替换并关闭旧的；
            # 否则创建失败后 self.client 指向已关闭的客户端，后续调用全部失败
            new_model = new_config.model_for_role(self.role)
            new_client = create_llm_client(new_model)
            old_client = self.client
            self.client = new_client
            self.model = new_model
            self.config = new_config
            old_client.close()
            self._rebuild_tools()
        except Exception as e:
            logger.warning(f"[SessionRunner:{self.role}] 重新加载配置失败: {e}")

    def run(self, *args, **kwargs):
        """统一入口：按角色 mode 分派。

        interactive → run(user_input, on_text=..., ...)（Web 聊天入口）
        autonomous → run() → dict（pinned 任务 → 循环 → 检查 → 兜底总结）
        """
        if self._mode == "interactive":
            return self._run_interactive(*args, **kwargs)
        return self.loop.run_autonomous()

    def _run_interactive(
        self,
        user_input: str | list[dict],
        sink: OutputSink | None = None,
        streaming: bool = True,
    ) -> None:
        """Run one turn of the interactive session loop."""
        self._stopped = False
        self._running = True
        self._turn_iterations = 0  # 新用户轮次清零，resume 路径保留累计
        self._streaming = streaming
        self.sink = layer_sink(self.default_sink, sink)

        try:
            # Add user message（loop.run_interactive 在回合前做 checkpoint save）
            self.add_message("user", user_input)

            self.loop.run_interactive()
        finally:
            self._running = False
            # 异常兜底 checkpoint：脏数据落盘（正常路径已存过，此处短路）
            if getattr(self, "session", None) is not None:
                self._sync_to_session()
                self.session.save()

    def _handle_approval_required(self, approval: dict) -> None:
        """合成 ask_user 卡询问用户是否批准高风险命令，随后暂停循环等待回答。

        在批处理所有工具结果之后调用，保证消息配对正确：
        [assistant tool_use...] → [tool_result...] → [assistant ask_user tool_use] → [user ask_user 占位]
        """
        self.approval_store.set_pending(approval)

        ask_id = generate_short_id()
        ask_input = {
            "questions": [
                {
                    "question": build_approval_question(approval),
                    "header": "命令批准",
                    "options": [
                        {"label": APPROVE_LABEL, "description": "批准后本会话内执行相同命令（含委派给子代理）不再询问。"},
                        {"label": REMEMBER_LABEL, "description": "批准并写入此工作区，重启后仍放行相同命令。"},
                        {"label": REJECT_LABEL, "description": "拒绝执行该命令。"},
                    ],
                }
            ]
        }
        # 手动补 assistant tool_use 块，避免悬挂 tool_result（否则 API 400 或触发 _pad_dangling_tool_results）
        self.add_message("assistant", [{"type": "tool_use", "id": ask_id, "name": "ask_user", "input": ask_input}])
        self.add_message("user", [self._execute_tool("ask_user", ask_input, ask_id)])
        self._sync_to_session()
        self.session.save()

    def _resume_loop(self, sink: OutputSink | None = None) -> None:
        """共享恢复路径：重新进入 interactive 循环，不重置本轮迭代计数。"""
        self._stopped = False
        self._running = True
        self.sink = layer_sink(self.default_sink, sink)

        try:
            # loop.run_interactive 在回合前做 checkpoint save
            self.loop.run_interactive()
        finally:
            self._running = False
            # 异常兜底 checkpoint：脏数据落盘（正常路径已存过，此处短路）
            self._sync_to_session()
            self.session.save()

    def resume_after_ask_user(self, sink: OutputSink | None = None) -> None:
        """Resume session loop after ask_user tool result has been injected."""
        self._resume_loop(sink=sink)

    def _handle_background_session_complete(self, exec_id: str, status: str) -> None:
        """后台 session 完成后自动恢复 master 循环（如果 master 空闲）。

        由 SessionTool.on_background_complete 回调触发。
        检查 message_bus 是否有未读通知，如果有且 master 空闲，则自动恢复循环。
        使用 default_sink（发布事件到全局事件总线）。
        """
        if self._mode != "interactive":
            return
        if self.is_running():
            # master 正在运行，不需要恢复（循环会在下次迭代时拾取通知）
            return
        # 检查 message_bus 是否有未读通知
        try:
            from core.agent_mailbox import get_agent_mailbox
            mbus = get_agent_mailbox()
            if not mbus.has_unread(self._session_id):
                return  # 没有未读通知，不需要恢复
        except Exception:
            return
        # 使用 default_sink（发布到全局事件总线）
        logger.info(f"[SessionRunner:{self.role}] 后台 session {exec_id} 完成，自动恢复 master 循环")
        self._resume_loop(sink=self.default_sink)

    def attach_session(self, session_store: SessionStore) -> None:
        """把 runner 附着到一个 SessionStore（切换会话 / 新建会话共用）。

        同步 session/context 引用、messages、工具 session 引用、usage 与缓存基线，
        避免切换与新建两条路径各自重复实现（曾各写一份、易漏）。
        """
        self.session = session_store
        self.context.set_session(session_store)  # 同步 context 引用，保持一致
        self.current_session_id = session_store.session_id
        self._session_id = session_store.session_id
        self.session_dir = session_store.session_dir
        self.messages = session_store.messages
        self._usage = session_store.get_usage()
        for tool in self.tools:
            tool.session = session_store
        # 切换会话后清除 prompt section 缓存（不同会话可能有不同上下文）
        self._prompt_section_cache.clear()
        # 重置缓存基线（不同会话的 cache_read 不可比），但保留累计统计
        self.cache_state.reset_after_compact()

    def switch_session(self, session_id: str) -> None:
        """Switch to a different (existing) session."""
        loaded = SessionStore.load_session(session_id, self.sessions_dir)
        if loaded:
            self.attach_session(loaded)
            logger.info(f"Switched to session: {session_id}")
        else:
            logger.warning(f"Session not found: {session_id}")

    def reset(self) -> None:
        """Clear conversation history."""
        self.messages.clear()
        self.session.clear()
        # 清除 prompt section 缓存，下次构建时重新计算
        self._prompt_section_cache.clear()
        # 重置缓存状态追踪（新会话无历史基线）
        self.cache_state.reset()

    def get_usage(self) -> dict[str, int]:
        """Return usage statistics synced with session."""
        self._sync_to_session()
        return self.session.get_usage()

    def compact(self) -> tuple[int, int]:
        """Manually compress conversation history."""
        result = self._perform_full_compact(3)
        # 压缩后清除 prompt section 缓存
        self._prompt_section_cache.clear()
        # 手动 compact 等同于 L2，重置缓存基线后标记 L2 供下次失效诊断
        self.cache_state.reset_after_compact()
        self.cache_state.on_compression(2)
        return result

    def cleanup(self) -> None:
        """Clean up resources before exit."""
        if self._mode == "interactive":
            self._sync_to_session()
            self.session.save()
        super().close()

    # ─── autonomous（worker/lite）：自主执行 ────────────────────────

    def _build_task_message(self) -> str:
        """Build task+plan as first user message (pinned, survives compression)."""
        lines = ["## Assigned Task", ""]

        lines.append("### Objective")
        lines.append("")
        lines.append(self.task)
        lines.append("")

        if self.plan:
            lines.append("### Execution Plan")
            lines.append("")
            for i, step in enumerate(self.plan, 1):
                lines.append(f"{i}. {step}")
            lines.append("")
            lines.append("Execute these steps in order. Report progress as you complete each step.")
            lines.append("")

        lines.append(
            f"Iteration budget: at most {self.max_iterations} tool-call rounds. "
            "Budget notices may appear near the limit — comply immediately."
        )
        lines.append("")

        # 下放主代理会话级批准的命令（共享同一 ApprovalStore，子代理可直接执行）
        approved_section = build_approved_commands_section(self.approval_store)
        if approved_section:
            lines.append(approved_section)

        return "\n".join(lines)

    def _inject_budget_notice(self, i: int) -> None:
        """迭代额度临近耗尽时注入预警消息（final 优先，各只触发一次）。

        用 list content 注入：_find_split_by_user_messages 只统计 string user
        消息（KEEP_USER_MESSAGES=3），string 消息会把计数推过阈值，导致 full
        compact 挤掉 pinned 任务消息。
        """
        if not self._budget_final_triggered and i >= int(self.max_iterations * BUDGET_FINAL_RATIO):
            self._budget_final_triggered = True
            self.add_message(
                "user",
                [{"type": "text", "text": BUDGET_FINAL_PROMPT.format(used=i, total=self.max_iterations)}],
                meta={"budget": "final"},
            )
        elif not self._budget_warn_triggered and i >= int(self.max_iterations * BUDGET_WARN_RATIO):
            self._budget_warn_triggered = True
            self.add_message(
                "user",
                [{"type": "text", "text": BUDGET_WARN_PROMPT.format(used=i, total=self.max_iterations)}],
                meta={"budget": "warn"},
            )

    @staticmethod
    def _downgrade_approval_result(result: dict) -> None:
        """autonomous 无 ask_user：把"需用户批准"的结果降级为普通错误，不挂起不询问。"""
        if META_KEY in result.get("_meta", {}):
            result["is_error"] = True
            result["content"] = "该命令需要用户（主代理会话）批准，子代理无法执行，请换用非拦截命令或告知主代理。"
            result["_meta"].pop(META_KEY, None)
            result["_meta"].pop("completed", None)

    def _wrapup_timeout_summary(self) -> str:
        """额度耗尽时兜底生成一次执行总结。

        直接调 client.chat 且不传 tools，杜绝兜底调用再次触发工具循环。
        """
        self._pad_dangling_tool_results()
        self.add_message("user", [{"type": "text", "text": TIMEOUT_WRAPUP_PROMPT}], meta={"budget": "wrapup"})

        # 消息预处理与 _call_llm_non_streaming 一致（此处已 pad，复用共享管道）
        message_objects = self._prepare_messages_for_llm(pad_dangling=False)

        response = self.client.chat(
            messages=message_objects,
            system=self._system_prompt,
            session_id=self._session_id,
        )
        if response.usage:
            self._update_usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                api_calls=1,
                cache_read_tokens=response.usage.cache_read_tokens,
                cache_creation_tokens=response.usage.cache_write_tokens,
            )
        text = response.get_text().strip()
        self.add_message("assistant", text)
        return text

    def _elapsed_seconds(self) -> float:
        """Calculate elapsed seconds since start."""
        if self._started_at is None:
            return 0.0
        return (datetime.now() - self._started_at).total_seconds()

    def _save_progress(self, iterations: int, status: str = "running",
                       current_tool: str = "", tool_calls: int | None = None) -> None:
        """Save execution progress in real-time.

        Writes to {exec_dir}/index.json in the format SessionStore.agent_logs.load_agent_log() expects.
        This ensures the file always has exec_id and task, even before the final save.

        Args:
            iterations: LLM 调用轮次数
            status: 当前状态（running/check/completed 等）
            current_tool: 正在执行的工具名
            tool_calls: 本批新增的工具调用数（累加到 _tool_call_count）
        """
        # interactive 模式可能没有 _exec_id（只有 autonomous/worker 才有）
        exec_id = getattr(self, '_exec_id', None)
        if not self.session_dir or not exec_id:
            return

        # 累加工具调用计数
        if tool_calls is not None and tool_calls > 0:
            self._tool_call_count += tool_calls

        metadata = {
            "parent_session_id": "",
            "session_id": self._session_id,
            "started_at": self._started_at.strftime("%Y-%m-%d %H:%M:%S") if self._started_at else "",
            "ended_at": None,
            "duration_seconds": self._elapsed_seconds(),
            "status": status,
            "iterations": iterations,
            "max_iterations": self.max_iterations,
            "message_count": len(self.messages),
            "current_tool": current_tool,
            "tool_call_count": self._tool_call_count,
        }

        log_data = build_exec_log_data(
            exec_id=exec_id,
            session_id=self._session_id,
            task=self.task,
            metadata=metadata,
            summary="",
            messages=self.messages,
        )

        try:
            log_file = self.session_dir / "index.json"
            atomic_write_json(log_file, log_data)
        except Exception as e:
            logger.warning(f"[SessionRunner:{self.role}] Failed to save progress: {e}")

        # 发布进度事件供前端实时更新 header（节流：每 0.5s 最多一次）
        if self._event_publisher:
            import time
            now = time.monotonic()
            if now - self._last_progress_event_time >= 0.5:
                self._last_progress_event_time = now
                try:
                    self._event_publisher("session_progress",
                                          exec_id=exec_id,
                                          iterations=iterations,
                                          message_count=len(self.messages),
                                          tool_call_count=self._tool_call_count,
                                          current_tool=current_tool,
                                          status=status)
                except Exception:
                    pass

    def _finalize(self, status: str, summary: str, iterations: int) -> None:
        """Finalize execution: save final log."""
        ended_at = datetime.now()
        duration_seconds = (ended_at - self._started_at).total_seconds() if self._started_at else 0.0

        if self.session_dir:
            metadata = {
                "parent_session_id": "",
                "session_id": self._session_id,
                "started_at": self._started_at.strftime("%Y-%m-%d %H:%M:%S") if self._started_at else "",
                "ended_at": ended_at.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_seconds": duration_seconds,
                "status": status,
                "iterations": iterations,
                "max_iterations": self.max_iterations,
                "message_count": len(self.messages),
                "tool_call_count": self._tool_call_count,
                "summary": summary,
            }

            log_data = build_exec_log_data(
                exec_id=self._exec_id,
                session_id=self._session_id,
                task=self.task,
                metadata=metadata,
                summary=summary,
                messages=self.messages,
            )

            try:
                self.session_dir.mkdir(parents=True, exist_ok=True)
                log_file = self.session_dir / "index.json"
                atomic_write_json(log_file, log_data)
            except Exception as e:
                logger.warning(f"[SessionRunner:{self.role}] Failed to finalize: {e}")
