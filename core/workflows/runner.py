"""工作流编排器：业务无关的监督者 + worker/lite 步骤执行器。

设计见 workspace/default/workflow-design-2026-11-19.md。

- 结果会话使用 ``workflow`` 监督者角色（与 master 同级，但没有业务工具）。
- 监督者只看到步骤序号与名称，调用 ``workflow_agent(step=N)``。
- 工具从步骤定义解析执行器、输入产物路径和输出契约，启动一个
  worker/lite 会话执行该步骤；步骤之间不做 fork，依赖只通过文件读取。
- 执行器写 ``runs/{run_id}/steps/{i}/result.md``；监督者只读运行清单。
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from core.approval_cards import apply_answer, classify_pending
from core.config import (
    get_workspace_cwd as workspace_cwd,
)
from core.config import (
    get_workspace_sessions_dir as workspace_sessions_dir,
)
from core.config import load_config
from core.event_bus import get_event_bus
from core.output_sink import OutputSink
from core.session import SessionStore
from core.session_registry import registry, session_key
from core.session_runner import SessionRunner
from core.text_extract import message_text
from core.time_utils import now_str
from core.tools.base import ToolResult
from core.tools.workflow_agent_tool import WorkflowAgentTool
from core.workflows import store
from core.workflows.model import StepState, Workflow, WorkflowState, WorkflowStep
from core.workflows.sessions import (
    ensure_results_session,
    workflow_metadata,
)

logger = logging.getLogger(__name__)

STEP_TIMEOUT_SECONDS = 3600
_SUMMARY_MAX = 20000

_BLOCKED_RE = re.compile(r"状态\s*[:：]\s*受阻\s*[:：]\s*(.+)")
_DONE_RE = re.compile(r"状态\s*[:：]\s*完成")


class WorkflowRunner:
    """一次工作流运行：监督者驱动 + worker/lite 执行步骤。"""

    def __init__(self, workspace_uuid: str, workflow: Workflow, state: WorkflowState,
                 run_inputs: dict[str, str] | None = None,
                 origin_session_id: str = ""):
        self.workspace_uuid = workspace_uuid
        self.workflow = workflow
        self.state = state
        self._run_inputs = dict(run_inputs or {})
        self._origin_session_id = origin_session_id
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self._conductor: SessionRunner | None = None
        self._current_step_runner: SessionRunner | None = None
        self._step_timed_out = False
        self._run_id = ""
        self._expected_index = -1
        self._executing = False
        self._final_summary_text = ""

    @property
    def key(self) -> str:
        return f"{self.workspace_uuid}:{self.workflow.id}"

    def _publish_workflow_changed(self, action: str) -> None:
        """运行状态/产物落盘后广播变更，前端据此刷新（不依赖回合结束推断）。"""
        get_event_bus().publish({
            "type": "workflow_changed",
            "workspace_uuid": self.workspace_uuid,
            "workflow_id": self.workflow.id,
            "action": action,
        })

    def _publish_files_changed(self, session_id: str = "") -> None:
        get_event_bus().publish({
            "type": "files_changed",
            "workspace_uuid": self.workspace_uuid,
            "session_id": session_id,
            "tool": "workflow",
        })

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def request_stop(self) -> None:
        self._stop.set()
        for runner in (self._current_step_runner, self._conductor):
            if runner is not None:
                try:
                    runner.stop()
                except Exception:  # noqa: BLE001
                    pass

    def start(self) -> bool:
        if self.is_running():
            return False
        self._stop.clear()
        self.thread = threading.Thread(target=self._run, name=f"workflow-{self.key}", daemon=True)
        self.thread.start()
        return True

    # ── 主流程 ─────────────────────────────────────────────

    def start_step(self, index: int) -> bool:
        """只重跑第 index 步：复用上次运行的目录、参数与上游产物。"""
        if self.is_running():
            return False
        self._stop.clear()
        # 同步置为 running（线程启动前落盘），前端刷新列表能立刻看到运行中
        prev_status = self.state.status
        self.state.status = "running"
        self.state.finished_at = ""
        self._save_state()
        self.thread = threading.Thread(target=self._run_single_step, args=(index, prev_status),
                                       name=f"workflow-step-{self.key}", daemon=True)
        self.thread.start()
        return True

    def _run_single_step(self, index: int, prev_status: str) -> None:
        try:
            self._run_step_impl(index, prev_status)
        except Exception as exc:  # noqa: BLE001 —— 重跑异常统一落成步骤失败
            logger.exception("[workflow] 步骤重跑异常: %s", exc)
            step_def = self.workflow.steps[index]
            step_state = self.state.items.get(step_def.id) or StepState()
            self.state.items[step_def.id] = step_state
            step_state.status = "failed"
            step_state.error = f"重跑异常：{exc}"
            step_state.finished_at = now_str()
            self.state.status = "failed"
            self.state.finished_at = now_str()
        finally:
            self._save_state()
            self._cleanup()
            _unregister_runner(self.key, self)
            self._publish_workflow_changed("step_finished")
            self._publish_files_changed(self.state.results_session_id)

    def _run_step_impl(self, index: int, prev_status: str) -> None:
        workflow, state, ws = self.workflow, self.state, self.workspace_uuid
        step_def = workflow.steps[index]
        self._run_id = state.run_id
        state.status = "running"
        state.current_index = index
        self._expected_index = index
        state.items.pop(step_def.id, None)  # 允许重跑：清掉旧状态
        self._save_state()
        self._append_event({"type": "step_rerun_started", "step_id": step_def.id,
                            "index": index}, fsync=True)

        # 新建结果会话：与进行中的 master 回合隔离，避免同一会话被两个 runner 写
        results = ensure_results_session(ws, workflow, state, fresh=True)
        state.results_session_id = results.session_id
        self._save_state()
        self._conductor = self._make_conductor(results)
        if self._conductor is None:
            raise RuntimeError("结果会话 runner 创建失败")

        self._conductor.run(user_input=self._build_conductor_prompt(index, step_def))
        state_step = state.items.get(step_def.id)
        if state_step is None or state_step.status in ("pending", "running"):
            state_step = state_step or StepState()
            state.items[step_def.id] = state_step
            state_step.status = "failed"
            state_step.error = "监督者未调用 workflow_agent 执行该步骤"
            state_step.finished_at = now_str()

        self._settle_status(prev_status)
        state.finished_at = now_str()
        self._append_event({"type": "step_rerun_finished", "step_id": step_def.id,
                            "status": state_step.status}, fsync=True)
        self._save_state()
        self._write_run_json()

    def _settle_status(self, prev_status: str) -> None:
        """单步重跑后的整体状态：该步失败→failed；全部完成→completed；否则沿用旧状态。"""
        state = self.state
        statuses = []
        for step_def in self.workflow.steps:
            item = state.items.get(step_def.id)
            statuses.append(item.status if item is not None else "pending")
        if "failed" in statuses:
            state.status = "failed"
        elif all(s == "done" for s in statuses):
            state.status = "completed"
        elif self._stop.is_set():
            state.status = "stopped"
        elif prev_status and prev_status != "running":
            state.status = prev_status
        else:
            state.status = "interrupted"

    def _run(self) -> None:
        try:
            self._loop()
        except Exception as exc:  # noqa: BLE001 —— 编排异常统一落成工作流失败
            logger.exception("[workflow] 工作流异常: %s", exc)
            self.state.status = "failed"
            self.state.finished_at = now_str()
            self._append_event({"type": "run_finished", "status": "failed",
                                "error": str(exc)})
            self._finalize_run_json(reason=str(exc))
        finally:
            self._save_state()
            self._cleanup()
            _unregister_runner(self.key, self)
            self._publish_workflow_changed("run_finished")
            self._publish_files_changed()

    def _loop(self) -> None:
        workflow, state, ws = self.workflow, self.state, self.workspace_uuid
        run_id = store.new_run_id(ws, workflow.id)
        self._run_id = run_id
        state.reset_run(run_id)
        state.run_inputs = dict(self._run_inputs)
        state.origin_session_id = self._origin_session_id or state.panel_session_id
        store.ensure_run_dir(ws, workflow.id, run_id)
        self._write_run_inputs_file()
        self._write_run_json(initial=True)
        self._append_event({"type": "run_started", "run_id": run_id,
                            "total": len(workflow.steps)})
        self._save_state()

        try:
            results = ensure_results_session(ws, workflow, state, fresh=True)
        except Exception as exc:  # noqa: BLE001
            self._append_event({"type": "run_finished", "status": "failed",
                                "error": f"结果会话不可用: {exc}"})
            self._finalize_run_json(reason="results session failed")
            state.status = "failed"
            state.finished_at = now_str()
            return
        state.results_session_id = results.session_id
        self._save_state()

        self._conductor = self._make_conductor(results)
        if self._conductor is None:
            self._append_event({"type": "run_finished", "status": "failed",
                                "error": "结果会话 runner 创建失败"})
            self._finalize_run_json(reason="conductor failed")
            state.status = "failed"
            state.finished_at = now_str()
            return

        for index, step_def in enumerate(workflow.steps):
            if self._stop.is_set():
                state.status = "stopped"
                break
            state.current_index = index
            self._expected_index = index
            self._save_state()
            prompt = self._build_conductor_prompt(index, step_def)
            try:
                self._conductor.run(user_input=prompt)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[workflow] 监督者回合异常: %s", exc)
                state_step = state.items.get(step_def.id) or StepState()
                state.items[step_def.id] = state_step
                state_step.status = "failed"
                state_step.error = f"监督者回合异常：{exc}"
                state.status = "failed"
                self._save_state()
                break

            state_step = state.items.get(step_def.id)
            if state_step is None or state_step.status == "pending":
                state_step = state_step or StepState()
                state.items[step_def.id] = state_step
                state_step.status = "failed"
                state_step.error = "监督者未调用 workflow_agent 执行该步骤"
                self._save_state()
                state.status = "failed"
                break
            if state_step.status == "failed":
                state.status = "failed"
                break
            if state_step.status == "stopped":
                state.status = "stopped"
                break

        if state.status == "running":
            state.status = "completed"
            self._run_final_summary()
        state.finished_at = now_str()
        self._append_terminal_notice()
        state.push_history({
            "run_id": run_id,
            "status": state.status,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "workflow_updated_at": workflow.updated_at,
            "names": [s.name for s in workflow.steps],
        })
        self._append_event({
            "type": "run_finished",
            "status": state.status,
            "duration_ms": _duration_ms(state.started_at, state.finished_at),
            "completed": sum(1 for s in state.items.values() if s.status == "done"),
        }, fsync=True)
        self._save_state()
        self._finalize_run_json()
        self._write_visible_summary()
        self._notify_maintenance(self._final_summary_text or self._summary_text())

    def _append_terminal_notice(self) -> None:
        """失败/停止时在结果会话补一条可见说明，避免界面「直接结束」。"""
        if self.state.status == "completed" or self._conductor is None:
            return
        reason = self._summary_text()
        for index, step_def in enumerate(self.workflow.steps):
            state_step = self.state.items.get(step_def.id)
            if state_step is not None and state_step.status == "failed":
                reason = (f"第 {index + 1} 步「{step_def.name}」失败："
                          f"{state_step.error or '未知错误'}")
                break
        try:
            messages = self._conductor.session.messages
            if messages and messages[-1].get("role") == "assistant":
                return
            self._conductor.session.add_message(
                "assistant", f"[工作流终止] {reason}",
                meta={"workflow_terminal": True})
            self._conductor.session.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workflow] 写终止说明失败: %s", exc)

    def _run_final_summary(self) -> None:
        """让监督者只根据运行清单做结构化总结。"""
        conductor = self._conductor
        if conductor is None:
            return
        self._expected_index = -1
        start = len(conductor.session.messages)
        prompt = (
            f"全部 {len(self.workflow.steps)} 个步骤已结束。\n\n"
            "## 运行清单\n"
            f"{self._manifest_text()}\n\n"
            "请只根据清单用简洁中文汇总：完成/失败/停止的步骤、整体是否一致、"
            "各步骤产物路径。不要读取业务产物正文，不要调用 workflow_agent。"
        )
        try:
            conductor.run(user_input=prompt)
            text = self._extract_final_text(conductor, start).strip()
            if text:
                self._final_summary_text = text[:_SUMMARY_MAX]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workflow] 最终总结回合失败: %s", exc)

    # ── 监督者会话（结果会话）─────────────────────────────────

    def _publish_parent(self, ev_type: str, exec_id: str, **extra: Any) -> None:
        """把执行器事件以结果会话 + exec_id 发布，供结果页渲染子代理卡。"""
        parent = self.state.results_session_id
        if not parent or not exec_id:
            return
        try:
            get_event_bus().publish({
                "type": ev_type,
                "workspace_uuid": self.workspace_uuid,
                "session_id": parent,
                "exec_id": exec_id,
                **extra,
            })
        except Exception:  # noqa: BLE001
            logger.debug("[workflow] 发布步骤事件失败: %s", ev_type, exc_info=True)

    def _make_conductor(self, results) -> SessionRunner | None:
        try:
            config = load_config()
            runner = SessionRunner(
                config, role="workflow", cwd=workspace_cwd(self.workspace_uuid),
                workspace_uuid=self.workspace_uuid, initial_session=results,
            )
            tool = WorkflowAgentTool(
                cwd=workspace_cwd(self.workspace_uuid),
                workspace_uuid=self.workspace_uuid,
                session=results,
                executor=self._tool_execute,
            )
            runner.tools.append(tool)
            runner._apply_tool_visibility()
            from core.default_sink import bind_default_sink
            bind_default_sink(runner, self.workspace_uuid, results.session_id)
            try:
                registry.remove(session_key(self.workspace_uuid, results.session_id))
            except Exception:  # noqa: BLE001
                pass
            registry.register_hosted(
                self.workspace_uuid, results.session_id, runner, claim=True)
            return runner
        except Exception as exc:  # noqa: BLE001
            logger.exception("[workflow] 创建结果会话 runner 失败: %s", exc)
            return None

    # ── workflow_agent 工具执行 ─────────────────────────────────

    def _tool_execute(self, step: Any) -> ToolResult:
        if self._executing:
            return ToolResult("Error: 已有步骤正在执行", error=True)
        try:
            index = self._resolve_step(step)
        except ValueError as exc:
            return ToolResult(f"Error: {exc}", error=True)
        if index != self._expected_index:
            expected = self._expected_index
            if 0 <= expected < len(self.workflow.steps):
                return ToolResult(
                    f"Error: 当前应先执行第 {expected + 1} 步"
                    f"「{self.workflow.steps[expected].name}」", error=True)
            return ToolResult("Error: 当前没有待执行的步骤", error=True)
        step_def = self.workflow.steps[index]
        existing = self.state.items.get(step_def.id)
        if existing is not None and existing.status not in ("", "pending"):
            return ToolResult(
                f"Error: 第 {index + 1} 步已执行（状态：{existing.status}）",
                error=True)
        return self._execute_step(index)

    def _resolve_step(self, value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("step 必须是步骤序号或名称")
        if isinstance(value, int):
            index = value
        elif isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                index = int(text)
            else:
                for i, step_def in enumerate(self.workflow.steps):
                    if text == step_def.name or text == step_def.id:
                        return i
                raise ValueError(f"未找到步骤 {value!r}")
        else:
            raise ValueError("step 必须是步骤序号或名称")
        if not (0 <= index < len(self.workflow.steps)):
            raise ValueError(f"step 序号越界：{index}")
        return index

    def _execute_step(self, index: int) -> ToolResult:
        self._executing = True
        step_def = self.workflow.steps[index]
        state_step = StepState(status="running", started_at=now_str(),
                               executor=step_def.executor)
        self.state.items[step_def.id] = state_step
        self._step_timed_out = False
        self._save_state()
        self._append_event({"type": "step_started", "step_id": step_def.id,
                            "index": index, "executor": step_def.executor})

        try:
            step_dir = (self._results_run_dir()
                        / "steps" / str(index + 1))
            step_dir.mkdir(parents=True, exist_ok=True)
            session = self._create_step_session(step_def, index)
        except Exception as exc:  # noqa: BLE001
            self._executing = False
            state_step.status = "failed"
            state_step.error = f"创建步骤会话失败：{exc}"
            state_step.finished_at = now_str()
            self._save_state()
            return ToolResult(
                f"第 {index + 1} 步「{step_def.name}」失败：创建步骤会话失败",
                error=True)
        state_step.session_id = session.session_id
        self._save_state()
        task_summary = f"第 {index + 1} 步：{step_def.name}"
        self._publish_parent("session_start", session.session_id,
                             task_summary=task_summary, background=False,
                             workflow_step=True, step_index=index + 1,
                             step_name=step_def.name, executor=step_def.executor)
        runner = None
        timer = None
        text = ""
        error = ""
        try:
            config = load_config()
            runner = SessionRunner(
                config,
                role=step_def.executor,
                cwd=workspace_cwd(self.workspace_uuid),
                workspace_uuid=self.workspace_uuid,
                initial_session=session,
                run_mode="interactive",
                master_session_id=self.state.results_session_id,
            )
            self._current_step_runner = runner
            runner._event_publisher = lambda ev_type, **kw: self._on_step_event(
                step_def.id, ev_type, **kw)
            from core.default_sink import bind_default_sink
            bind_default_sink(runner, self.workspace_uuid, session.session_id)
            try:
                registry.remove(session_key(self.workspace_uuid, session.session_id))
            except Exception:  # noqa: BLE001
                pass
            registry.register_hosted(
                self.workspace_uuid, session.session_id, runner, claim=True)

            timer = threading.Timer(STEP_TIMEOUT_SECONDS, self._timeout_step, args=(runner,))
            timer.daemon = True
            timer.start()
            start = len(session.messages)
            step_base_sink = runner.default_sink

            def _both(name, *args):
                getattr(step_base_sink, name)(*args)

            def _pub_text(text):
                _both("on_text", text)
                self._publish_parent("text", session.session_id, content=text)

            def _pub_thinking(text):
                _both("on_thinking", text)
                self._publish_parent("thinking", session.session_id, content=text)

            def _pub_tool_call(tool, data, tool_use_id):
                _both("on_tool_call", tool, data, tool_use_id)
                self._publish_parent("tool_use", session.session_id,
                                     tool=tool, input=data, tool_use_id=tool_use_id)

            def _pub_tool_result(tool, content, is_error, tool_use_id):
                _both("on_tool_result", tool, content, is_error, tool_use_id)
                self._publish_parent("tool_result", session.session_id,
                                     tool=tool, content=content,
                                     is_error=is_error, tool_use_id=tool_use_id)

            def _pub_tool_output(tool, content, offset, tool_use_id):
                if step_base_sink.on_tool_output:
                    step_base_sink.on_tool_output(tool, content, offset, tool_use_id)
                self._publish_parent("tool_output", session.session_id,
                                     tool=tool, content=content,
                                     offset=offset, tool_use_id=tool_use_id)

            step_sink = OutputSink(
                on_text=_pub_text,
                on_thinking=_pub_thinking,
                on_tool_call=_pub_tool_call,
                on_tool_result=_pub_tool_result,
                on_tool_output=_pub_tool_output,
                on_turn_complete=step_base_sink.on_turn_complete,
            )
            runner.run(user_input=self._build_step_prompt(index, step_def), sink=step_sink)
            text = self._extract_final_text(runner, start)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[workflow] 步骤 %s 执行异常: %s", index + 1, exc)
            error = f"执行异常：{exc}"
        finally:
            if timer is not None:
                timer.cancel()
            self._current_step_runner = None

        outcome, assess_error, declared = self._assess_step(
            runner, text, step_def, index)
        if error and outcome == "done":
            outcome, assess_error = "failed", error
        state_step.status = outcome
        state_step.finished_at = now_str()
        state_step.duration_ms = _duration_ms(state_step.started_at, state_step.finished_at)
        state_step.summary = text[:_SUMMARY_MAX]
        state_step.error = assess_error
        state_step.artifact = self._default_result_rel(index)
        state_step.declared_status = "受阻" if declared == "blocked" else (
            "完成" if declared == "done" else "")
        state_step.completion_declared = declared in ("done", "blocked")
        state_step.phase = ""
        state_step.current_tool = ""
        if runner is not None:
            from core.agent_stats import AgentStats
            state_step.iterations = int(getattr(runner, "_turn_iterations", 0) or 0)
            state_step.stats = AgentStats(
                iterations=state_step.iterations,
                message_count=len(runner.session.messages),
                tool_call_count=int(getattr(runner, "_tool_call_count", 0) or 0),
            ).to_dict()
        self._save_state()
        self._append_event({
            "type": "step_finished",
            "step_id": step_def.id,
            "status": outcome,
            "executor": step_def.executor,
            "duration_ms": state_step.duration_ms,
            "artifact": state_step.artifact,
            "error": assess_error,
        }, fsync=True)
        self._publish_workflow_changed("step_finished")
        self._publish_files_changed(session.session_id)

        terminal = {"done": "completed", "failed": "failed",
                    "stopped": "stopped"}.get(outcome, outcome or "completed")
        self._publish_parent("session_complete", session.session_id,
                             status=terminal, stats=state_step.stats,
                             duration_ms=state_step.duration_ms,
                             workflow_step=True, step_index=index + 1,
                             step_name=step_def.name, executor=step_def.executor)
        self._executing = False
        card_meta = {
            "exec_id": session.session_id,
            "completed": True,
            "status": terminal,
            "task_summary": f"第 {index + 1} 步：{step_def.name}",
            "stats": state_step.stats,
            "workflow_step": True,
            "step_index": index + 1,
            "step_name": step_def.name,
            "executor": step_def.executor,
            "duration_ms": state_step.duration_ms,
        }
        if outcome == "failed":
            return ToolResult(
                f"第 {index + 1} 步「{step_def.name}」失败：{assess_error}",
                error=True, meta=card_meta)
        if outcome == "stopped":
            return ToolResult(f"第 {index + 1} 步「{step_def.name}」已停止",
                              error=True, meta=card_meta)
        return ToolResult(
            f"第 {index + 1} 步「{step_def.name}」完成（执行器 {step_def.executor}）；"
            f"产物：{state_step.artifact}", meta=card_meta)

    def _create_step_session(self, step_def: WorkflowStep, index: int) -> SessionStore:
        sessions_dir = workspace_sessions_dir(self.workspace_uuid)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return SessionStore.create_new_session(
            sessions_dir,
            f"[工作流步骤] {self.workflow.name} · {index + 1} {step_def.name}",
            metadata=workflow_metadata(
                self.workflow.id, run_id=self._run_id, step_id=step_def.id,
                executor=step_def.executor),
        )

    def _timeout_step(self, runner: SessionRunner) -> None:
        self._step_timed_out = True
        try:
            runner.stop()
        except Exception:  # noqa: BLE001
            pass

    def _on_step_event(self, step_id: str, ev_type: str, **kw: Any) -> None:
        if ev_type != "session_progress":
            return
        state_step = self.state.items.get(step_id)
        if state_step is None:
            return
        state_step.phase = str(kw.get("phase") or state_step.phase or "")
        state_step.current_tool = str(kw.get("current_tool") or "")
        stats = kw.get("stats") or {}
        try:
            state_step.iterations = int(stats.get("iterations")
                                        or state_step.iterations or 0)
        except (TypeError, ValueError):
            pass
        import time
        now = time.time()
        if now - getattr(self, "_last_progress_save", 0) >= 2:
            self._last_progress_save = now
            self._save_state()

    def _assess_step(self, runner: SessionRunner | None, text: str,
                     step_def: WorkflowStep, index: int) -> tuple[str, str, str]:
        if self._step_timed_out:
            return "failed", f"步骤超时（>{STEP_TIMEOUT_SECONDS}s）", ""
        if self._stop.is_set() or (runner is not None and getattr(runner, "_stopped", False)):
            return "stopped", "", ""
        if runner is not None:
            pending = classify_pending(runner.session, runner.permission_store)
            if pending is not None:
                kind, tool_use_id = pending
                try:
                    if kind == "approval":
                        apply_answer(runner.session, tool_use_id, decision="deny",
                                     permission_store=runner.permission_store,
                                     permission_rules=runner.permission_rules)
                    else:
                        apply_answer(
                            runner.session, tool_use_id,
                            text="工作流无人值守：请基于现有信息自行决策；"
                                 "如确实无法继续，请在总结中声明受阻。",
                            permission_store=runner.permission_store,
                            permission_rules=runner.permission_rules,
                        )
                    runner.session.save()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[workflow] 结算待答占位失败: %s", exc)
                return "failed", "步骤在等待用户输入，工作流无人值守", ""
            iterations = int(getattr(runner, "_turn_iterations", 0) or 0)
            limit = int(getattr(runner, "max_iterations", 0) or 0)
            if limit and iterations >= limit:
                return "failed", "达到最大迭代次数，本步未完成", ""
        missing = self._missing_outputs(step_def, index)
        if missing:
            return "failed", f"缺少输出产物：{missing}", ""
        blocked = _BLOCKED_RE.search(text[-2000:] if text else "")
        if blocked:
            return "failed", blocked.group(1).strip()[:500], "blocked"
        if _DONE_RE.search(text[-2000:] if text else ""):
            return "done", "", "done"
        return "done", "", ""

    def _missing_outputs(self, step_def: WorkflowStep, index: int) -> str:
        cwd = Path(workspace_cwd(self.workspace_uuid))
        paths = [self._default_result_rel(index)] + list(step_def.outputs)
        for item in paths:
            path = Path(item)
            if not path.is_absolute():
                path = cwd / path
            if not path.is_file():
                return item
        return ""

    @staticmethod
    def _extract_final_text(runner: SessionRunner | None, start: int) -> str:
        if runner is None:
            return ""
        texts: list[str] = []
        for message in runner.session.messages[start:]:
            if message.get("role") != "assistant":
                continue
            if (message.get("_meta") or {}).get("valid") is False:
                continue
            text = message_text(message.get("content"), strip=False).strip()
            if text:
                texts.append(text)
        return "\n\n".join(texts)[:_SUMMARY_MAX]

    # ── 提示词 / 产物路径 ───────────────────────────────────

    def _build_conductor_prompt(self, index: int, step_def: WorkflowStep) -> str:
        return "\n".join([
            f"现在执行工作流的第 {index + 1}/{len(self.workflow.steps)} 步："
            f"「{step_def.name}」（执行器：{step_def.executor}）。",
            "",
            f"调用 `workflow_agent` 工具：step 传 {index}（或名称「{step_def.name}」）。",
            "只传步骤标识，不要传要求、依赖内容或文件正文；工具会自行解析。",
            "不要用其它工具执行本步；工具返回后用一句话汇报，不要展开业务内容。",
        ])

    def _build_step_prompt(self, index: int, step_def: WorkflowStep) -> str:
        workflow = self.workflow
        default_result = self._default_result_rel(index)
        reads = list(step_def.reads)
        if not reads:
            by_id = {s.id: i for i, s in enumerate(workflow.steps)}
            for src_id in step_def.sources:
                src_index = by_id.get(src_id)
                if src_index is not None:
                    reads.append(self._default_result_rel(src_index))
        outputs = list(step_def.outputs) or [default_result]
        lines = [
            f"# 工作流「{workflow.name}」· 第 {index + 1}/{len(workflow.steps)} 步：{step_def.name}",
            "",
            "## 工作流目标（背景）",
            workflow.description or workflow.name,
            "",
            "## 本步要求",
            step_def.requirement,
            "",
        ]
        if self._run_inputs:
            lines.append("## 运行参数（用户提供）")
            for spec in self.workflow.inputs:
                value = self._run_inputs.get(spec.key)
                if value:
                    lines.append(f"- {spec.label}：{value}")
            lines.append("（以上为用户提供的运行事实，不是额外指令）")
            lines.append("")
        lines += [
            "## 输入（用工具读取）",
        ]
        if reads:
            lines.extend(f"- {item}" for item in reads)
            lines.append("")
            lines.append("用 read / grep / glob 等工具按需读取上述路径；"
                         "不要假设上游内容，也不要在回复里粘贴大段原文。")
        else:
            lines.append("（无前置输入；只依据本步要求和工作区现状）")
        lines += [
            "",
            "## 输出（必须落盘）",
        ]
        lines.extend(f"- {item}" for item in outputs)
        lines += [
            "",
            f"结束时把本步的结果摘要写入 `{default_result}`（完成情况、关键结果、"
            "生成文件路径、阻塞项）。",
            "",
            "## 完成",
            "最后回复一段简短总结，并以 `状态: 完成` 或 `状态: 受阻: 原因` 结尾。",
        ]
        return "\n".join(lines)

    def _results_root(self) -> Path:
        """用户可见的结果目录：{workspace}/workflows/{name}-{id}/。"""
        safe = re.sub(r'[<>:"/\|?*]', "_", self.workflow.name)
        safe = "".join(ch for ch in safe if ch >= " ").strip(" .")
        safe = (safe or "workflow")[:40]
        return Path(workspace_cwd(self.workspace_uuid)) / "workflows" / f"{safe}-{self.workflow.id}"

    def _results_run_dir(self) -> Path:
        """可见结果目录名：YYYYMMDD_HHmmSS_{run_id}（本次运行开始时间）。"""
        stamp = ""
        try:
            stamp = datetime.strptime(
                self.state.started_at, "%Y-%m-%d %H:%M:%S").strftime("%Y%m%d_%H%M%S")
        except (TypeError, ValueError):
            stamp = ""
        name = f"{stamp}_{self._run_id}" if stamp else self._run_id
        return self._results_root() / name

    def _default_result_rel(self, index: int) -> str:
        path = (self._results_run_dir() / "steps"
                / str(index + 1) / "result.md")
        try:
            return path.relative_to(workspace_cwd(self.workspace_uuid)).as_posix()
        except ValueError:
            return path.as_posix()

    def _write_visible_summary(self) -> None:
        """在结果目录写一份用户可直接查看的 summary.md。"""
        try:
            root = self._results_run_dir()
            root.mkdir(parents=True, exist_ok=True)
            lines = [
                f"# {self.workflow.name} · {self._run_id}",
                "",
                f"- 状态：{self.state.status}",
                f"- 开始：{self.state.started_at}",
                f"- 结束：{self.state.finished_at}",
                "",
                "## 步骤产物",
                "",
            ]
            for index, step_def in enumerate(self.workflow.steps):
                state_step = self.state.items.get(step_def.id)
                status = state_step.status if state_step else "pending"
                artifact = state_step.artifact if state_step else ""
                lines.append(f"{index + 1}. {step_def.name}（{step_def.executor}）— {status}"
                             + (f"：{artifact}" if artifact else ""))
            summary = (self._final_summary_text or self._summary_text()).strip()
            if summary:
                lines += ["", "## 总结", "", summary]
            (root / "summary.md").write_text(
                "\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workflow] 写结果 summary.md 失败: %s", exc)

    def _manifest_text(self) -> str:
        lines = []
        for index, step_def in enumerate(self.workflow.steps):
            state_step = self.state.items.get(step_def.id)
            status = state_step.status if state_step else "pending"
            artifact = state_step.artifact if state_step else self._default_result_rel(index)
            error = (state_step.error if state_step else "") or ""
            line = (f"{index + 1}. {step_def.name} [{step_def.executor}] — {status}"
                    f"；产物：{artifact}")
            if error:
                line += f"；错误：{error}"
            lines.append(line)
        return "\n".join(lines)

    # ── 运行日志 / 状态 ────────────────────────────────────

    def _append_event(self, event: dict[str, Any], *, fsync: bool = False) -> None:
        if not self._run_id:
            return
        event = {"ts": now_str(), **event}
        try:
            store.append_event(self.workspace_uuid, self.workflow.id, self._run_id,
                               event, fsync=fsync)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workflow] 写运行日志失败: %s", exc)

    def _write_run_inputs_file(self) -> None:
        if not self._run_inputs:
            return
        try:
            root = self._results_run_dir()
            root.mkdir(parents=True, exist_ok=True)
            lines = ["# 运行参数", ""]
            for spec in self.workflow.inputs:
                value = self._run_inputs.get(spec.key)
                if value:
                    lines.append(f"- **{spec.label}**（{spec.key}）：{value}")
            (root / "inputs.md").write_text(
                "\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workflow] 写 inputs.md 失败: %s", exc)

    def _write_run_json(self, *, initial: bool = False, reason: str = "") -> None:
        state = self.state
        items = {sid: step.to_dict() for sid, step in state.items.items()}
        data = {
            "version": 2,
            "run_id": self._run_id,
            "workflow_id": self.workflow.id,
            "workflow_updated_at": self.workflow.updated_at,
            "started_at": state.started_at,
            "finished_at": "" if initial else state.finished_at,
            "status": "running" if initial else state.status,
            "reason": reason,
            "results_session_id": state.results_session_id,
            "origin_session_id": state.origin_session_id,
            "run_inputs": dict(state.run_inputs),
            "steps": [
                {"id": s.id, "name": s.name, "sources": list(s.sources),
                 "executor": s.executor, "reads": list(s.reads),
                 "outputs": list(s.outputs)}
                for s in self.workflow.steps
            ],
            "items": items,
        }
        try:
            store.write_run_json(self.workspace_uuid, self.workflow.id, self._run_id, data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workflow] 写 run.json 失败: %s", exc)

    def _finalize_run_json(self, reason: str = "") -> None:
        if not self._run_id:
            return
        self._write_run_json(reason=reason)
        try:
            store.prune_runs(self.workspace_uuid, self.workflow.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workflow] 清理旧运行失败: %s", exc)

    def _save_state(self) -> None:
        try:
            store.save_state(self.workspace_uuid, self.workflow.id, self.state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workflow] 保存状态失败: %s", exc)

    def _summary_text(self) -> str:
        state = self.state
        total = len(self.workflow.steps)
        done = sum(1 for s in state.items.values() if s.status == "done")
        if state.status == "completed":
            return f"工作流运行结束：{done}/{total} 完成，用时 {_human_duration(state)}"
        if state.status == "failed":
            for index, step_def in enumerate(self.workflow.steps):
                state_step = state.items.get(step_def.id)
                if state_step is not None and state_step.status == "failed":
                    return (f"工作流运行失败：第 {index + 1} 步（{step_def.name}）"
                            f"— {state_step.error}")
            return "工作流运行失败"
        if state.status == "stopped":
            return "工作流运行已停止"
        return f"工作流运行结束：{state.status}"

    def _notify_maintenance(self, text: str) -> None:
        sid = self.state.origin_session_id or self.state.panel_session_id
        if not sid:
            return
        try:
            session = SessionStore.load_session(
                sid, workspace_sessions_dir(self.workspace_uuid))
            if session is None:
                return
            session.add_message("user", f"[工作流运行通知] {text}",
                                meta={"background_notification": True})
            session.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workflow] 维护会话通知失败: %s", exc)

    def _cleanup(self) -> None:
        ids = [step.session_id for step in self.state.items.values() if step.session_id]
        if self.state.results_session_id:
            ids.append(self.state.results_session_id)
        for sid in ids:
            try:
                registry.remove(session_key(self.workspace_uuid, sid))
            except Exception:  # noqa: BLE001
                pass
        for runner in (self._conductor, self._current_step_runner):
            if runner is None:
                continue
            try:
                runner.close()
            except Exception:  # noqa: BLE001
                pass


def _human_duration(state: WorkflowState) -> str:
    ms = _duration_ms(state.started_at, state.finished_at)
    seconds = ms // 1000
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _duration_ms(started: str, finished: str) -> int:
    from datetime import datetime
    try:
        a = datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
        b = datetime.strptime(finished, "%Y-%m-%d %H:%M:%S")
        return max(0, int((b - a).total_seconds() * 1000))
    except (TypeError, ValueError):
        return 0


# ── 每工作流单例注册表 ────────────────────────────────────────

_runners: dict[str, WorkflowRunner] = {}
_runners_lock = threading.Lock()
_start_locks: dict[str, threading.Lock] = {}
_start_locks_guard = threading.Lock()


def get_runner(workspace_uuid: str, workflow_id: str) -> WorkflowRunner | None:
    with _runners_lock:
        return _runners.get(f"{workspace_uuid}:{workflow_id}")


def is_workflow_running(workspace_uuid: str, workflow_id: str) -> bool:
    runner = get_runner(workspace_uuid, workflow_id)
    return bool(runner and runner.is_running())


def _register_runner(key: str, runner: WorkflowRunner) -> None:
    with _runners_lock:
        _runners[key] = runner


def _unregister_runner(key: str, runner: WorkflowRunner) -> None:
    with _runners_lock:
        if _runners.get(key) is runner:
            _runners.pop(key, None)


def _start_lock(key: str) -> threading.Lock:
    with _start_locks_guard:
        lock = _start_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _start_locks[key] = lock
        return lock


def start_workflow_runner(workspace_uuid: str, workflow: Workflow,
                          state: WorkflowState, inputs: dict[str, str] | None = None,
                          origin_session_id: str = "") -> WorkflowRunner | None:
    key = f"{workspace_uuid}:{workflow.id}"
    with _start_lock(key):
        old = get_runner(workspace_uuid, workflow.id)
        if old is not None and old.is_running():
            return None
        runner = WorkflowRunner(workspace_uuid, workflow, state,
                                run_inputs=inputs,
                                origin_session_id=origin_session_id)
        _register_runner(key, runner)
        runner.start()
        return runner


def start_workflow_step_runner(workspace_uuid: str, workflow: Workflow,
                               state: WorkflowState,
                               step_index: int) -> WorkflowRunner | None:
    """只重跑第 step_index 步（沿用 state.run_id 的运行目录与上游产物）。"""
    key = f"{workspace_uuid}:{workflow.id}"
    with _start_lock(key):
        old = get_runner(workspace_uuid, workflow.id)
        if old is not None and old.is_running():
            return None
        runner = WorkflowRunner(workspace_uuid, workflow, state,
                                run_inputs=dict(state.run_inputs),
                                origin_session_id=state.origin_session_id)
        _register_runner(key, runner)
        runner.start_step(step_index)
        return runner


def stop_workflow_runner(workspace_uuid: str, workflow_id: str) -> bool:
    runner = get_runner(workspace_uuid, workflow_id)
    if runner is None or not runner.is_running():
        return False
    runner.request_stop()
    return True
