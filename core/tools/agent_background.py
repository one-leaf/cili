"""后台子代理 Runner：全局活跃跟踪 + AgentTool 的 runner 方法。

从 ``background.py`` 拆出：只有 ``AgentTool`` 使用这些方法，不应挂在所有 Tool 上。
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from typing import Any

from core.agent_stats import AgentStats
from core.tools.background import (
    BackgroundTask,
    BackgroundTaskManager,
    owner_session_id,
)
from core.tools.result import ToolResult

logger = logging.getLogger(__name__)

# 全局跟踪所有活跃的后台 Runner，用于进程退出时清理 + 并发上限控制
_active_background_runners: list = []
_background_runners_cond = threading.Condition()
_atexit_registered = False
# 并发注册 atexit 的 check-then-set 需要锁（审查 P2）
_atexit_guard_lock = threading.Lock()


def _atexit_cleanup_runners() -> None:
    """进程退出时停止所有活跃的后台 Runner"""
    # 与 append/remove 共用 cond：否则可能遍历到正在被移除的列表
    with _background_runners_cond:
        runners = list(_active_background_runners)
        _active_background_runners.clear()
    for runner in runners:
        try:
            if hasattr(runner, 'stop'):
                runner.stop()
        except Exception:
            pass


def any_background_runner_running() -> bool:
    """是否存在运行中的后台子代理 Runner（升级自动重启的空闲判定）。

    只统计 session 型后台 Runner（LLM 子代理）：它们是进程内的线程与
    Runner 状态，重启即中断，代价高。shell 型后台命令是独立子进程，
    服务进程退出后仍可继续运行，只丢失输出捕获，不阻塞重启判定。
    """
    with _background_runners_cond:
        return bool(_active_background_runners)


class AgentBackgroundMixin:
    def _acquire_background_runner_slot(self, runner: Any) -> ToolResult | None:
        """等待后台 Runner 并发槽位并预留。

        超过 config.system.max_concurrent_agents（默认 2，范围 1-10）时阻塞等待，
        直到有子代理结束释放槽位、任务被停止、或等待超时（1 小时）。
        返回 None 表示获得槽位（runner 已加入活跃列表）；否则返回错误 ToolResult。
        """
        config = getattr(self, "config", None)
        system = getattr(config, "system", None)
        limit = getattr(system, "max_concurrent_agents", 2)
        try:
            limit = max(1, min(10, int(limit or 2)))
        except (TypeError, ValueError):
            limit = 2
        stop_check = getattr(self, "stop_check", None)
        deadline = time.time() + 3600
        with _background_runners_cond:
            while len(_active_background_runners) >= limit:
                if stop_check and stop_check():
                    return ToolResult(
                        f"Error: 后台 Runner 并发已达上限（{len(_active_background_runners)}/{limit}）"
                        "且任务已停止，本次委派未启动。",
                        error=True,
                    )
                remaining = deadline - time.time()
                if remaining <= 0:
                    return ToolResult(
                        f"Error: 等待后台 Runner 并发槽位超时（{len(_active_background_runners)}/{limit} 仍在运行）。"
                        "请稍后重试，或用 action=\"kill\" 终止占用任务。",
                        error=True,
                    )
                _background_runners_cond.wait(timeout=min(1.0, remaining))
            _active_background_runners.append(runner)
            return None

    def _start_background_runner(
        self,
        runner: Any,
        session: Any,
        exec_id: str,
        task_summary: str,
        on_complete: Any = None,
    ) -> ToolResult:
        """Start a Runner in background and return task_id.

        Args:
            runner: SessionRunner instance to run in background.
            session: Session reference.
            exec_id: Execution ID.
            task_summary: Task summary for display.
        """
        global _atexit_registered
        task_id = BackgroundTaskManager.allocate_task_id(prefix="agent")

        # Register atexit handler (once)——加锁防并发 check-then-set 竞态（审查 P2）
        with _atexit_guard_lock:
            if not _atexit_registered:
                atexit.register(_atexit_cleanup_runners)
                _atexit_registered = True

        # 并发上限控制：等待空余槽位（全局计数），并预留当前 runner 的槽位
        slot_error = self._acquire_background_runner_slot(runner)
        if slot_error is not None:
            return slot_error

        def run_runner():
            """Run Runner in background thread."""
            try:
                result = runner.run()
                task.result = result
                result_status = (result or {}).get("status", "completed")
                # handoff 是内部续任信号；无续任走到这里说明任务未完成
                if result_status == "handoff":
                    result_status = "timeout"
                task.status = result_status

                # 执行日志由 SessionRunner._finalize 单写；这里只保存主会话状态。
                if session and hasattr(session, "save"):
                    try:
                        session.save()
                    except Exception as e:
                        logger.warning(
                            f"Failed to save session after Runner {task_id}: {e}"
                        )

            except Exception as e:
                logger.error(f"Background Runner {task_id} error: {e}")
                task.status = "error"
                task.result = {"status": "error", "message": str(e)}
            finally:
                # 与 append / atexit 遍历共用同一把 cond 锁
                with _background_runners_cond:
                    try:
                        _active_background_runners.remove(runner)
                    except ValueError:
                        pass
                    _background_runners_cond.notify_all()
                # 注销子 runner 的 AgentMailbox 注册（exec_id + 所有别名如 label）
                try:
                    # 终态后关闭消息队列：只注销名字不清队列时，按 session_id
                    # （exec_id）send 仍会「成功」并把消息投进无人消费的队列。
                    from core.agent_mailbox import unregister_session_agents
                    unregister_session_agents(exec_id)
                except Exception as e:
                    logger.warning(
                        f"Failed to unregister background Runner from AgentMailbox {task_id}: {e}"
                    )
                # T18: 资源/统计对称 —— 后台子 runner 结束也 close LLM client 并转发 usage，
                # 与同步委派（agent_tool.py）保持一致。
                try:
                    runner.close()
                except Exception:
                    pass
                # 用量不在这里转发：runner 已把累计用量写进 exec 日志
                # metadata.usage，会话总量由读取端聚合。
                status = (task.result or {}).get("status", "completed")
                # handoff 是内部续任信号；无续任走到这里说明任务未完成
                if status == "handoff":
                    status = "timeout"
                defer_completion = False
                if on_complete is not None:
                    try:
                        defer_completion = bool(on_complete(task.result or {}))
                    except Exception as e:
                        logger.warning(
                            f'on_complete callback error {task_id}: {e}'
                        )
                if not defer_completion:
                    # 全局事件流：后台模式补发 session_complete + 保留 on_session_complete 回调
                    # + 通过 message_bus 发送通知给 master（使其在空闲时也能收到通知）
                    try:
                        publish = getattr(self, "_publish", None)
                        if publish:
                            stats = AgentStats.from_dict((task.result or {}).get("stats"))
                            publish("session_complete", exec_id=exec_id, status=status,
                                    stats=stats.to_dict())
                        # 通过 message_bus 发送通知给 master，使其在空闲时也能收到通知
                        sm = getattr(self, "session", None)
                        master_session_id = getattr(sm, "session_id", None) if sm else None
                        if master_session_id:
                            try:
                                from core.agent_mailbox import get_agent_mailbox
                                mbus = get_agent_mailbox()
                                summary_text = ""
                                if task.result:
                                    summary_text = (task.result.get("summary")
                                                    or task.result.get("message") or "")
                                mbus.send_to_agent(
                                    exec_id,
                                    master_session_id,
                                    f"[子 Runner 完成通知] exec_id={exec_id}, status={status}\nsummary: {summary_text[:200]}",
                                    message_type="notification",
                                )
                                # 发送特殊事件通知前端触发 master 继续（作为后端回调的兜底）。
                                # 用户主动停止的后台任务不唤醒会话：通知留在 mailbox，
                                # 下一次用户回合仍能看到「已停止」。
                                if not task.suppress_auto_resume:
                                    try:
                                        from core.event_bus import get_event_bus
                                        get_event_bus().publish({
                                            "type": "background_runner_resume",
                                            "workspace_uuid": self.workspace_uuid,
                                            "session_id": master_session_id,
                                            "exec_id": exec_id,
                                            "status": status,
                                        })
                                    except Exception:
                                        pass  # 事件发布失败不影响主流程
                            except Exception as e:
                                logger.debug(
                                    f"Failed to send notification via message_bus: {e}"
                                )
                        if getattr(self, "on_session_complete", None):
                            self.on_session_complete(exec_id, status)
                    except Exception as e:
                        logger.warning(
                            f"Failed to notify background Runner complete {task_id}: {e}"
                        )
                    # 后端直接触发 master 恢复循环（通知已在 message_bus 中，可以安全拾取）。
                    # 用户主动停止的任务不自动恢复；通知留待下一次用户回合。
                    if not task.suppress_auto_resume and getattr(self, "on_background_complete", None):
                        try:
                            self.on_background_complete(exec_id, status)
                        except Exception as e:
                            logger.warning(
                                f"on_background_complete callback error {task_id}: {e}"
                            )

        # Create background task entry
        task = BackgroundTask(
            task_id=task_id,
            command=task_summary,
            runner=runner,
            status="running",
            exec_id=exec_id,
            workspace_uuid=getattr(self, "workspace_uuid", ""),
            session_id=owner_session_id(session),
            kind="agent",
            label=task_summary,
        )

        BackgroundTaskManager.register(task)

        # 后台任务 ID（agent-N）也注册为邮箱别名：spawn 结果向模型暴露的是
        # Task ID，与邮箱里的 exec_id 是两套 ID，不注册别名则
        # send_to_agent("agent-N") 必然失败（见 message_bus 工具描述）。
        try:
            from core.agent_mailbox import register_agent_alias
            register_agent_alias(task_id, exec_id)
        except Exception as e:
            logger.warning(
                f"Failed to register mailbox alias {task_id} -> {exec_id}: {e}"
            )

        # Start Runner in background thread
        thread = threading.Thread(target=run_runner, daemon=True)
        thread.start()

        return ToolResult(
            f"Background Runner started.\n"
            f"Task ID: {task_id}\n"
            f"Task: {task_summary}\n\n"
            f"You will receive an automatic notification when it completes — do NOT poll with "
            f"action=\"read\" or sleep to wait. Continue your other work in the meantime.\n"
            f"Use action=\"kill\", task_id=\"{task_id}\" only if you need to terminate it early.",
            # 工具调用本身已完成；completed=False 是 ask_user/审批占位协议，
            # 后台任务仍在运行不能用它，否则主循环会误判为等待外部输入。
            meta={"exec_id": exec_id, "completed": True, "task_id": task_id,
                  "background": True, "background_running": True},
        )

    def _read_background_runner(self, task_id: str) -> ToolResult:
        """Read status and output from a background Runner."""
        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        if task.kind != "agent":
            return ToolResult(f"Error: task '{task_id}' is not a Runner", error=True)

        runner = task.runner
        if not runner:
            return ToolResult(f"Error: Runner for '{task_id}' not found", error=True)

        # Check status
        if task.result:
            # Completed
            result = task.result
            status = result.get("status", "unknown")
            summary = result.get("summary") or result.get("message") or ""
            stats = result.get("stats") if isinstance(result.get("stats"), dict) else {}
            iterations = int(stats.get("iterations", 0))

            # Clean up completed task
            BackgroundTaskManager.remove(task_id)

            meta = {"exec_id": task.exec_id, "completed": True, "stats": stats, "background": True}
            if summary:
                return ToolResult(
                    f"Runner {task_id} completed.\n"
                    f"Status: {status}\n"
                    f"Iterations: {iterations}\n\n"
                    f"Summary:\n{summary}",
                    meta=meta,
                )
            else:
                return ToolResult(
                    f"Runner {task_id} completed.\n"
                    f"Status: {status}\n"
                    f"Iterations: {iterations}",
                    meta=meta,
                )
        else:
            # Still running
            iterations = len(runner.messages) // 2  # Rough estimate
            return ToolResult(
                f"Runner {task_id} still running.\n"
                f"Estimated iterations: {iterations}\n"
                f"Current tool: {getattr(runner, '_current_tool', 'none')}",
                meta={"exec_id": task.exec_id, "completed": True,
                      "background": True, "background_running": True},
            )

    def _kill_background_runner(self, task_id: str) -> ToolResult:
        """Terminate a background Runner."""
        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        if task.kind != "agent":
            return ToolResult(f"Error: task '{task_id}' is not a Runner", error=True)

        runner = task.runner
        if not runner:
            return ToolResult(f"Error: Runner for '{task_id}' not found", error=True)

        try:
            # 只请求停止，不从注册表移除：线程需要自然收尾、写终态/归档 epoch，
            # 否则卡片会停在 running 且执行日志丢失。
            task.stop_requested = True
            task.suppress_auto_resume = True
            task.status = "stopping"
            runner._stopped = True
            publish = getattr(self, "_publish", None)
            if callable(publish):
                try:
                    publish("session_progress", exec_id=task.exec_id, status="running",
                            phase="stopping", current_tool="", stats={})
                except Exception:
                    pass

            return ToolResult(
                f"Runner {task_id} stop requested",
                meta={"exec_id": task.exec_id, "completed": True,
                      "status": "stopping", "background": True},
            )

        except Exception as e:
            return ToolResult(f"Error stopping Runner {task_id}: {e}", error=True)
