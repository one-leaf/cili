"""Loop - 统一循环编排层（Step 4）。

交互（master）与自主（worker/lite）两套循环统一为一个骨架：
LoopPolicy 携带行为参数，PhaseMachine 管理 autonomous 的执行/检查阶段转移。
单回合原子单位是 Runner.run_round（压缩→LLM 调用），本层只做编排：
迭代计数、stop、预算注入、检查阶段、审批卡合成、wait_for_external、
落盘触发、finalize。

职责边界：
- Context：消息状态
- Runner：单回合执行（run_round / execute_tool）
- Loop：while 编排与模式差异（差异点①无工具调用、②工具结果 handler）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core.llm import format_llm_error
from core.tools.approval import META_KEY

logger = logging.getLogger(__name__)

# ─── autonomous 运行时常量（原 core/agent.py 模块级，迁移至此避免循环导入）───

BUDGET_WARN_RATIO = 0.8  # 迭代额度使用率达到 80% 时注入一次预警提示
BUDGET_FINAL_RATIO = 0.95  # 达到 95% 时跳过检查阶段，直接兜底总结交付

BUDGET_WARN_PROMPT = (
    "## 额度预警\n\n"
    "迭代额度已使用 {used}/{total}。请评估当前进度：\n"
    "- 不要再扩展新的工作面\n"
    "- 已开始的工作尽快完成，准备收尾总结"
)

BUDGET_FINAL_PROMPT = (
    "## 额度即将耗尽\n\n"
    "迭代额度即将用尽（{used}/{total}）。**立即停止发起新的工具调用**，"
    "直接输出最终总结报告，必须包含：\n"
    "1. 已完成的工作与产出位置\n"
    "2. 未完成/未验证的部分及原因\n"
    "3. 供父代理继续的后续建议"
)

TIMEOUT_WRAPUP_PROMPT = (
    "迭代额度已耗尽，任务循环被强制终止。请基于上方全部历史，"
    "输出最终执行总结报告：\n"
    "1. 已完成的工作与产出位置\n"
    "2. 未完成/未验证的部分及原因\n"
    "3. 后续建议\n\n"
    "不要调用任何工具。"
)

# Check phase prompt (injected after main execution completes)
CHECK_PROMPT = (
    "## 检查阶段\n\n"
    "执行阶段已完成。现在进入 **检查** 环节，逐项验证任务是否正确完成，"
    "不允许只凭之前的工具结果印象下结论：\n\n"
    "1. 重新阅读上方的「任务目标」和「执行计划」，提炼可验证的验收标准\n"
    "2. 逐项核对执行结果：每一项都要**用工具取证**（运行测试、读取实际文件、"
    "检查输出与配置），不要仅凭记忆\n"
    "3. 如发现遗漏或错误，**立即修复**，并在修复后重新验证该项\n"
    "4. 全部核对完成后，输出最终总结报告，**必须包含**：\n"
    "   - 已逐项验证的内容（附取证来源）\n"
    "   - 未能验证或未验证的项及原因\n"
    "   - 检查过程中修复的问题\n"
    "5. 总结前回顾本次任务：若发现值得跨会话复用的**非显然知识**"
    "（操作经验、关键决策、踩坑教训），用 `memory(action='store')` 存入"
    "（type 选 skill 或 fact）\n"
)


class LoopPolicy:
    """循环行为参数：结构参数冻结，运行参数实时读 agent。

    结构参数（mode / on_max_iterations）由 Agent 构造时固定；
    运行参数（迭代/检查/预算/流式）读 agent 与 role_cfg 的实时值——
    运行时改 agent.max_iterations / role_cfg.check_iterations 立即生效
    （测试与 config 热重载均依赖此行为）。
    """

    def __init__(
        self,
        agent: Any,
        mode: str = "interactive",
        on_max_iterations: str = "soft",
    ):
        self.agent = agent
        self.mode = mode
        self.on_max_iterations = on_max_iterations  # "soft" 提示 / "hard" 兜底总结

    @property
    def max_iterations(self) -> int:
        return self.agent.max_iterations

    @property
    def check_phase(self) -> bool:
        return self.agent.role_cfg.check_phase

    @property
    def check_iterations(self) -> int | None:
        return self.agent.role_cfg.check_iterations

    @property
    def budget_notice(self) -> bool:
        return self.agent.role_cfg.budget_notice

    @property
    def max_consecutive_failures(self) -> int | None:
        return getattr(self.agent, "max_consecutive_failures", None)

    @property
    def streaming(self) -> bool:
        if self.mode == "interactive":
            return bool(getattr(self.agent, "_streaming", True))
        return self.agent.role_cfg.streaming


class NoToolCallOutcome:
    """无工具调用轮的后续动作决定（差异点①）。"""

    __slots__ = ("done", "action")

    def __init__(self, done: bool, action: str):
        self.done = done
        self.action = action  # complete/deliver/budget_wrapup/enter_check/check_complete


class PhaseMachine:
    """autonomous 执行/检查阶段状态机；interactive 单相（无阶段转移）。"""

    def __init__(self, policy: LoopPolicy):
        self.policy = policy
        self.in_check_phase = False
        self.check_iters = 0
        self.max_check_iterations: int | None = None

    def decide_no_tool_calls(self, budget_final_triggered: bool) -> NoToolCallOutcome:
        """无工具调用轮：决定是交付/进入检查还是继续等待。"""
        if self.policy.mode != "autonomous":
            return NoToolCallOutcome(done=True, action="complete")
        if not self.in_check_phase:
            if self.policy.budget_notice and budget_final_triggered:
                # 额度兜底下跳过检查阶段，直接交付总结
                return NoToolCallOutcome(done=True, action="budget_wrapup")
            if not self.policy.check_phase:
                return NoToolCallOutcome(done=True, action="deliver")
            return NoToolCallOutcome(done=False, action="enter_check")
        return NoToolCallOutcome(done=True, action="check_complete")

    def enter_check(self, current_iteration: int) -> None:
        """主阶段结束 → 进入检查阶段。

        current_iteration 为进入检查那一轮的 0 基迭代号；检查轮次上限
        以 max(已耗迭代, 配置) 起算，保证已耗用额度不会压缩检查轮次。
        None = 不设上限，仅由总迭代额度兜底。
        """
        self.in_check_phase = True
        self.check_iters = 0
        ci = self.policy.check_iterations
        self.max_check_iterations = max(current_iteration, ci) if ci is not None else None

    def on_tool_round(self) -> bool:
        """检查阶段工具调用轮：计数；返回 True 表示超上限应强制收尾。"""
        if not self.in_check_phase:
            return False
        self.check_iters += 1
        return self.max_check_iterations is not None and self.check_iters > self.max_check_iterations

    def mark_check_complete(self) -> None:
        """检查阶段文本收尾轮：计数（check_iterations 含收尾轮）。"""
        self.check_iters += 1


class Loop:
    """统一循环编排：交互与自主共用同一 while 骨架。"""

    def __init__(self, agent: Any, policy: LoopPolicy):
        self.agent = agent
        self.policy = policy
        self.phase_machine = PhaseMachine(policy)

    # ─── 入口 ───────────────────────────────────────────────────────

    def run_interactive(self) -> None:
        """交互回合循环主体（run()/resume_* 薄入口调用）。"""
        agent = self.agent
        agent._sync_to_session_manager()
        agent.session_manager.save()
        self._run_loop(autonomous=False)

    def run_autonomous(self) -> dict[str, Any]:
        """自主执行循环，返回结构化结果。"""
        agent = self.agent
        agent._started_at = datetime.now()
        agent._stopped = False
        agent._running = True
        agent._budget_warn_triggered = False
        agent._budget_final_triggered = False
        # 构建 pinned 任务消息（任务+计划，压缩免疫）
        agent.add_message("user", agent._build_task_message(), meta={"pinned": True})
        try:
            return self._run_loop(autonomous=True) or {}
        finally:
            agent._running = False

    # ─── 统一骨架 ───────────────────────────────────────────────────

    def _run_loop(self, autonomous: bool) -> dict[str, Any] | None:
        """两套循环统一骨架：interactive 返回 None，autonomous 返回结果 dict。

        各 break 出口均用 return，因此 while 正常结束（条件为假）等价于
        迭代额度耗尽 → on_max_iterations（soft 提示 / hard 兜底总结）。
        """
        agent = self.agent
        policy = self.policy
        pm = self.phase_machine

        # 迭代计数：interactive 用 agent._turn_iterations（resume 累计基准），
        # autonomous 用局部 n 从 0 起（每执行独立）
        n = 0 if autonomous else agent._turn_iterations

        # 批处理跨轮状态（熔断计数跨轮累计；审批/等待每轮重置）
        state: dict[str, Any] = {
            "consecutive_failures": 0,
            "approval": None,
            "wait_for_external": False,
            "external_already": False,
        }

        max_iterations = policy.max_iterations

        while n < max_iterations:
            # ── stop 检查（父代理停止或流式中断标记）──
            if agent._stopped or (autonomous and agent.stop_check and agent.stop_check()):
                if autonomous:
                    agent._finalize("stopped", "Stopped by user", n)
                    return self._autonomous_result("stopped", "Stopped by user", n)
                logger.info(f"[Agent:{agent.role}] 已停止")
                agent._sync_to_session_manager()
                agent.session_manager.save()
                if agent._on_text:
                    agent._on_text("\n\n[已停止]")
                return None

            # ── 额度预警（autonomous，stop 已排除）──
            if autonomous and policy.budget_notice:
                agent._inject_budget_notice(n)

            n += 1
            if not autonomous:
                agent._turn_iterations = n  # 同步计数，resume 保留累计

            # ── LLM 调用 ──
            if autonomous:
                try:
                    response = agent._call_llm(
                        streaming=policy.streaming,
                        system_prompt=agent._system_prompt,
                    )
                except Exception as e:
                    summary = format_llm_error(e, agent.client.base_url if agent.client else "")
                    task_brief = agent.task[:50].replace("\n", " ")
                    logger.error(
                        f"[Agent:{agent.role}] LLM 错误 (iter={n - 1}, exec={agent._exec_id}, "
                        f"task='{task_brief}'): {summary}"
                    )
                    agent._finalize("error", summary, n - 1)
                    return self._autonomous_result("error", summary, n - 1)
                if agent._stopped:
                    agent._finalize("stopped", "Stopped by user", n - 1)
                    return self._autonomous_result("stopped", "Stopped by user", n - 1)
            else:
                system_prompt = agent._build_system_prompt()
                response = agent._call_llm(
                    streaming=policy.streaming,
                    system_prompt=system_prompt,
                )
                if agent._stopped:
                    logger.info(f"[Agent:{agent.role}] 已停止")
                    agent._sync_to_session_manager()
                    agent.session_manager.save()
                    return None

            tool_calls = response.get_tool_calls()

            if not tool_calls:
                # 差异点①：无工具调用 → 交付/转检查（autonomous）或回合完成（interactive）
                if not autonomous:
                    agent.add_message("assistant", response.content_as_dicts())
                    agent._sync_to_session_manager()
                    agent.session_manager.save()
                    return None

                outcome = pm.decide_no_tool_calls(agent._budget_final_triggered)
                if outcome.done:
                    if outcome.action == "budget_wrapup":
                        summary = response.get_text() or "预算耗尽，模型未输出总结"
                        agent.add_message("assistant", response.content_as_dicts())
                        agent._finalize("completed", summary, n)
                        return self._autonomous_result("completed", summary, n, budget_wrapup=True)
                    if outcome.action == "deliver":
                        summary = response.get_text()
                        agent.add_message("assistant", response.content_as_dicts())
                        agent._finalize("completed", summary, n)
                        return self._autonomous_result("completed", summary, n)
                    # check_complete：检查阶段文本收尾 → 任务真正完成
                    pm.mark_check_complete()
                    summary = response.get_text()
                    agent.add_message("assistant", response.content_as_dicts())
                    agent._finalize("completed", summary, n)
                    return self._autonomous_result(
                        "completed", summary, n, check_iterations=pm.check_iters
                    )

                # enter_check：主阶段结束 → 注入检查提示转检查
                summary = response.get_text()
                agent.add_message("assistant", response.content_as_dicts())
                agent.add_message("user", CHECK_PROMPT, meta={"pinned": True})
                pm.enter_check(n - 1)
                logger.debug(
                    f"[Agent:{agent.role}] 进入检查阶段 (iter={n - 1}, "
                    f"max_check={pm.max_check_iterations}, exec={agent._exec_id})"
                )
                continue

            # ── 检查阶段轮次上限（autonomous 工具调用轮）──
            if autonomous and pm.on_tool_round():
                summary = response.get_text() or "检查阶段超出最大迭代次数"
                agent.add_message("assistant", response.content_as_dicts())
                agent._finalize("completed", summary, n)
                logger.warning(
                    f"[Agent:{agent.role}] 检查阶段超出迭代上限 "
                    f"(iter={n - 1}, max={pm.max_check_iterations})"
                )
                return self._autonomous_result(
                    "completed", summary, n, check_iterations=pm.check_iters
                )

            # ── 添加 assistant 消息（工具调用轮）──
            agent.add_message("assistant", response.content_as_dicts())

            # ── 工具批执行（差异点②：结果 handler 模式专属）──
            if autonomous:
                phase = "check" if pm.in_check_phase else "running"
                agent._save_progress(n, status=phase)
                if self._autonomous_tool_batch(tool_calls, n, phase, state):
                    # 连续失败熔断
                    cap = policy.max_consecutive_failures
                    summary = f"Exceeded max consecutive failures ({cap})"
                    agent._finalize("failed", summary, n)
                    return self._autonomous_result("failed", summary, n)
            else:
                if self._interactive_tool_batch(tool_calls, state):
                    # 等待外部输入（ask_user/agent 占位或审批卡）
                    logger.info(f"[Agent:{agent.role}] Waiting for external input (user or agent)")
                    agent._sync_to_session_manager()
                    agent.session_manager.save()
                    return None
                if agent._stopped:
                    logger.info(f"[Agent:{agent.role}] 已停止")
                    # 中途停止可能留下未回应的 tool_use，补占位避免下次调用 400
                    agent._pad_dangling_tool_results()
                    agent._sync_to_session_manager()
                    agent.session_manager.save()
                    return None

        # 迭代额度耗尽 → on_max_iterations
        if not autonomous:
            logger.warning(f"[Agent:{agent.role}] 达到最大调用次数 ({max_iterations})")
            agent._sync_to_session_manager()
            agent.session_manager.save()
            if agent._on_text:
                agent._on_text(
                    f"\n\n[已达到最大工具调用次数限制 ({max_iterations})，请继续提问以继续对话]"
                )
            return None

        status = "timeout"
        summary = f"Exceeded max iterations ({max_iterations})"
        wrapped_up = False
        if not (agent.stop_check and agent.stop_check()):
            try:
                wrapup = agent._wrapup_timeout_summary()
                if wrapup:
                    summary = wrapup
                    wrapped_up = True
            except Exception as e:
                logger.warning(f"[Agent:{agent.role}] 兜底总结失败: {e}")
        agent._finalize(status, summary, max_iterations)
        result = self._autonomous_result(status, summary, max_iterations)
        if wrapped_up:
            result["wrapped_up"] = True
        return result

    # ─── 工具批执行（差异点②） ─────────────────────────────────────

    def _autonomous_tool_batch(
        self,
        tool_calls: list,
        n: int,
        phase: str,
        state: dict[str, Any],
    ) -> bool:
        """autonomous 工具批：降级审批 + 实时进度 + 连续失败熔断。

        返回 True 表示熔断触发（结果已组装），本批与整个执行应终止。
        """
        agent = self.agent
        policy = self.policy
        for tc in tool_calls:
            agent._save_progress(n, status=phase, current_tool=tc.name)
            input_data = tc.parse_arguments()
            result = agent._execute_tool(tc.name, input_data, tc.id)
            # autonomous 无 ask_user：把"需用户批准"的结果降级为普通错误，不挂起不询问
            agent._downgrade_approval_result(result)
            agent.add_message("user", [result])
            agent._save_progress(n, status=phase)

            if result.get("is_error"):
                state["consecutive_failures"] += 1
                if (
                    policy.max_consecutive_failures is not None
                    and state["consecutive_failures"] >= policy.max_consecutive_failures
                ):
                    return True
            else:
                state["consecutive_failures"] = 0
        return False

    def _interactive_tool_batch(self, tool_calls: list, state: dict[str, Any]) -> bool:
        """interactive 工具批：审批/占位标注 + 批后合成 ask_user 卡。

        返回 True 表示需等待外部输入（ask_user/agent 占位或审批卡）。
        """
        agent = self.agent
        for block in tool_calls:
            if agent._stopped:
                break
            input_data = block.parse_arguments()
            result = agent._execute_tool(block.name, input_data, block.id)
            placeholder = result.get("_meta", {}).get("completed") is False
            # 高风险命令需用户批准：降级为错误提示，统一在批处理完后合成 ask_user 卡
            if META_KEY in result.get("_meta", {}):
                if state["approval"] is None:
                    state["approval"] = result["_meta"][META_KEY]
                    result["is_error"] = True
                    result["content"] = "该命令需要用户批准，正在询问用户..."
                else:
                    # 同批多个需批准命令：只询问第一条，其余保持拒绝
                    result["is_error"] = True
                    result["content"] = "该命令需要用户批准，本批仅询问一条，请稍后重试。"
                result["_meta"].pop(META_KEY, None)
                result["_meta"].pop("completed", None)
            elif placeholder:
                # 模型自发的 ask_user/agent 占位：正常等待，不叠加审批卡
                state["wait_for_external"] = True
                state["external_already"] = True
            agent.add_message("user", [result])
            agent._sync_to_session_manager()

        # 合成 ask_user 卡询问用户是否批准（放在所有工具结果之后，保持消息配对正确；
        # 本批已有模型自发的占位时不合成，避免与 pending 单槽冲突）
        if state["approval"] and not state["external_already"] and not agent._stopped:
            agent._handle_approval_required(state["approval"])
            state["wait_for_external"] = True
        # 迭代边界批量落盘一次：本批工具结果已入内存，一次性追加 jsonl + fsync（非逐条）。
        # 已由 _handle_approval_required 的 save() 落盘时此处为幂等 no-op。
        agent.session_manager.flush()
        return state["wait_for_external"]

    # ─── 辅助 ───────────────────────────────────────────────────────

    def _autonomous_result(self, status: str, summary: str, iterations: int, **extra: Any) -> dict[str, Any]:
        """组装 autonomous 结果 dict（usage 恒取当前快照）。"""
        result = {
            "status": status,
            "summary": summary,
            "iterations": iterations,
            "usage": self.agent._usage,
        }
        result.update(extra)
        return result
