"""GoalRunner — master 目标驱动循环的后台编排。

/goal 命令设置目标后，后台 daemon 线程逐轮执行 agent.run(goal_round_msg)。
每轮结束（自然完成或迭代预算耗尽）检查完成标记 / 轮次上限 / 手动暂停，
未完成则重注入续跑提示进入下一轮。事件发布到全局事件总线，前端 EventSource
复用 worker 卡片机制流式显示，无需前端改动。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from core.event_bus import get_event_bus
from core.goal import GoalManager

logger = logging.getLogger(__name__)

_ROUND_PREFIX = "goal-"          # 轮次 exec_id 前缀（前端按此建卡）
_SUMMARY_MAX = 2000              # 轮次摘要截断长度
_CLAIM_RETRIES = 30              # 执行权认领重试次数（间隔 1s，共 ~30s）
_CLAIM_RETRY_INTERVAL = 1.0
_STATUS_LABELS = {
    "active": "执行中",
    "paused": "已暂停",
    "complete": "已完成",
    "blocked": "受阻",
    "stopped": "已清除",
}


def _publish(workspace_uuid: str, session_id: str, exec_id: str, event: dict) -> None:
    """发布带会话壳的事件到全局事件总线（无 exec_id 用于会话级事件）。"""
    try:
        get_event_bus().publish({
            "workspace_uuid": workspace_uuid,
            "session_id": session_id,
            "exec_id": exec_id,
            **event,
        })
    except Exception as e:
        logger.warning(f"[goal] 事件发布失败: {e}")


def _make_round_callbacks(workspace_uuid: str, session_id: str, exec_id: str) -> dict:
    """构造 agent.run 的回调组：事件全部发布到全局事件总线。"""
    return {
        "on_text": lambda t, _id=exec_id: _publish(workspace_uuid, session_id, _id, {"type": "text", "content": t}),
        "on_thinking": lambda t, _id=exec_id: _publish(workspace_uuid, session_id, _id, {"type": "thinking", "content": t}),
        "on_tool_call": lambda tool, inp, tid, _id=exec_id: _publish(
            workspace_uuid, session_id, _id, {"type": "tool_use", "tool": tool, "input": inp, "tool_use_id": tid}),
        "on_tool_result": lambda tool, content, is_error, tid, _id=exec_id: _publish(
            workspace_uuid, session_id, _id, {"type": "tool_result", "tool": tool, "content": content, "is_error": is_error, "tool_use_id": tid}),
        "on_agent_start": lambda eid, task_summary, _id=exec_id: _publish(
            workspace_uuid, session_id, _id, {"type": "agent_start", "exec_id": eid, "task_summary": task_summary}),
        "on_agent_complete": lambda eid, _id=exec_id: _publish(
            workspace_uuid, session_id, _id, {"type": "agent_complete", "exec_id": eid}),
    }


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
    """逐轮执行目标循环的后台线程。"""

    def __init__(
        self,
        workspace_uuid: str,
        session_id: str,
        agent,
        manager: GoalManager,
        acquire_lock,
        release_lock,
    ):
        self.workspace_uuid = workspace_uuid
        self.session_id = session_id
        self.agent = agent
        self.manager = manager
        self.acquire_lock = acquire_lock      # callable(key) -> bool，会话执行权
        self.release_lock = release_lock      # callable(key)
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
        # 用户轮次可能正在执行：有限重试等执行权，防"目标已设但 runner 直接退出"的空转态
        acquired = False
        for _ in range(_CLAIM_RETRIES):
            if self._stop.is_set():
                return
            if self.acquire_lock(key):
                acquired = True
                break
            time.sleep(_CLAIM_RETRY_INTERVAL)
        if not acquired:
            logger.warning(f"[goal] {_CLAIM_RETRIES}s 内未获得会话执行权（会话持续执行中）: {key}")
            return
        try:
            self._goal_loop()
        except Exception as e:
            logger.exception(f"[goal] 目标循环异常: {e}")
            try:
                self.manager.block("runner_error")
            except Exception:
                pass
        finally:
            try:
                self.release_lock(key)
            except Exception:
                pass
            _unregister_runner(key, self)

    def _goal_loop(self) -> None:
        m = self.manager
        agent = self.agent
        last_exec_id: str | None = None
        while m.is_active():
            if self._stop.is_set():
                break

            m.state.round += 1
            round_no = m.state.round
            if round_no > m.state.max_rounds:
                m.block("round_limit")
                self._round_finish(last_exec_id, "timeout",
                    f"⏱️ 已达轮次上限 {m.state.max_rounds} 轮，目标未完成，已停止。")
                break

            exec_id = f"{_ROUND_PREFIX}{round_no}"
            last_exec_id = exec_id
            prompt = m.next_round_prompt()

            # 建卡片：agent_start 先于一切轮次事件
            _publish(self.workspace_uuid, self.session_id, exec_id, {
                "type": "agent_start",
                "exec_id": exec_id,
                "task_summary": f"目标循环第 {round_no} 轮: {m.objective[:40]}",
            })

            # 上一轮若留了 ask_user 占位（agent 违反"不要询问"），注入自动决策
            self._resolve_pending_ask_user()

            # 执行本轮（interactive 循环，每轮重置迭代预算）
            try:
                agent.run(user_input=prompt, **_make_round_callbacks(self.workspace_uuid, self.session_id, exec_id))
            except Exception as e:
                logger.error(f"[goal] 第 {round_no} 轮执行异常: {e}")
                m.block("round_error")
                self._round_finish(exec_id, "failed", f"❌ 第 {round_no} 轮执行异常：{e}")
                break

            self._save_round_log(exec_id, prompt)

            final_text = self._last_assistant_text()
            completed = m.is_complete(final_text)
            m.set_last_summary(final_text[:_SUMMARY_MAX])
            m.save()

            if completed:
                m.mark_complete(summary=final_text[:_SUMMARY_MAX])
                self._round_finish(exec_id, "completed", f"🎯 目标已完成。\n\n{final_text[:_SUMMARY_MAX]}")
                break

            self._round_finish(exec_id, "completed", "本轮完成，目标尚未达成，继续下一轮。")
        # end while

    def _round_finish(self, exec_id: str | None, status: str, text: str) -> None:
        """收尾轮次卡片：先追加状态文本再发完成事件（exec_id 为空则跳过）。"""
        if not exec_id:
            return
        if text:
            _publish(self.workspace_uuid, self.session_id, exec_id, {"type": "text", "content": text})
        _publish(self.workspace_uuid, self.session_id, exec_id, {
            "type": "agent_complete", "exec_id": exec_id, "status": status,
        })

    # ── 辅助 ─────────────────────────────────────────────

    def _last_assistant_text(self) -> str:
        """取消息历史中最后一条 assistant 文本（用于完成检测与摘要衔接）。"""
        msgs = getattr(self.agent, "messages", None) or []
        for msg in reversed(msgs):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content if b.get("type") == "text" and b.get("text")]
                if texts:
                    return "\n".join(texts)
        return ""

    def _resolve_pending_ask_user(self) -> None:
        """目标模式禁止询问用户：自动答复遗留 ask_user 占位（含审批，视为拒绝）。"""
        sm = getattr(self.agent, "session_manager", None)
        if sm is None:
            return
        try:
            from web.routes_ask_user import _find_pending_ask_user, _inject_ask_user_answer
            pending_id = _find_pending_ask_user(sm)
            if pending_id:
                answer = "目标模式：禁止询问用户。请基于现有信息自行决策，继续执行。"
                _inject_ask_user_answer(self.agent, pending_id, answer)
        except Exception as e:
            logger.warning(f"[goal] 解析遗留 ask_user 占位失败: {e}")

    def _save_round_log(self, exec_id: str, prompt: str) -> None:
        """保存轮次消息到 agent_logs，前端卡片展开时全量重建。"""
        sm = getattr(self.agent, "session_manager", None)
        if sm is None or not hasattr(sm, "agent_logs"):
            return
        try:
            sm.agent_logs.save_agent_log(
                exec_id=exec_id,
                task=prompt,
                messages=self.agent.messages,
                metadata={
                    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "completed",
                    "iterations": 0,
                    "message_count": len(self.agent.messages),
                },
                summary="",
            )
        except Exception as e:
            logger.warning(f"[goal] 保存轮次日志失败: {e}")


# ── 每会话单例注册表 ────────────────────────────────────

_runners: dict[str, GoalRunner] = {}
_runners_lock = threading.Lock()
# 每个会话一个启动锁：串行化"停旧 runner + 启动新 runner"，防新旧双线程
# 同时操作同一会话（执行权认领无法排队）。
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
    acquire_lock,
    release_lock,
) -> GoalRunner | None:
    """创建并启动该会话的 GoalRunner。

    若已有旧 runner 在跑，先请求停止并等待其收尾（释放会话执行权），
    再启动新 runner。旧 runner 60s 内未收尾则放弃并返回 None（本轮仍在执行）。
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
        runner = GoalRunner(workspace_uuid, session_id, agent, manager, acquire_lock, release_lock)
        _register_runner(key, runner)
        runner.start()
        return runner


def stop_goal_runner(key: str) -> None:
    """请求停止运行中的 runner（轮间检查跳出，不中断进行中的单轮）。"""
    runner = get_runner(key)
    if runner is not None:
        runner.request_stop()
