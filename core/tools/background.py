"""后台 shell 任务：BackgroundTask/BackgroundTaskManager + 后台命令 mixin。

进程注册表与 shell 后台方法（以 BackgroundMixin 继承到 Tool）。
后台子代理 Runner 的方法在 ``core.tools.agent_background``。
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from core.tools.result import ToolResult
from core.tools.shell import _GIT_BASH_PATH, _PWSH_PATH, _VENV_DIR, _VENV_SCRIPTS

logger = logging.getLogger(__name__)


def owner_session_id(session: Any) -> str:
    """后台任务归属的根会话 id。

    master 的 SessionStore 只有 ``session_id``；worker/lite 的 SessionIdentity
    另外带 ``master_session_id``。嵌套子代理（及其后台 shell）必须归到根会话，
    否则主会话的 /status 聚合不到。
    """
    if session is None:
        return ""
    return (
        getattr(session, "master_session_id", "")
        or getattr(session, "session_id", "")
        or ""
    )


@dataclass
class BackgroundTask:
    """A background task (shell command or SessionRunner).

    Supports two task types:
    - "shell": subprocess.Popen (command execution)
    - "session": SessionRunner instance (autonomous runner loop)
    """
    task_id: str
    command: str = ""  # For shell tasks
    process: subprocess.Popen | None = None  # For shell tasks
    output_queue: queue.Queue[str | None] = field(default_factory=queue.Queue)
    status: str = "running"  # running, stopping, completed, killed, stopped, error
    exit_code: int | None = None
    created_at: float = field(default_factory=time.time)

    # 会话归属（/status 聚合与后台停止按 workspace+session 过滤）
    workspace_uuid: str = ""
    session_id: str = ""  # root master session id
    kind: str = ""  # "agent" | "shell"
    label: str = ""

    # 停止意图：UI 停后台后置位；run_runner 收尾时据此抑制自动恢复
    stop_requested: bool = False
    suppress_auto_resume: bool = False

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
        if task.status in ("completed", "killed", "stopped", "error"):
            return True
        if task.kind == "shell" and task.process is not None:
            return task.process.poll() is not None
        if task.kind == "agent":
            return task.result is not None
        return False

    @classmethod
    def is_active(cls, task: BackgroundTask) -> bool:
        """任务是否仍占用后台槽位（running / stopping）。

        不修改任务状态；供 /status 与 stop_background 做只读快照。
        """
        if task.status in ("completed", "killed", "stopped", "error"):
            return False
        if task.kind == "shell" and task.process is not None:
            return task.process.poll() is None
        if task.kind == "agent":
            return task.result is None
        return task.status in ("running", "stopping")

    @classmethod
    def session_tasks(cls, workspace_uuid: str, session_id: str,
                      *, active_only: bool = True) -> list[BackgroundTask]:
        """按 workspace + 根会话过滤后台任务（线程安全快照）。"""
        with cls._lock:
            tasks = [
                task for task in cls._tasks.values()
                if task.workspace_uuid == workspace_uuid
                and task.session_id == session_id
            ]
        if active_only:
            tasks = [task for task in tasks if cls.is_active(task)]
        return tasks

    @classmethod
    def session_snapshot(cls, workspace_uuid: str, session_id: str) -> dict[str, Any]:
        """会话后台任务快照：计数 + 活跃项。"""
        tasks = cls.session_tasks(workspace_uuid, session_id, active_only=True)
        items = [
            {
                "task_id": task.task_id,
                "exec_id": task.exec_id,
                "kind": task.kind or "shell",
                "label": task.label or task.command,
                "status": task.status,
                "created_at": task.created_at,
            }
            for task in sorted(tasks, key=lambda t: t.created_at)
        ]
        agents = sum(1 for task in tasks if task.kind == "agent")
        shells = len(tasks) - agents
        return {
            "agents": agents,
            "shells": shells,
            "active": len(tasks),
            "items": items,
        }

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
                if task.kind == "shell" and task.process:
                    if task.process.poll() is not None and task.status not in ("killed", "stopped"):
                        task.status = "completed"
                        task.exit_code = task.process.returncode
                elif task.kind == "agent" and task.result is not None:
                    # 终态以 run() 的结果为准：stopped 不能被误写成 completed
                    result_status = task.result.get("status") or "completed"
                    task.status = "timeout" if result_status == "handoff" else result_status

                result.append({
                    "task_id": task_id,
                    "kind": task.kind,
                    "command": task.command or (task.runner.task[:100] if task.runner else ""),
                    "status": task.status,
                    "exit_code": task.exit_code,
                    "created_at": task.created_at,
                })
            return result


class BackgroundMixin:
    """后台 shell 命令方法族，经 Tool(ShellMixin, BackgroundMixin) MRO 挂到 Tool 实例。

    依赖宿主 Tool 提供的 self.cwd / self.output_file / self.on_output /
    self._emit_output / self._kill_process_tree（来自 ShellMixin）。
    后台子代理 Runner 方法在 ``core.tools.agent_background.AgentBackgroundMixin``。
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
        env["PATH"] = os.pathsep.join([_VENV_DIR, _VENV_SCRIPTS, env.get("PATH", "")])
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
                workspace_uuid=getattr(self, "workspace_uuid", ""),
                session_id=owner_session_id(getattr(self, "session", None)),
                kind="shell",
                label=command[:100],
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
            # Kill process tree；保留任务记录供终态/审计，活跃快照按状态过滤。
            self._kill_process_tree(task.process)
            task.process.wait(timeout=5)
            task.status = "killed"
            task.stop_requested = True
            return ToolResult(f"Task {task_id} terminated")

        except subprocess.TimeoutExpired:
            # Force kill
            try:
                self._kill_process_tree(task.process)
            except Exception:
                pass
            task.status = "killed"
            task.stop_requested = True
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
            kind_label = t.get("kind", "shell")
            status = t["status"]
            if status == "completed" and t.get("exit_code") is not None:
                status += f" (exit {t['exit_code']})"
            lines.append(f"  {t['task_id']}: [{kind_label}][{status}] {t['command']}")
        return ToolResult("\n".join(lines))
