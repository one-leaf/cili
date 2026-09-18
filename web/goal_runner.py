"""GoalRunner — master 经 agent 工具逐轮委派 worker 的目标循环后台编排。

/goal 命令设置目标后，后台 daemon 线程逐轮调用 master 的 agent 工具
委派一个 worker 执行轮次提示（与用户手动委派 / master LLM 循环内委派
走同一条路径）。每轮完成后检查完成标记 / 轮次上限 / 手动暂停，
未完成则重注入续跑提示进入下一轮。

worker 委派复用 AgentTool 既有机制：exec_id 为 exec_{8位hex}、
agent_start/agent_complete 事件、执行日志、工具输出目录（exec_*）全部与
普通委派一致，前端按 worker 卡渲染。goal 级完成/暂停/错误文本不带 exec_id
→ 前端按 master 主聊天渲染。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading

from core.event_bus import get_event_bus
from core.goal import GoalManager

logger = logging.getLogger(__name__)

_SUMMARY_MAX = 2000              # 轮次摘要截断长度
_TOOL_INPUT_MAX = 500            # 占位 tool_use input.task 截断长度
_STATUS_LABELS = {
    "active": "执行中",
    "paused": "已暂停",
    "complete": "已完成",
    "blocked": "受阻",
    "stopped": "已清除",
}


def _publish(workspace_uuid: str, session_id: str, exec_id: str | None, event: dict) -> None:
    """发布带会话壳的事件到全局事件总线。

    exec_id 为空时省略该键 → 前端按 master 主聊天渲染（goal 级状态文本）。
    """
    try:
        payload = {"workspace_uuid": workspace_uuid, "session_id": session_id, **event}
        if exec_id:
            payload["exec_id"] = exec_id
        get_event_bus().publish(payload)
    except Exception as e:
        logger.warning(f"[goal] 事件发布失败: {e}")


def format_goal_status(m: GoalManager) -> str:
    """构建 /goal status 展示文本。"""
    s = m.state
    label = _STATUS_LABELS.get(s.status, s.status)
    if s.status == "blocked" and s.blocked_reason:
        label += f"（{s.blocked_reason}）"
    lines = [
        "**🎯 目标状态**",
        f"- **目标：** {s.objective or '（无）'}",
        f"- **状态：** {label}",
    ]
    if m.exists():
        lines.append(f"- **轮次：** {s.round}/{s.max_rounds}")
        if s.last_summary:
            lines.append(f"- **最近进展：** {s.last_summary[:100]}")
        lines.append("- **命令：** `/goal <新目标>` 重设 · `/goal pause` 暂停 · `/goal resume` 恢复 · `/goal clear` 清除")
    return "\n".join(lines)


class GoalRunner:
    """逐轮委派 worker 执行目标循环的后台线程。"""

    def __init__(
        self,
        workspace_uuid: str,
        session_id: str,
        agent,
        manager: GoalManager,
        agent_tool=None,
    ):
        self.workspace_uuid = workspace_uuid
        self.session_id = session_id
        self.agent = agent
        self.manager = manager
        self.agent_tool = agent_tool
        if self.agent_tool is None:
            self.agent_tool = next(
                (t for t in getattr(agent, "tools", []) or [] if getattr(t, "name", "") == "agent"),
                None,
            )
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    @property
    def key(self) -> str:
        return f"{self.workspace_uuid}:{self.session_id}"

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def request_stop(self) -> None:
        """请求停止：轮间检查跳出，不中断进行中的单轮。"""
        self._stop.set()

    def start(self) -> bool:
        if self.is_running():
            return False
        self._stop.clear()
        self.thread = threading.Thread(target=self._run, name=f"goal-{self.key}", daemon=True)
        self.thread.start()
        return True

    # ── 主循环 ───────────────────────────────────────────

    def _run(self) -> None:
        key = self.key
        try:
            self._goal_loop()
        except Exception as e:
            logger.exception(f"[goal] 目标循环异常: {e}")
            try:
                self.manager.block("runner_error")
            except Exception:
                pass
        finally:
            _unregister_runner(key, self)

    def _goal_loop(self) -> None:
        m = self.manager
        agent_tool = self.agent_tool
        while m.is_active():
            # 轮间停止检查：/goal pause|clear 经 request_stop 置位；
            # UI 停止按钮置 master._stopped → 暂停并跳出（防停止后空转）
            if self._stop.is_set():
                break
            if getattr(self.agent, "_stopped", False):
                m.pause()
                self._round_finish("⏸️ 目标循环已暂停（agent 已停止）。")
                break

            m.state.round += 1
            round_no = m.state.round
            if round_no > m.state.max_rounds:
                m.block("round_limit")
                self._round_finish(
                    f"⏱️ 已达轮次上限 {m.state.max_rounds} 轮，目标未完成，已停止。")
                break

            prompt = m.next_round_prompt()
            if agent_tool is None:
                m.block("runner_error")
                self._round_finish("❌ 未找到 master 的 agent 工具，无法委派 worker 执行目标。")
                break

            # 委派 worker：事件/卡片/日志/exec_id 全部由 AgentTool 完成，
            # 同步阻塞至本轮 worker 结束（AgentTool 内部有 1h 超时兜底）。
            # 先在 master 会话记录占位消息（assistant tool_use + user tool_result 带
            # _meta.exec_id），与普通委派一致 —— 前端 loadSession 重渲染 / 页面重载时
            # renderMessages 按 tool_result._meta.exec_id 重建 worker 卡，否则卡片会消失。
            sm = getattr(self.agent, "session_manager", None)
            exec_id = self._pre_generate_exec_id(sm)
            if sm is not None and exec_id:
                self._record_round_ref(sm, exec_id, prompt)
            try:
                if exec_id:
                    tr = agent_tool.execute(task=prompt, agent_type="worker", exec_id=exec_id)
                else:
                    tr = agent_tool.execute(task=prompt, agent_type="worker")
            except Exception as e:
                logger.error(f"[goal] 第 {round_no} 轮执行异常: {e}")
                if sm is not None and exec_id:
                    self._finish_round_ref(sm, exec_id, {"status": "error", "summary": str(e), "iterations": 0})
                m.block("round_error")
                self._round_finish(f"❌ 第 {round_no} 轮执行异常：{e}")
                break

            result = self._parse_result(tr)
            if sm is not None and exec_id:
                self._finish_round_ref(sm, exec_id, result or {})
            final_text = (result or {}).get("summary") or ""
            completed = m.is_complete(final_text)
            m.set_last_summary(final_text[:_SUMMARY_MAX])
            m.save()

            if completed:
                m.mark_complete(summary=final_text[:_SUMMARY_MAX])
                self._round_finish(f"🎯 目标已完成。\n\n{final_text[:_SUMMARY_MAX]}")
                break
        # end while

    def _round_finish(self, text: str) -> None:
        """goal 级状态文本：进主聊天 + 写入 master 会话（重载可见）。"""
        if not text:
            return
        _publish(self.workspace_uuid, self.session_id, None, {"type": "text", "content": text})
        sm = getattr(self.agent, "session_manager", None)
        if sm is None:
            return
        try:
            sm.add_message("assistant", [{"type": "text", "text": text}])
            sm.save()
        except Exception as e:
            logger.warning(f"[goal] 写入 master 会话失败: {e}")

    # ── 辅助 ─────────────────────────────────────────────

    @staticmethod
    def _parse_result(tr) -> dict:
        """从 agent 工具 ToolResult 解析结果 dict（{status, summary, iterations, ...}）。"""
        output = getattr(tr, "output", None) or ""
        if isinstance(output, str) and output.strip().startswith("{"):
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                pass
        return {}

    @staticmethod
    def _pre_generate_exec_id(sm) -> str:
        """预生成本轮 worker 的 exec_id（与 AgentTool 内部生成规则一致）。"""
        if sm is None:
            return ""
        gen = getattr(getattr(sm, "agent_logs", None), "_generate_exec_id", None)
        if callable(gen):
            return gen()
        return f"sub-{secrets.token_hex(4)}"

    @staticmethod
    def _record_round_ref(sm, exec_id: str, task: str) -> None:
        """在 master 会话记录本轮委派的 tool_use + tool_result 占位消息。

        与普通委派一致：assistant tool_use + user tool_result（带 _meta.exec_id）。
        前端 renderMessages 按 tool_result._meta.exec_id 重建 worker 卡，
        使 goal 轮次卡片在 loadSession 重渲染与页面重载后仍能恢复。
        """
        try:
            tool_use_id = f"goal-{exec_id}"
            sm.add_message("assistant", [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": "agent",
                "input": {"task": task[:_TOOL_INPUT_MAX], "agent_type": "worker"},
            }])
            sm.add_message("user", [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                # jsonl 内容只在追加时写入、不可原地更新，故用一段永不失真的中性说明
                "content": "（目标轮次 worker 委派记录，结果见执行日志与轮次摘要）",
                "is_error": False,
                "_meta": {
                    "tool_name": "agent",
                    "exec_id": exec_id,
                    "completed": False,
                    "iterations": 0,
                    "message_count": 0,
                    "task_summary": task[:_SUMMARY_MAX],
                },
            }])
            sm.save()
        except Exception as e:
            logger.warning(f"[goal] 记录轮次占位消息失败: {e}")

    @staticmethod
    def _finish_round_ref(sm, exec_id: str, result: dict) -> None:
        """把占位 tool_result 更新为完成态（completed=True + 迭代/消息数）。"""
        try:
            for msg in reversed(sm.messages):
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if block.get("type") == "tool_result" and block.get("_meta", {}).get("exec_id") == exec_id:
                        meta = block.setdefault("_meta", {})
                        meta["completed"] = True
                        meta["iterations"] = result.get("iterations", 0)
                        meta["message_count"] = result.get("message_count", 0)
                        # 原地改 _meta 不触发版本变化，save() 会短路 → 必须强制重写 meta.json
                        sm.save(force=True)
                        return
        except Exception as e:
            logger.warning(f"[goal] 更新轮次占位消息失败: {e}")


# ── 每会话单例注册表 ────────────────────────────────────

_runners: dict[str, GoalRunner] = {}
_runners_lock = threading.Lock()
# 每个会话一个启动锁：串行化"停旧 runner + 启动新 runner"，防新旧双线程
# 同时操作同一会话。
_goal_start_locks: dict[str, threading.Lock] = {}
_goal_start_locks_guard = threading.Lock()


def get_runner(key: str) -> GoalRunner | None:
    with _runners_lock:
        return _runners.get(key)


def _register_runner(key: str, runner: GoalRunner) -> None:
    with _runners_lock:
        _runners[key] = runner


def _unregister_runner(key: str, runner: GoalRunner) -> None:
    with _runners_lock:
        if _runners.get(key) is runner:
            _runners.pop(key, None)


def _get_start_lock(key: str) -> threading.Lock:
    with _goal_start_locks_guard:
        if key not in _goal_start_locks:
            _goal_start_locks[key] = threading.Lock()
        return _goal_start_locks[key]


def start_goal_runner(
    workspace_uuid: str,
    session_id: str,
    agent,
    manager: GoalManager,
) -> GoalRunner | None:
    """创建并启动该会话的 GoalRunner。

    若已有旧 runner 在跑，先请求停止并等待其收尾，再启动新 runner。
    旧 runner 60s 内未收尾则放弃并返回 None（本轮仍在执行）。
    阻塞调用，web 层应经 asyncio.to_thread 调用。
    """
    key = f"{workspace_uuid}:{session_id}"
    lock = _get_start_lock(key)
    with lock:
        old = get_runner(key)
        if old is not None and old.is_running():
            old.request_stop()
            if old.thread is not None and old.thread.is_alive():
                old.thread.join(timeout=60)
            if old.is_running():
                logger.warning(f"[goal] 上一轮目标 60s 内未收尾，暂不启动新循环: {key}")
                return None
        runner = GoalRunner(workspace_uuid, session_id, agent, manager)
        _register_runner(key, runner)
        runner.start()
        return runner


def stop_goal_runner(key: str) -> None:
    """请求停止运行中的 runner（轮间检查跳出，不中断进行中的单轮）。"""
    runner = get_runner(key)
    if runner is not None:
        runner.request_stop()
