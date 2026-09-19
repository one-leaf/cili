"""后台任务管理：BackgroundTask/BackgroundTaskManager + 后台命令与后台 Agent mixin。

从 base.py 抽取的后台域：进程注册表、全局活跃 Agent 跟踪、并发槽位控制，
以及 Tool 的 10 个后台方法（以 BackgroundMixin 继承，经 MRO 解析到 Tool 实例）。
"""

from __future__ import annotations

import atexit
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from core.tools.result import ToolResult
from core.tools.shell import _GIT_BASH_PATH, _PWSH_PATH


# 全局跟踪所有活跃的后台 Agent，用于进程退出时清理 + 并发上限控制
_active_background_agents: list = []
_background_agents_cond = threading.Condition()
_atexit_registered = False


def _atexit_cleanup_agents() -> None:
    """进程退出时停止所有活跃的后台 Agent"""
    for agent in list(_active_background_agents):
        try:
            if hasattr(agent, 'stop'):
                agent.stop()
        except Exception:
            pass
    _active_background_agents.clear()


@dataclass
class BackgroundTask:
    """A background task (shell command or Agent).

    Supports two task types:
    - "shell": subprocess.Popen (command execution)
    - "agent": Agent instance (autonomous agent loop)
    """
    task_id: str
    task_type: str = "shell"  # "shell" or "agent"
    command: str = ""  # For shell tasks
    process: subprocess.Popen | None = None  # For shell tasks
    output_file: str | None = None
    output_queue: queue.Queue[str | None] = field(default_factory=queue.Queue)
    reader_thread: threading.Thread | None = None
    status: str = "running"  # running, completed, killed, error
    exit_code: int | None = None
    created_at: float = field(default_factory=time.time)
    stdin_pipe: Any = None  # subprocess.PIPE for write_stdin

    # Agent-specific fields
    agent: Any = None  # Agent instance
    session_manager: Any = None  # SessionManager reference
    result: dict | None = None  # Agent execution result


class BackgroundTaskManager:
    """Manages background tasks (shell commands and Agents) across all tool instances.

    Thread-safe singleton that maintains a registry of background processes.
    """
    _tasks: dict[str, BackgroundTask] = {}
    _counter: int = 0
    _lock = threading.Lock()

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
                elif task.task_type == "agent" and task.agent:
                    if not task.agent._running and task.status == "running":
                        task.status = "completed"

                result.append({
                    "task_id": task_id,
                    "task_type": task.task_type,
                    "command": task.command or (task.agent.task[:100] if task.agent else ""),
                    "status": task.status,
                    "exit_code": task.exit_code,
                    "created_at": task.created_at,
                })
            return result


class BackgroundMixin:
    """后台命令/Agent 方法族，经 Tool(ShellMixin, BackgroundMixin) MRO 挂到 Tool 实例。

    依赖宿主 Tool 提供的 self.cwd / self.output_file / self.on_output /
    self._emit_output / self._kill_process_tree（来自 ShellMixin）/ self.config /
    self.stop_check / self._publish / self.on_agent_complete。
    """

    def _start_background_task(
        self,
        command: str,
        shell_path: str | None = None,
        env_prefix: str = "",
        *,
        shell: str = "bash",
    ) -> ToolResult:
        """Start a command in background and return task_id.

        _start_pwsh_background_task() 的共享实现。命令前缀构造与 Popen argv 因 shell 而异，
        其余（reader 线程、文件流式、注册）共用一份。

        Args:
            command: Shell command to execute.
            shell_path: Path to shell executable. Defaults to Git Bash.
            env_prefix: Environment setup prefix (e.g., PATH export).
            shell: "bash"（默认）或 "pwsh"，决定命令前缀与 Popen argv。
        """
        task_id = BackgroundTaskManager.allocate_task_id()

        if shell == "pwsh":
            # Build full command with environment prefix
            if env_prefix:
                full_command = f"{env_prefix}; {command}"
            else:
                full_command = command

            # UTF-8 encoding preamble
            encoding_preamble = (
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            )
            full_command = f"{encoding_preamble}{full_command}"
            argv = [_PWSH_PATH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", full_command]
        else:
            # Determine shell
            if shell_path is None:
                shell_path = _GIT_BASH_PATH

            # Build full command with environment prefix
            if env_prefix:
                full_command = f"{env_prefix} && {command}"
            else:
                full_command = command
            argv = [shell_path, "-c", full_command]

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
                except Exception:
                    pass
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
                output_file=output_file,
                output_queue=output_queue,
                reader_thread=thread,
                stdin_pipe=proc.stdin,
            )

            BackgroundTaskManager.register(task)

            return ToolResult(
                f"Background task started.\n"
                f"Task ID: {task_id}\n"
                f"Command: {command}\n\n"
                f"Use read_task(\"{task_id}\") to check output.\n"
                f"Use kill_task(\"{task_id}\") to terminate."
            )

        except Exception as e:
            return ToolResult(f"Error starting background task: {e}", error=True)

    def _start_pwsh_background_task(self, command: str, env_prefix: str = "") -> ToolResult:
        """Start a PowerShell command in background and return task_id.

        Mirrors _start_background_task() but uses pwsh invocation.
        (delegates to _start_background_task)
        """
        return self._start_background_task(command, env_prefix=env_prefix, shell="pwsh")

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

    def _acquire_background_agent_slot(self, agent: Any) -> ToolResult | None:
        """等待后台 Agent 并发槽位并预留。

        超过 config.system.max_concurrent_agents（默认 2，范围 1-10）时阻塞等待，
        直到有子代理结束释放槽位、任务被停止、或等待超时（1 小时）。
        返回 None 表示获得槽位（agent 已加入活跃列表）；否则返回错误 ToolResult。
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
        with _background_agents_cond:
            while len(_active_background_agents) >= limit:
                if stop_check and stop_check():
                    return ToolResult(
                        f"Error: 后台 Agent 并发已达上限（{len(_active_background_agents)}/{limit}）"
                        "且任务已停止，本次委派未启动。",
                        error=True,
                    )
                remaining = deadline - time.time()
                if remaining <= 0:
                    return ToolResult(
                        f"Error: 等待后台 Agent 并发槽位超时（{len(_active_background_agents)}/{limit} 仍在运行）。"
                        "请稍后重试，或用 kill_task 终止占用任务。",
                        error=True,
                    )
                _background_agents_cond.wait(timeout=min(1.0, remaining))
            _active_background_agents.append(agent)
            return None

    def _start_background_agent(
        self,
        agent: Any,
        session_manager: Any,
        exec_id: str,
        task_summary: str,
    ) -> ToolResult:
        """Start a Agent in background and return task_id.

        Args:
            agent: Agent instance to run in background.
            session_manager: SessionManager reference.
            exec_id: Execution ID.
            task_summary: Task summary for display.
        """
        global _atexit_registered
        task_id = BackgroundTaskManager.allocate_task_id(prefix="agent")

        # Register atexit handler (once)
        if not _atexit_registered:
            atexit.register(_atexit_cleanup_agents)
            _atexit_registered = True

        # 并发上限控制：等待空余槽位（全局计数），并预留当前 agent 的槽位
        slot_error = self._acquire_background_agent_slot(agent)
        if slot_error is not None:
            return slot_error

        def run_agent():
            """Run Agent in background thread."""
            try:
                result = agent.run()
                task.result = result
                task.status = "completed"

                # Save Agent log
                if session_manager and exec_id:
                    from datetime import datetime
                    try:
                        session_manager.agent_logs.save_agent_log(
                            exec_id=exec_id,
                            task=agent.task,
                            messages=agent.messages,
                            metadata={
                                "started_at": agent._started_at.strftime("%Y-%m-%d %H:%M:%S") if agent._started_at else "",
                                "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "duration_seconds": agent._elapsed_seconds(),
                                "status": result.get("status", "completed"),
                                "iterations": result.get("iterations", 0),
                                "message_count": len(agent.messages),
                                "max_iterations": agent.max_iterations,
                            },
                            summary=result.get("summary") or result.get("message") or "",
                        )
                        session_manager.save()
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning(
                            f"Failed to save Agent log for {task_id}: {e}"
                        )

            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Background Agent {task_id} error: {e}")
                task.status = "error"
                task.result = {"status": "error", "message": str(e)}
            finally:
                try:
                    _active_background_agents.remove(agent)
                except ValueError:
                    pass
                with _background_agents_cond:
                    _background_agents_cond.notify_all()
                # T18: 资源/统计对称 —— 后台子代理结束也 close LLM client 并转发 usage，
                # 与同步委派（agent_tool.py）保持一致。
                try:
                    agent.close()
                except Exception:
                    pass
                usage = (task.result or {}).get("usage", {})
                if session_manager and usage:
                    try:
                        session_manager.update_usage(
                            input_tokens=usage.get("input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0),
                            api_calls=0,
                            cache_read_tokens=usage.get("cache_read_tokens", 0),
                            cache_creation_tokens=usage.get("cache_creation_tokens", 0),
                        )
                        session_manager.save()
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning(
                            f"Failed to forward usage for background Agent {task_id}: {e}"
                        )
                # 全局事件流：后台模式补发 agent_complete（修复原先缺失）+ 保留 on_agent_complete 回调
                # + 写入 master 通知队列（使主循环下一轮迭代自动感知，无需 LLM 轮询 read_task）
                try:
                    status = (task.result or {}).get("status", "completed")
                    publish = getattr(self, "_publish", None)
                    if publish:
                        publish("agent_complete", exec_id=exec_id, status=status)
                    # 写入 master 通知队列
                    queue = getattr(self, "_notification_queue", None)
                    if queue is not None:
                        summary = ""
                        if task.result:
                            summary = (task.result.get("summary")
                                       or task.result.get("message") or "")
                        queue.append({
                            "exec_id": exec_id,
                            "status": status,
                            "summary": summary[:200],
                        })
                    if getattr(self, "on_agent_complete", None):
                        self.on_agent_complete(exec_id)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"Failed to notify background Agent complete {task_id}: {e}"
                    )

        # Create background task entry
        task = BackgroundTask(
            task_id=task_id,
            task_type="agent",
            command=task_summary,
            agent=agent,
            session_manager=session_manager,
            status="running",
        )

        BackgroundTaskManager.register(task)

        # Start Agent in background thread
        thread = threading.Thread(target=run_agent, daemon=True)
        thread.start()
        task.reader_thread = thread

        return ToolResult(
            f"Background Agent started.\n"
            f"Task ID: {task_id}\n"
            f"Task: {task_summary}\n\n"
            f"Use read_task(\"{task_id}\") to check status.\n"
            f"Use kill_task(\"{task_id}\") to terminate."
        )

    def _read_background_agent(self, task_id: str) -> ToolResult:
        """Read status and output from a background Agent."""
        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        if task.task_type != "agent":
            return ToolResult(f"Error: task '{task_id}' is not a Agent", error=True)

        agent = task.agent
        if not agent:
            return ToolResult(f"Error: Agent for '{task_id}' not found", error=True)

        # Check status
        if task.result:
            # Completed
            result = task.result
            status = result.get("status", "unknown")
            summary = result.get("summary") or result.get("message") or ""
            iterations = result.get("iterations", 0)

            # Clean up completed task
            BackgroundTaskManager.remove(task_id)

            if summary:
                return ToolResult(
                    f"Agent {task_id} completed.\n"
                    f"Status: {status}\n"
                    f"Iterations: {iterations}\n\n"
                    f"Summary:\n{summary}"
                )
            else:
                return ToolResult(
                    f"Agent {task_id} completed.\n"
                    f"Status: {status}\n"
                    f"Iterations: {iterations}"
                )
        else:
            # Still running
            iterations = len(agent.messages) // 2  # Rough estimate
            return ToolResult(
                f"Agent {task_id} still running.\n"
                f"Estimated iterations: {iterations}\n"
                f"Current tool: {getattr(agent, '_current_tool', 'none')}"
            )

    def _kill_background_agent(self, task_id: str) -> ToolResult:
        """Terminate a background Agent."""
        task = BackgroundTaskManager.get(task_id)
        if not task:
            return ToolResult(f"Error: task '{task_id}' not found", error=True)

        if task.task_type != "agent":
            return ToolResult(f"Error: task '{task_id}' is not a Agent", error=True)

        agent = task.agent
        if not agent:
            return ToolResult(f"Error: Agent for '{task_id}' not found", error=True)

        try:
            # Set stop flag
            agent._stopped = True
            task.status = "killed"

            # Clean up
            BackgroundTaskManager.remove(task_id)

            return ToolResult(f"Agent {task_id} terminated")

        except Exception as e:
            return ToolResult(f"Error killing Agent {task_id}: {e}", error=True)
