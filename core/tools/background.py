"""后台任务管理：BackgroundTask/BackgroundTaskManager + 后台命令与后台 Runner mixin。

从 base.py 抽取的后台域：进程注册表、全局活跃 Runner 跟踪、并发槽位控制，
以及 Tool 的 10 个后台方法（以 BackgroundMixin 继承，经 MRO 解析到 Tool 实例）。
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from core.agent_stats import AgentStats
from core.tools.result import ToolResult
from core.tools.shell import _GIT_BASH_PATH, _PWSH_PATH, _VENV_DIR, _VENV_SCRIPTS

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


@dataclass
class BackgroundTask:
    """A background task (shell command or SessionRunner).

    Supports two task types:
    - "shell": subprocess.Popen (command execution)
    - "session": SessionRunner instance (autonomous runner loop)
    """
    task_id: str
    task_type: str = "shell"  # "shell" or "session"
    command: str = ""  # For shell tasks
    process: subprocess.Popen | None = None  # For shell tasks
    output_queue: queue.Queue[str | None] = field(default_factory=queue.Queue)
    status: str = "running"  # running, completed, killed, error
    exit_code: int | None = None
    created_at: float = field(default_factory=time.time)

    # Runner-specific fields
    runner: Any = None  # SessionRunner instance
    result: dict | None = None  # Runner execution result
    exec_id: str | None = None  # Runner execution ID for UI card rendering


class BackgroundTaskManager:
    """Manages background tasks (shell commands and Runners) across all tool instances.

    Thread-safe singleton that maintains a registry of background processes.
    """
    _tasks: dict[str, BackgroundTask] = {}
    _counter: int = 0
    _lock = threading.Lock()
    # 已完成任务的保留上限：read/kill 是唯一的移除路径，不设上限时完成的任务
    # （各自持有 runner 引用与输出缓冲）会随进程生命周期持续累积
    _MAX_COMPLETED_RETAINED = 50

    @classmethod
    def allocate_task_id(cls, prefix: str = "bg") -> str:
        """Allocate a unique task ID with prefix."""
        with cls._lock:
            cls._counter += 1
            return f"{prefix}-{cls._counter}"

    @classmethod
    def register(cls, task: BackgroundTask) -> None:
        """Register a background task."""
        with cls._lock:
            cls._tasks[task.task_id] = task
            cls._evict_completed_locked()

    @staticmethod
    def _is_finished(task: BackgroundTask) -> bool:
        """任务是否已结束（不依赖 list_tasks 的轮询刷新）。"""
        if task.status in ("completed", "killed", "error"):
            return True
        if task.task_type == "shell" and task.process is not None:
            return task.process.poll() is not None
        if task.task_type == "session":
            return task.result is not None
        return False

    @classmethod
    def _evict_completed_locked(cls) -> None:
        """淘汰最旧的已完成任务，只保留最近 _MAX_COMPLETED_RETAINED 条。

        调用方须持有 ``_lock``。
        """
        finished = [t for t in cls._tasks.values() if cls._is_finished(t)]
        excess = len(finished) - cls._MAX_COMPLETED_RETAINED
        if excess <= 0:
            return
        for task in sorted(finished, key=lambda t: t.created_at)[:excess]:
            cls._tasks.pop(task.task_id, None)

    @classmethod
    def get(cls, task_id: str) -> BackgroundTask | None:
        """Get a task by ID."""
        return cls._tasks.get(task_id)

    @classmethod
    def remove(cls, task_id: str) -> None:
        """Remove a task from registry."""
        with cls._lock:
            cls._tasks.pop(task_id, None)

    @classmethod
    def list_tasks(cls) -> list[dict[str, Any]]:
        """List all background tasks with their status."""
        with cls._lock:
            result = []
            for task_id, task in cls._tasks.items():
                # Check status
                if task.task_type == "shell" and task.process:
                    if task.process.poll() is not None:
                        task.status = "completed"
                        task.exit_code = task.process.returncode
                elif task.task_type == "session" and task.runner:
                    if not task.runner._running and task.status == "running":
                        task.status = "completed"

                result.append({
                    "task_id": task_id,
                    "task_type": task.task_type,
                    "command": task.command or (task.runner.task[:100] if task.runner else ""),
                    "status": task.status,
                    "exit_code": task.exit_code,
                    "created_at": task.created_at,
                })
            return result


class BackgroundMixin:
    """后台命令/Runner 方法族，经 Tool(ShellMixin, BackgroundMixin) MRO 挂到 Tool 实例。

    依赖宿主 Tool 提供的 self.cwd / self.output_file / self.on_output /
    self._emit_output / self._kill_process_tree（来自 ShellMixin）/ self.config /
    self.stop_check / self._publish / self.on_session_complete。
    """

    def _background_env(self) -> dict[str, str]:
        """后台子进程环境：与前台一致的 TEMP/TMP/TMPDIR/CILI_TMP + venv PATH。"""
        env = os.environ.copy()
        tmp_dir = self._workspace_tmp(self.workspace_uuid)
        for key in ("TEMP", "TMP", "TMPDIR", "CILI_TMP"):
            env[key] = tmp_dir
        env.update({
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
        })
        paths = [p for p in (_VENV_DIR, _VENV_SCRIPTS) if p]
        if paths:
            env["PATH"] = os.pathsep.join(paths + [env.get("PATH", "")])
        return env

    def _start_background_task(
        self,
        command: str,
        *,
        shell: str = "bash",
    ) -> ToolResult:
        """Start a command in background and return task_id.

        Args:
            command: Shell command to execute.
            shell: "bash"（默认）或 "pwsh"，决定命令前缀与 Popen argv。
        """
        task_id = BackgroundTaskManager.allocate_task_id()

        if shell == "pwsh":
            # UTF-8 encoding preamble
            encoding_preamble = (
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            )
            full_command = f"{encoding_preamble}{command}"
            argv = [_PWSH_PATH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", full_command]
        else:
            argv = [_GIT_BASH_PATH, "-c", command]

        # Determine output file
        output_file = self.output_file

        try:
            # Start process with stdin pipe for write_stdin support
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,  # Keep stdin open for write_stdin
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.cwd,
                env=self._background_env(),
            )

            # Create output queue and reader thread
            output_queue: queue.Queue[str | None] = queue.Queue()

            def reader_thread():
                # 复用单一句柄写输出文件，避免每行 open/close 的高开销（T23）
                f_out = None
                written_bytes = 0
                if output_file:
                    try:
                        f_out = open(output_file, "a", encoding="utf-8")
                        written_bytes = os.path.getsize(output_file)
                    except Exception:
                        f_out = None
                try:
                    for line in proc.stdout:
                        output_queue.put(line)
                        # Write to output file in real-time
                        if f_out:
                            try:
                                f_out.write(line)
                                f_out.flush()
                            except Exception:
                                pass
                        if self.on_output:
                            # 文本模式写 Windows 下 \n -> \r\n，按行内换行数补偿字节数
                            written_bytes += len(line.encode("utf-8", "replace")) + line.count("\n")
                            self._emit_output(line, written_bytes)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[bg reader] reader loop ended: {e}")
                finally:
                    if f_out:
                        try:
                            f_out.close()
                        except Exception:
                            pass
                    output_queue.put(None)  # Sentinel: EOF

            thread = threading.Thread(target=reader_thread, daemon=True)
            thread.start()

            # Register task
            task = BackgroundTask(
                task_id=task_id,
                command=command,
                process=proc,
                output_queue=output_queue,
            )

            BackgroundTaskManager.register(task)

            return ToolResult(
                f"Background task started.\n"
                f"Task ID: {task_id}\n"
                f"Command: {command}\n\n"
                f"Use action=\"read\", task_id=\"{task_id}\" to check output.\n"
                f"Use action=\"kill\", task_id=\"{task_id}\" to terminate."
            )

        except Exception as e:
            return ToolResult(f"Error starting background task: {e}", error=True)

    def _read_background_task(self, task_id: str) -> ToolResult:
        """Read accumulated output from a background task (non-blocking)."""
        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        # Drain queue (non-blocking)
        output_parts = []
        try:
            while True:
                line = task.output_queue.get_nowait()
                if line is None:
                    # EOF - process completed
                    break
                output_parts.append(line)
        except queue.Empty:
            pass

        # Session 型任务没有 process（审查 P1-21：误传 session 任务 ID 会崩溃）
        if task.process is None:
            return ToolResult(f"Error: task '{task_id}' is a session/agent task (no process)", error=True)

        # Check process status
        if task.process.poll() is not None:
            # Process completed
            task.status = "completed"
            task.exit_code = task.process.returncode

            # Drain any remaining output
            try:
                while True:
                    line = task.output_queue.get_nowait()
                    if line is None:
                        break
                    output_parts.append(line)
            except queue.Empty:
                pass

            # Clean up completed task
            BackgroundTaskManager.remove(task_id)

            output = "".join(output_parts).strip()
            if output:
                return ToolResult(
                    f"[Task {task_id} completed with exit code {task.exit_code}]\n{output}"
                )
            else:
                return ToolResult(
                    f"Task {task_id} completed with exit code {task.exit_code}"
                )

        # Still running
        output = "".join(output_parts).strip()
        if output:
            return ToolResult(f"[Task {task_id} still running]\n{output}")
        else:
            return ToolResult(f"Task {task_id} still running (no new output)")

    def _kill_background_task(self, task_id: str) -> ToolResult:
        """Terminate a background task."""
        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        if task.process is None:
            return ToolResult(f"Error: task '{task_id}' is a session/agent task (no process)", error=True)

        try:
            # Kill process tree
            self._kill_process_tree(task.process)
            task.process.wait(timeout=5)
            task.status = "killed"

            # Clean up
            BackgroundTaskManager.remove(task_id)

            return ToolResult(f"Task {task_id} terminated")

        except subprocess.TimeoutExpired:
            # Force kill
            try:
                self._kill_process_tree(task.process)
            except Exception:
                pass
            BackgroundTaskManager.remove(task_id)
            return ToolResult(f"Task {task_id} force terminated")

        except Exception as e:
            return ToolResult(f"Error killing task {task_id}: {e}", error=True)

    def _write_stdin_to_task(self, task_id: str | None, text: str) -> ToolResult:
        """Send input to a running background task's stdin."""
        if not task_id:
            return ToolResult("Error: task_id is required", error=True)

        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        # Session 型任务没有 process（审查 P1-22）
        if task.process is None:
            return ToolResult(f"Error: task '{task_id}' is a session/agent task (no process)", error=True)

        # Check if process is still running
        if task.process.poll() is not None:
            task.status = "completed"
            BackgroundTaskManager.remove(task_id)
            return ToolResult(
                f"Error: task '{task_id}' has already completed", error=True
            )

        try:
            # Write to stdin
            task.process.stdin.write(text)
            task.process.stdin.flush()
            return ToolResult(f"Sent input to task {task_id}")
        except Exception as e:
            return ToolResult(f"Error writing to task {task_id}: {e}", error=True)

    def _list_background_tasks(self) -> ToolResult:
        """List all background tasks with their status."""
        tasks = BackgroundTaskManager.list_tasks()
        if not tasks:
            return ToolResult("No background tasks")

        lines = ["Background tasks:"]
        for t in tasks:
            task_type = t.get("task_type", "shell")
            status = t["status"]
            if status == "completed" and t.get("exit_code") is not None:
                status += f" (exit {t['exit_code']})"
            lines.append(f"  {t['task_id']}: [{task_type}][{status}] {t['command']}")
        return ToolResult("\n".join(lines))

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
                task.status = "completed"

                # 执行日志由 SessionRunner._finalize 单写；这里只保存主会话状态。
                if session and hasattr(session, "save"):
                    try:
                        session.save()
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning(
                            f"Failed to save session after Runner {task_id}: {e}"
                        )

            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Background Runner {task_id} error: {e}")
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
                    from core.agent_mailbox import get_agent_mailbox
                    mbus = get_agent_mailbox()
                    for agent_name in mbus.get_session_agents(exec_id):
                        mbus.unregister_agent(agent_name)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"Failed to unregister background Runner from AgentMailbox {task_id}: {e}"
                    )
                # T18: 资源/统计对称 —— 后台子 runner 结束也 close LLM client 并转发 usage，
                # 与同步委派（agent_tool.py）保持一致。
                try:
                    runner.close()
                except Exception:
                    pass
                usage = (task.result or {}).get("usage", {})
                if session and usage:
                    try:
                        session.update_usage(
                            input_tokens=usage.get("input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0),
                            api_calls=0,
                            cache_read_tokens=usage.get("cache_read_tokens", 0),
                            cache_creation_tokens=usage.get("cache_creation_tokens", 0),
                        )
                        session.save()
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning(
                            f"Failed to forward usage for background Runner {task_id}: {e}"
                        )
                status = (task.result or {}).get("status", "completed")
                defer_completion = False
                if on_complete is not None:
                    try:
                        defer_completion = bool(on_complete(task.result or {}))
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning(
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
                                # 发送特殊事件通知前端触发 master 继续（作为后端回调的兜底）
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
                                import logging
                                logging.getLogger(__name__).debug(
                                    f"Failed to send notification via message_bus: {e}"
                                )
                        if getattr(self, "on_session_complete", None):
                            self.on_session_complete(exec_id)
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning(
                            f"Failed to notify background Runner complete {task_id}: {e}"
                        )
                    # 后端直接触发 master 恢复循环（通知已在 message_bus 中，可以安全拾取）
                    if getattr(self, "on_background_complete", None):
                        try:
                            self.on_background_complete(exec_id, status)
                        except Exception as e:
                            import logging
                            logging.getLogger(__name__).warning(
                                f"on_background_complete callback error {task_id}: {e}"
                            )

        # Create background task entry
        task = BackgroundTask(
            task_id=task_id,
            task_type="session",
            command=task_summary,
            runner=runner,
            status="running",
            exec_id=exec_id,
        )

        BackgroundTaskManager.register(task)

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
            meta={"exec_id": exec_id, "completed": False, "task_id": task_id, "background": True},
        )

    def _read_background_runner(self, task_id: str) -> ToolResult:
        """Read status and output from a background Runner."""
        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        if task.task_type != "session":
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
                meta={"exec_id": task.exec_id, "completed": False, "background": True},
            )

    def _kill_background_runner(self, task_id: str) -> ToolResult:
        """Terminate a background Runner."""
        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        if task.task_type != "session":
            return ToolResult(f"Error: task '{task_id}' is not a Runner", error=True)

        runner = task.runner
        if not runner:
            return ToolResult(f"Error: Runner for '{task_id}' not found", error=True)

        try:
            # Set stop flag
            runner._stopped = True
            task.status = "killed"

            # Clean up
            BackgroundTaskManager.remove(task_id)

            return ToolResult(
                f"Runner {task_id} terminated",
                meta={"exec_id": task.exec_id, "completed": True, "status": "killed", "background": True},
            )

        except Exception as e:
            return ToolResult(f"Error killing Runner {task_id}: {e}", error=True)
