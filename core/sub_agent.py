"""SubAgent - autonomous agent loop for delegated tasks.

Provides SubAgent.run() for executing tasks inside a Python script
with its own tool set and message history.

Key design:
- Inherits from BaseAgent for unified execution loop
- Task/plan in pinned first user message (immune to compression)
- Independent message history
- Execution logged to {session_dir}/index.json
- Post-execution check phase for verification (目标→计划→执行→检查)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core.config import load_config
from core.llm import create_llm_client, format_llm_error
from core.fs_utils import atomic_write_json
from core.base_agent import BaseAgent
from core.prompts import build_sub_prompt
from core.tools.sub import create_sub_tools
from core.tools.shared.approval import META_KEY, build_approved_commands_section

logger = logging.getLogger(__name__)


_MAX_ITERATIONS = 200
_MIN_CHECK_ITERATIONS = 10

# 迭代额度预警阈值（占 max_iterations 的比例），各阶段只触发一次
_BUDGET_WARN_RATIO = 0.8
_BUDGET_FINAL_RATIO = 0.95

_BUDGET_WARN_PROMPT = (
    "## 额度预警\n\n"
    "迭代额度已使用 {used}/{total}。请评估当前进度：\n"
    "- 不要再扩展新的工作面\n"
    "- 已开始的工作尽快完成，准备收尾总结"
)

_BUDGET_FINAL_PROMPT = (
    "## 额度即将耗尽\n\n"
    "迭代额度即将用尽（{used}/{total}）。**立即停止发起新的工具调用**，"
    "直接输出最终总结报告，必须包含：\n"
    "1. 已完成的工作与产出位置\n"
    "2. 未完成/未验证的部分及原因\n"
    "3. 供父代理继续的后续建议"
)

_TIMEOUT_WRAPUP_PROMPT = (
    "迭代额度已耗尽，任务循环被强制终止。请基于上方全部历史，"
    "输出最终执行总结报告：\n"
    "1. 已完成的工作与产出位置\n"
    "2. 未完成/未验证的部分及原因\n"
    "3. 后续建议\n\n"
    "不要调用任何工具。"
)

# Check phase prompt (injected after main execution completes)
_CHECK_PROMPT = (
    "## 检查阶段\n\n"
    "执行阶段已完成。现在进入 **检查** 环节，请验证任务是否正确完成：\n\n"
    "1. 重新阅读上方的「任务目标」和「执行计划」\n"
    "2. 逐项检查执行结果，确认每项是否达标\n"
    "3. 如发现遗漏或错误，**立即修复**（可使用工具）\n"
    "4. 全部确认无误后，输出最终总结报告\n"
)


class _SessionIdRef:
    """简单的 session_id 引用，供工具获取 session 标识。

    SubAgent 使用 exec_id 作为 session 标识。
    """

    def __init__(self, session_id: str):
        self.session_id = session_id


class SubAgent(BaseAgent):
    """SubAgent for delegated task execution.

    - Full tool set (read/write/edit/bash/grep/find/python/browser/web_search/memory)
    - Nesting forbidden (subagent cannot delegate further)
    - Independent message history
    - Execution logged to separate directory
    - Post-execution check phase for verification (目标→计划→执行→检查)
    """

    def __init__(
        self,
        task: str,
        plan: list[str] | None = None,
        workspace_uuid: str = "",
        cwd: str = "",
        max_consecutive_failures: int = 5,
        session_dir: Path | None = None,
        stop_check: Callable[[], bool] | None = None,
        exec_id: str = "",
        temperature: float | None = None,
        approval_store=None,
    ):
        """Initialize SubAgent.

        Args:
            task: Task description
            plan: Optional execution plan (list of steps)
            workspace_uuid: Workspace UUID
            cwd: Working directory
            max_consecutive_failures: Maximum consecutive tool failures before abort
            session_dir: Directory for saving messages
            stop_check: Callable that returns True when parent stopped
            exec_id: Pre-assigned execution ID
            temperature: Optional temperature override for LLM (0.0~1.0)
        """
        self.task = task
        self.plan = plan
        self._exec_id = exec_id

        # Load config
        config = load_config()

        # Initialize base agent
        super().__init__(
            config=config,
            workspace_uuid=workspace_uuid,
            cwd=cwd or os.getcwd(),
            session_dir=session_dir,
            stop_check=stop_check,
            max_iterations=config.system.max_iterations,
        )

        self._session_id = exec_id
        logger.debug(f"[SubAgent] Session ID set to: {exec_id}")

        # 创建 session 引用，供工具获取 session_id
        self._session_ref = _SessionIdRef(exec_id)

        # 根代理会话级审批存储（共享同一实例），已批准命令子代理可直接执行
        self.approval_store = approval_store

        # Create LLM client
        logger.debug(f"[SubAgent] Creating LLM client for task: {task[:50]}...")
        self.client = create_llm_client(config.model)
        if temperature is not None:
            self.client.temperature = temperature

        # Create sub tool set (pass session_ref for temp tool etc.)
        self.tools = create_sub_tools(
            cwd=self.cwd,
            workspace_uuid=self.workspace_uuid,
            session_manager=self._session_ref,
            config=config,
            approval_store=self.approval_store,
        )
        self.tool_schemas = [t.to_schema() for t in self.tools]

        # Build system prompt (task/plan goes in first user message, not system prompt)
        self._system_prompt = build_sub_prompt(self.workspace_uuid, self.cwd)

        # Execution tracking
        self._started_at: datetime | None = None
        self.max_consecutive_failures = max_consecutive_failures
        self._budget_warn_triggered = False
        self._budget_final_triggered = False

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
        if not self._budget_final_triggered and i >= int(self.max_iterations * _BUDGET_FINAL_RATIO):
            self._budget_final_triggered = True
            self.add_message(
                "user",
                [{"type": "text", "text": _BUDGET_FINAL_PROMPT.format(used=i, total=self.max_iterations)}],
                meta={"budget": "final"},
            )
        elif not self._budget_warn_triggered and i >= int(self.max_iterations * _BUDGET_WARN_RATIO):
            self._budget_warn_triggered = True
            self.add_message(
                "user",
                [{"type": "text", "text": _BUDGET_WARN_PROMPT.format(used=i, total=self.max_iterations)}],
                meta={"budget": "warn"},
            )

    @staticmethod
    def _downgrade_approval_result(result: dict) -> None:
        """子代理无 ask_user：把"需用户批准"的结果降级为普通错误，不挂起不询问。"""
        if META_KEY in result.get("_meta", {}):
            result["is_error"] = True
            result["content"] = "该命令需要用户（主代理会话）批准，子代理无法执行，请换用非拦截命令或告知主代理。"
            result["_meta"].pop(META_KEY, None)
            result["_meta"].pop("completed", None)

    def run(self) -> dict[str, Any]:
        """Execute SubAgent loop with check phase, return structured result.

        Flow: 目标→计划→执行→检查
        - Pinned task/plan message at start
        - Main execution loop
        - Check phase: verify results, fix if needed, confirm completion
        """
        self._started_at = datetime.now()
        self._stopped = False
        self._running = True
        self._budget_warn_triggered = False
        self._budget_final_triggered = False

        # Build initial pinned message (task + plan, immune to compression)
        self.add_message("user", self._build_task_message(), meta={"pinned": True})

        consecutive_failures = 0
        status = "completed"
        summary = ""
        in_check_phase = False
        check_iters = 0
        max_check_iterations = _MIN_CHECK_ITERATIONS

        try:
            for i in range(self.max_iterations):
                # Check stop
                if self.stop_check and self.stop_check():
                    status = "stopped"
                    summary = "Stopped by user"
                    self._finalize(status, summary, i)
                    return {"status": status, "message": summary, "iterations": i, "usage": self._usage}

                # 额度预警（stop 优先：用户主动停止时不注入）
                self._inject_budget_notice(i)

                try:
                    # Compress if needed
                    self._check_and_compress()

                    # Call LLM (non-streaming)
                    response = self._call_llm(streaming=False, system_prompt=self._system_prompt)
                except Exception as e:
                    summary = format_llm_error(e, self.client.base_url if self.client else "")
                    task_brief = self.task[:50].replace("\n", " ")
                    logger.error(f"[SubAgent] LLM 错误 (iter={i}, exec={self._exec_id}, task='{task_brief}'): {summary}")
                    status = "error"
                    self._finalize(status, summary, i)
                    return {"status": status, "message": summary, "iterations": i, "usage": self._usage}

                tool_calls = response.get_tool_calls()

                if not tool_calls:
                    if not in_check_phase:
                        if self._budget_final_triggered:
                            # 额度兜底下跳过检查阶段，直接交付总结
                            summary = response.get_text() or "预算耗尽，模型未输出总结"
                            self.add_message("assistant", response.content_as_dicts())
                            status = "completed"
                            self._finalize(status, summary, i + 1)
                            return {"status": status, "summary": summary, "iterations": i + 1,
                                    "budget_wrapup": True, "usage": self._usage}

                        # Main phase ended → inject check prompt for verification
                        summary = response.get_text()
                        self.add_message("assistant", response.content_as_dicts())

                        # Inject check prompt (pinned to survive compression)
                        self.add_message("user", _CHECK_PROMPT, meta={"pinned": True})
                        in_check_phase = True
                        check_iters = 0
                        max_check_iterations = max(i, _MIN_CHECK_ITERATIONS)
                        logger.debug(f"[SubAgent] 进入检查阶段 (iter={i}, max_check={max_check_iterations}, exec={self._exec_id})")
                        continue
                    else:
                        # Check phase ended → task truly complete
                        check_iters += 1
                        summary = response.get_text()
                        self.add_message("assistant", response.content_as_dicts())
                        status = "completed"
                        self._finalize(status, summary, i + 1)
                        logger.debug(f"[SubAgent] 检查完成 (check_iters={check_iters}, iter={i})")
                        return {"status": status, "summary": summary, "iterations": i + 1,
                                "check_iterations": check_iters, "usage": self._usage}

                # Track check phase iterations
                if in_check_phase:
                    check_iters += 1
                    if check_iters > max_check_iterations:
                        summary = response.get_text() or "检查阶段超出最大迭代次数"
                        self.add_message("assistant", response.content_as_dicts())
                        status = "completed"
                        self._finalize(status, summary, i + 1)
                        logger.warning(f"[SubAgent] 检查阶段超出迭代上限 (iter={i}, max={max_check_iterations})")
                        return {"status": status, "summary": summary, "iterations": i + 1,
                                "check_iterations": check_iters, "usage": self._usage}

                # Add assistant message with tool calls - convert to dicts for storage
                self.add_message("assistant", response.content_as_dicts())

                # Save progress
                phase = "check" if in_check_phase else "running"
                self._save_progress(i + 1, status=phase)

                # Execute tools
                for tc in tool_calls:
                    self._save_progress(i + 1, status=phase, current_tool=tc.name)

                    # Parse arguments from raw JSON string to dict at execution time
                    input_data = tc.parse_arguments()
                    result = self._execute_tool(tc.name, input_data, tc.id)
                    # 子代理无 ask_user：把"需用户批准"的结果降级为普通错误，不挂起不询问
                    self._downgrade_approval_result(result)
                    self.add_message("user", [result])

                    self._save_progress(i + 1, status=phase)

                    # Track consecutive failures (is_error is Anthropic format)
                    if result.get("is_error"):
                        consecutive_failures += 1
                        if consecutive_failures >= self.max_consecutive_failures:
                            status = "failed"
                            summary = f"Exceeded max consecutive failures ({self.max_consecutive_failures})"
                            self._finalize(status, summary, i + 1)
                            return {"status": status, "message": summary, "iterations": i + 1, "usage": self._usage}
                    else:
                        consecutive_failures = 0

            # Timeout — 额度耗尽，兜底生成一次执行总结
            status = "timeout"
            summary = f"Exceeded max iterations ({self.max_iterations})"
            wrapped_up = False
            if not (self.stop_check and self.stop_check()):
                try:
                    wrapup = self._wrapup_timeout_summary()
                    if wrapup:
                        summary = wrapup
                        wrapped_up = True
                except Exception as e:
                    logger.warning(f"[SubAgent] 兜底总结失败: {e}")
            self._finalize(status, summary, self.max_iterations)
            result = {"status": status, "summary": summary, "iterations": self.max_iterations, "usage": self._usage}
            if wrapped_up:
                result["wrapped_up"] = True
            return result

        finally:
            self._running = False

    def _wrapup_timeout_summary(self) -> str:
        """额度耗尽时兜底生成一次执行总结。

        直接调 client.chat 且不传 tools，杜绝兜底调用再次触发工具循环。
        """
        self._pad_dangling_tool_results()
        self.add_message("user", [{"type": "text", "text": _TIMEOUT_WRAPUP_PROMPT}], meta={"budget": "wrapup"})

        # 消息预处理与 _call_llm_non_streaming 一致
        messages = self._get_messages_with_header()
        messages = self._resolve_tool_results(messages)
        messages = self._strip_meta_from_messages(messages)
        if not self.config.model.multimodal:
            messages = self._strip_images_from_messages(messages)

        response = self.client.chat(
            messages=self._convert_to_message_objects(messages),
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

    def _save_progress(self, iterations: int, status: str = "running", current_tool: str = "") -> None:
        """Save execution progress in real-time.

        Writes to {exec_dir}/index.json in the format SessionManager.load_subagent_log() expects.
        This ensures the file always has exec_id and task, even before the final save.
        """
        if not self.session_dir or not self._exec_id:
            return

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
        }

        log_data = {
            "exec_id": self._exec_id,
            "session_id": self._session_id,
            "task": self.task,
            "metadata": metadata,
            "summary": "",
            "messages": self.messages,
        }

        try:
            log_file = self.session_dir / "index.json"
            atomic_write_json(log_file, log_data)
        except Exception as e:
            logger.warning(f"[SubAgent] Failed to save progress: {e}")

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
                "summary": summary,
            }

            log_data = {
                "exec_id": self._exec_id,
                "session_id": self._session_id,
                "task": self.task,
                "metadata": metadata,
                "summary": summary,
                "messages": self.messages,
            }

            try:
                self.session_dir.mkdir(parents=True, exist_ok=True)
                log_file = self.session_dir / "index.json"
                with open(log_file, "w", encoding="utf-8") as f:
                    json.dump(log_data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"[SubAgent] Failed to finalize: {e}")
