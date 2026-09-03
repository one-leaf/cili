"""Cron scheduler - lightweight periodic task execution.

Runs tasks defined in core/cron/*.json using SubAgent.
Each task has a schedule (interval or cron), task description, and execution plan.

Usage:
    from core.cron import start_scheduler, stop_scheduler
    start_scheduler()  # starts background thread
    # ... application running ...
    stop_scheduler()   # clean shutdown
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT, DATA_DIR, AGENTS_DIR

logger = logging.getLogger(__name__)


def _next_cron_time(from_time: datetime, expr: str) -> datetime:
    """Calculate next run time from a 5-field cron expression.

    Format: minute hour day month weekday
    Supports: * (any), */N (every N), N-M (range), N,M (list)

    Weekday: 0=Sunday, 1=Monday, ..., 6=Saturday
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        logger.warning(f"[cron] Invalid cron expression: {expr}, fallback to 60 minutes")
        return from_time + timedelta(minutes=60)

    try:
        minute_spec, hour_spec, day_spec, month_spec, weekday_spec = parts

        # Parse each field
        minutes = _parse_cron_field(minute_spec, 0, 59)
        hours = _parse_cron_field(hour_spec, 0, 23)
        days = _parse_cron_field(day_spec, 1, 31)
        months = _parse_cron_field(month_spec, 1, 12)
        weekdays = _parse_cron_field(weekday_spec, 0, 6)  # 0=Sunday

        # Start searching from the next minute
        check_time = from_time.replace(second=0, microsecond=0) + timedelta(minutes=1)

        # Search up to 366 days ahead
        max_iterations = 366 * 24 * 60
        for _ in range(max_iterations):
            if (check_time.month in months and
                check_time.day in days and
                check_time.hour in hours and
                check_time.minute in minutes and
                # Convert Python weekday (0=Monday) to cron weekday (0=Sunday)
                (check_time.weekday() + 1) % 7 in weekdays):
                return check_time
            check_time += timedelta(minutes=1)

        # Fallback if no match found
        logger.warning(f"[cron] No valid time found for expression: {expr}, fallback to 60 minutes")
        return from_time + timedelta(minutes=60)

    except Exception as e:
        logger.warning(f"[cron] Failed to parse cron expression: {expr}, error: {e}")
        return from_time + timedelta(minutes=60)


def _parse_cron_field(field: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field into a set of valid values."""
    values = set()

    for part in field.split(","):
        if "/" in part:
            # */N or M/N
            range_part, step = part.split("/", 1)
            step = int(step)
            if range_part == "*":
                start, end = min_val, max_val
            elif "-" in range_part:
                start, end = map(int, range_part.split("-", 1))
            else:
                start, end = int(range_part), max_val
            values.update(range(start, end + 1, step))
        elif "-" in part:
            # M-N range
            start, end = map(int, part.split("-", 1))
            values.update(range(start, end + 1))
        elif part == "*":
            # Wildcard
            values.update(range(min_val, max_val + 1))
        else:
            # Single value
            values.add(int(part))

    return values


# Directory containing task JSON configs
CRON_DIR = Path(__file__).parent / "cron.d"

# Base directory for cron data
CRON_BASE_DIR = DATA_DIR / "cron.d"

# Directory for task runtime state (last_run, run_count, etc.)
CRON_STATE_DIR = CRON_BASE_DIR / "state"

# User tasks config file
USER_TASKS_FILE = CRON_BASE_DIR / "user_tasks.json"


class CronTask:
    """A single scheduled task."""

    def __init__(self, config: dict, task_fn=None):
        self.name: str = config["name"]
        self.description: str = config.get("description", "")
        self.enabled: bool = config.get("enabled", True)
        self.schedule: dict = config["schedule"]  # {"type": "interval", "minutes": 60}
        self.config: dict = config.get("config", {})  # Extra config (max_iterations, etc.)
        self.workspace_uuid: str = config.get("workspace_uuid", "")  # Empty = System workspace
        self.one_time: bool = config.get("one_time", False)  # Delete after first execution

        # Task ID for state file naming (use name if not provided)
        self.task_id: str = config.get("task_id", self.name)

        self._next_run: datetime | None = None
        self._last_run: datetime | None = None
        self._run_count: int = 0
        self._session_id: str = ""  # 关联的 session UUID
        self._remaining: int | None = None  # Remaining execution counter
        self._task_fn = task_fn  # Function that returns (task, plan) or (None, None) to skip

        # 从状态文件恢复 last_run
        self._restore_state()

    def _get_state_path(self) -> Path:
        """获取任务状态文件路径: data/cron.d/state/{task_id}.json"""
        CRON_STATE_DIR.mkdir(parents=True, exist_ok=True)
        return CRON_STATE_DIR / f"{self.task_id}.json"

    def _restore_state(self) -> None:
        """从状态文件恢复任务运行状态。"""
        state_path = self._get_state_path()
        if not state_path.exists():
            logger.debug(f"[cron] Task {self.name}: no state file, starting fresh")
            return

        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)

            self._session_id = state.get("session_id", "")
            self._remaining = state.get("remaining")  # May be None for old tasks
            saved_last_run = state.get("last_run")
            if saved_last_run:
                self._last_run = datetime.fromisoformat(saved_last_run)
                self._run_count = state.get("run_count", 0)
                # 根据 last_run 计算 next_run
                self._calculate_next_run(self._last_run)
                logger.debug(f"[cron] Task {self.name}: restored last_run={saved_last_run}, "
                            f"run_count={self._run_count}, next_run={self._next_run}, "
                            f"session_id={self._session_id}, remaining={self._remaining}")
        except Exception as e:
            logger.warning(f"[cron] Task {self.name}: failed to restore state: {e}")

    def _calculate_next_run(self, from_time: datetime) -> None:
        """根据 schedule 计算下次运行时间。"""
        schedule_type = self.schedule.get("type", "interval")
        if schedule_type == "interval":
            minutes = self.schedule.get("minutes", 60)
            self._next_run = from_time + timedelta(minutes=minutes)
        elif schedule_type == "cron":
            expr = self.schedule.get("expr", "")
            self._next_run = _next_cron_time(from_time, expr)
        else:
            # Fallback: run again in 60 minutes
            self._next_run = from_time + timedelta(minutes=60)

    def should_run(self, now: datetime) -> bool:
        """Check if this task should run now."""
        if not self.enabled:
            return False
        if self._next_run is None:
            return True  # First run (never run before and no saved last_run)
        return now >= self._next_run

    def get_tasks(self) -> list[dict]:
        """Get task list from task function. Returns [] if should skip.

        Returns:
            List of dicts: [{"task": "...", "plan": [...], "workspace_uuid": "..."}, ...]
        """
        if self._task_fn is None:
            return []
        try:
            result = self._task_fn()
            if not result:
                return []
            # 支持返回单个任务或任务列表
            if isinstance(result, dict):
                # 单个任务：{"task": "...", "plan": [...]}
                task = result.get("task", "")
                if not task:
                    return []
                return [{
                    "task": task,
                    "plan": result.get("plan", []),
                    "workspace_uuid": result.get("workspace_uuid", ""),
                }]
            elif isinstance(result, list):
                # 任务列表：[{"task": "...", "plan": [...]}, ...]
                tasks = []
                for item in result:
                    if isinstance(item, dict):
                        task = item.get("task", "")
                        if task:
                            tasks.append({
                                "task": task,
                                "plan": item.get("plan", []),
                                "workspace_uuid": item.get("workspace_uuid", ""),
                            })
                return tasks
            return []
        except Exception as e:
            logger.error(f"[cron] get_tasks() failed for {self.name}: {e}")
            return []

    def mark_executed(self, now: datetime, result: dict | None = None) -> None:
        """Update next run time after execution and persist state."""
        self._last_run = now
        self._run_count += 1
        self._calculate_next_run(now)

        # 持久化状态到独立的状态文件
        self._save_state(now, result)

    def _save_state(self, now: datetime, result: dict | None = None) -> None:
        """Save task state to state file for persistence across restarts.

        State file format (data/cron.d/state/{task_id}.json):
        {
            "last_run": "2026-08-25T22:40:32.128806",
            "run_count": 5,
            "session_id": "431ea12b",
            "remaining": 9994
        }
        """
        try:
            state_path = self._get_state_path()
            state = {
                "last_run": now.isoformat(),
                "run_count": self._run_count,
            }
            if self._session_id:
                state["session_id"] = self._session_id
            if self._remaining is not None:
                state["remaining"] = self._remaining

            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)

            logger.debug(f"[cron] Task {self.name}: saved state to {state_path}")
        except Exception as e:
            logger.warning(f"[cron] Task {self.name}: failed to save state: {e}")

    def execute(self) -> dict[str, Any]:
        """Execute all tasks through RootAgent. Returns result dict.

        Cron 的职责：创建/复用 session + 注入 user message。
        RootAgent 走正常 agent loop，自主决定是否委派 SubAgent。
        """
        # Get task list dynamically
        tasks = self.get_tasks()
        if not tasks:
            return {"status": "skipped", "message": "No tasks to execute", "iterations": 0}

        results = []

        logger.info(f"[cron] Starting {len(tasks)} task(s) for: {self.name}")

        for i, task_item in enumerate(tasks, 1):
            # Resolve target workspace: task_item > task-level > system
            target_ws = task_item.get("workspace_uuid") or self.workspace_uuid
            result = self._execute_in_session(target_ws, task_item)
            results.append({"task": i, **result})

        # 汇总结果
        all_success = all(r.get("status") == "completed" for r in results)
        return {
            "status": "completed" if all_success else "partial",
            "tasks_count": len(tasks),
            "results": results,
            "iterations": sum(r.get("iterations", 0) for r in results),
        }

    def _execute_in_session(self, workspace_uuid: str, task_item: dict) -> dict:
        """在 workspace 的 cron session 中通过 SubAgent 执行任务。

        Cron 采用 SubAgent 策略（模拟 RootAgent 调用 SubAgent 的消息格式）：
        1. 解析目标 workspace，复用或创建 cron session
        2. 添加 user message（任务描述）+ assistant message（tool_use 块）
        3. 创建 SubAgent 直接执行任务（非流式，自主运行）
        4. 添加 user message（tool_result 块，含 exec_id，UI 渲染卡片）
        5. 添加 assistant message（结果摘要）
        6. 保存 SubAgent 执行日志到 session 目录

        SubAgent 使用 context-bounded-processing 技能策略，
        适合后台自主执行的定时任务场景。
        """
        from core.sub_agent import SubAgent
        from core.session import SessionManager
        from core.config import load_config
        from datetime import datetime as dt

        # 1. 解析 workspace
        ws_uuid = workspace_uuid or "system"
        ws_dir = self._resolve_workspace_dir(ws_uuid)
        sessions_dir = Path(ws_dir) / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        # 2. 查找或创建 cron session
        cron_session_id = self._resolve_cron_session(sessions_dir)

        # 3. 加载 SessionManager
        session_mgr = SessionManager.load_session(cron_session_id, sessions_dir)
        if session_mgr is None:
            logger.error(f"[cron] Failed to load session {cron_session_id}")
            return {"status": "error", "error": "Session load failed", "workspace_uuid": ws_uuid}

        # 4. 构造任务信息
        task_desc = task_item.get("task", "")
        plan = task_item.get("plan", [])
        # 不主动限制 max_iterations，使用 SubAgent 的默认值（200）
        # 除非用户在任务配置中明确指定
        max_iterations = self.config.get("max_iterations", 200)

        # 5. 生成 exec_id 和 SubAgent 日志目录
        exec_id = session_mgr._generate_exec_id()
        exec_dir = session_mgr.session_dir / exec_id
        exec_dir.mkdir(parents=True, exist_ok=True)

        # 6. 添加 user message（任务描述）
        user_msg = self._build_cron_message(task_item)
        session_mgr.add_message("user", user_msg)

        # 7. 添加 assistant message（模拟 LLM 调用 subagent 的 tool_use 块）
        tool_use_id = f"cron_{exec_id}"
        task_brief = task_desc[:200]
        session_mgr.add_message("assistant", [
            {
                "type": "tool_use",
                "id": tool_use_id,
                "name": "subagent",
                "input": {"task": task_brief},
            }
        ])
        session_mgr.save()

        # 8. 创建并运行 SubAgent
        config = load_config()
        logger.info(f"[cron] [{self.name}] Starting SubAgent in session {cron_session_id}: {task_brief[:50]}...")

        subagent = SubAgent(
            task=task_desc,
            plan=plan,
            workspace_uuid=ws_uuid,
            cwd=ws_dir,
            max_iterations=max_iterations,
            session_dir=exec_dir,
            exec_id=exec_id,
        )

        try:
            result = subagent.run()
            status = result.get("status", "completed")
            summary = result.get("summary", "")
            iterations = result.get("iterations", 0)

            # 9. 添加 user message（tool_result 块，含 exec_id，UI 据此渲染 SubAgent 卡片）
            result_json = json.dumps(result, ensure_ascii=False, indent=2)
            session_mgr.add_message("user", [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_json,
                    "is_error": False,
                    "_meta": {
                        "tool_name": "subagent",
                        "exec_id": exec_id,
                        "completed": True,
                    },
                }
            ])

            # 10. 添加 assistant message（结果摘要，保证对话连续性）
            session_mgr.add_message("assistant", summary or f"SubAgent {status}")
            session_mgr.save()

            # 11. 更新 subagent_count
            session_mgr.metadata["subagent_count"] = session_mgr.metadata.get("subagent_count", 0) + 1

            # 12. 保存 SubAgent 执行日志
            ended_at = dt.now()
            started_at = subagent._started_at or ended_at
            session_mgr.save_subagent_log(
                exec_id=exec_id,
                task=task_desc,
                messages=subagent.messages,
                metadata={
                    "parent_session_id": "",
                    "session_id": exec_id,
                    "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "ended_at": ended_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "duration_seconds": (ended_at - started_at).total_seconds(),
                    "status": status,
                    "iterations": iterations,
                    "max_iterations": max_iterations,
                    "message_count": len(subagent.messages),
                },
                summary=summary,
            )

            # 转发 SubAgent usage 到 session
            sub_usage = result.get("usage", {})
            if sub_usage:
                session_mgr.update_usage(
                    input_tokens=sub_usage.get("input_tokens", 0),
                    output_tokens=sub_usage.get("output_tokens", 0),
                    api_calls=0,
                    cache_read_tokens=sub_usage.get("cache_read_tokens", 0),
                    cache_creation_tokens=sub_usage.get("cache_creation_tokens", 0),
                )
                session_mgr.save()

            logger.info(f"[cron] [{self.name}] SubAgent {status} in session {cron_session_id}")
            return {"status": "completed", "workspace_uuid": ws_uuid, "session_id": cron_session_id,
                    "iterations": iterations}

        except Exception as e:
            logger.error(f"[cron] [{self.name}] SubAgent failed: {e}")
            # 添加错误 tool_result（UI 仍可渲染卡片）
            error_result = {"status": "error", "summary": str(e), "iterations": 0}
            session_mgr.add_message("user", [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": json.dumps(error_result, ensure_ascii=False),
                    "is_error": True,
                    "_meta": {
                        "tool_name": "subagent",
                        "exec_id": exec_id,
                        "completed": True,
                    },
                }
            ])
            session_mgr.add_message("assistant", f"任务执行失败: {e}")
            session_mgr.save()
            return {"status": "error", "error": str(e), "workspace_uuid": ws_uuid}
        finally:
            subagent.close()

    def _resolve_workspace_dir(self, ws_uuid: str) -> str:
        """解析 workspace 目录。System workspace → data/"""
        if ws_uuid == "system":
            return str(DATA_DIR)
        return str(AGENTS_DIR / ws_uuid)

    def _resolve_cron_session(self, sessions_dir: Path) -> str:
        """查找或创建 cron session。

        优先使用 state 中保存的 session_id；如果 session 已不存在，创建新的。
        新建 session 名字为 "[Cron] 任务描述"。
        """
        from core.session import SessionManager

        # 1. 尝试使用已保存的 session_id
        if self._session_id:
            # 验证 session 是否还存在
            session_path = sessions_dir / self._session_id
            if session_path.exists() and session_path.is_dir():
                return self._session_id
            else:
                logger.info(f"[cron] Task {self.name}: saved session {self._session_id} no longer exists, creating new")
                self._session_id = ""

        # 2. 创建新 session，名字用任务描述
        session_name = f"[Cron] {self.description}" if self.description else f"[Cron] {self.name}"
        session_mgr = SessionManager.create_new_session(sessions_dir, name=session_name)
        self._session_id = session_mgr.session_id
        logger.info(f"[cron] Task {self.name}: created new session {self._session_id} ({session_name})")
        return self._session_id

    def _build_cron_message(self, task_item: dict) -> str:
        """构造 user message。"""
        task = task_item.get("task", "")
        plan = task_item.get("plan", [])

        lines = ["[Cron 定时任务触发]", ""]
        lines.append(f"## 任务描述\n{task}")

        if plan:
            lines.append("\n## 执行计划")
            for i, step in enumerate(plan, 1):
                lines.append(f"{i}. {step}")

        lines.append("\n请根据任务描述执行。")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialize task state for API response."""
        return {
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "workspace_uuid": self.workspace_uuid,
            "schedule": self.schedule,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "next_run": self._next_run.isoformat() if self._next_run else None,
            "run_count": self._run_count,
        }


class CronScheduler:
    """Lightweight cron scheduler that runs tasks in background thread."""

    def __init__(self):
        self.tasks: list[CronTask] = []
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._wake_event = threading.Event()  # Reusable for loop sleep (can be .set() to wake early)
        self._workspace_locks: dict[str, threading.Lock] = {}  # Per-workspace execution lock

    def _get_workspace_lock(self, workspace_uuid: str) -> threading.Lock:
        """Get or create a per-workspace lock for serializing cron tasks."""
        if workspace_uuid not in self._workspace_locks:
            self._workspace_locks[workspace_uuid] = threading.Lock()
        return self._workspace_locks[workspace_uuid]

    def load_tasks(self) -> int:
        """Load task configs from core/cron.d/*.json and data/cron/user_tasks.json. Returns count loaded."""
        self.tasks = []

        # Load system-level tasks from core/cron.d/
        if CRON_DIR.exists():
            for json_file in CRON_DIR.glob("*.json"):
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    task_fn = self._load_task_fn(json_file, config)
                    task = CronTask(config, task_fn=task_fn)
                    self.tasks.append(task)
                    logger.info(f"[cron] Loaded system task: {task.name}")
                except Exception as e:
                    logger.error(f"[cron] Failed to load {json_file}: {e}")
        else:
            logger.warning(f"[cron] System task directory not found: {CRON_DIR}")

        # Load user-level tasks from data/cili/cron.d/user_tasks.json
        if USER_TASKS_FILE.exists():
            try:
                with open(USER_TASKS_FILE, "r", encoding="utf-8") as f:
                    user_configs = json.load(f)
                for config in user_configs:
                    task_fn = self._load_user_task_fn(config)
                    if task_fn:
                        task = CronTask(config, task_fn=task_fn)
                        self.tasks.append(task)
                        logger.info(f"[cron] Loaded user task: {task.name}")
            except Exception as e:
                logger.error(f"[cron] Failed to load user tasks: {e}")

        return len(self.tasks)

    def _load_user_task_fn(self, config: dict):
        """Load task function from user task config (inline task/plan in 'content' field)."""
        content = config.get("content")
        if not content:
            logger.warning(f"[cron] User task {config.get('name')} has no 'content' field")
            return None

        # Capture workspace_uuid from config for fallback
        ws_uuid = config.get("workspace_uuid", "")

        # User tasks use inline task/plan
        if isinstance(content, dict):
            task = content.get("task", "")
            plan = content.get("plan", [])
            if not task:
                logger.warning(f"[cron] User task {config.get('name')} content.task is empty")
                return None
            logger.info(f"[cron] Loaded user inline task: {config.get('name')}")
            return lambda: [{"task": task, "plan": plan, "workspace_uuid": ws_uuid}]

        if isinstance(content, list):
            tasks = []
            for item in content:
                if isinstance(item, dict):
                    task = item.get("task", "")
                    if task:
                        tasks.append({
                            "task": task,
                            "plan": item.get("plan", []),
                            "workspace_uuid": ws_uuid,
                        })
            if not tasks:
                logger.warning(f"[cron] User task {config.get('name')} content list has no valid tasks")
                return None
            logger.info(f"[cron] Loaded {len(tasks)} user inline tasks from: {config.get('name')}")
            return lambda: tasks

        logger.error(f"[cron] Invalid content field type in user task {config.get('name')}")
        return None

    def _load_task_fn(self, json_path: Path, config: dict):
        """Load task from the content field. Can be a Python file path or inline dict/list."""
        content = config.get("content")
        if not content:
            logger.warning(f"[cron] {json_path.name} has no 'content' field")
            return None

        # Capture workspace_uuid from config
        ws_uuid = config.get("workspace_uuid", "")

        # Option 1: content is a string path to Python file
        if isinstance(content, str):
            py_path = json_path.parent / content
            if not py_path.exists():
                logger.error(f"[cron] Content file not found: {py_path}")
                return None

            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    f"cron_task_{json_path.stem}", py_path
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                fn = getattr(module, "get_tasks", None)
                if fn and callable(fn):
                    logger.info(f"[cron] Loaded task function from: {content}")
                    return fn
                else:
                    logger.warning(f"[cron] {content} missing get_tasks() function")
                    return None
            except Exception as e:
                logger.error(f"[cron] Failed to load task function from {py_path}: {e}")
                return None

        # Option 2: content is an inline dict with task/plan (single task)
        if isinstance(content, dict):
            task = content.get("task", "")
            plan = content.get("plan", [])
            if not task:
                logger.warning(f"[cron] {json_path.name} content.task is empty")
                return None
            # Return a lambda that provides the inline task as a list
            logger.info(f"[cron] Loaded inline task from: {json_path.name}")
            return lambda: [{"task": task, "plan": plan, "workspace_uuid": ws_uuid}]

        # Option 3: content is a list of tasks
        if isinstance(content, list):
            tasks = []
            for item in content:
                if isinstance(item, dict):
                    task = item.get("task", "")
                    if task:
                        tasks.append({
                            "task": task,
                            "plan": item.get("plan", []),
                            "workspace_uuid": ws_uuid,
                        })
            if not tasks:
                logger.warning(f"[cron] {json_path.name} content list has no valid tasks")
                return None
            logger.info(f"[cron] Loaded {len(tasks)} inline tasks from: {json_path.name}")
            return lambda: tasks

        logger.error(f"[cron] Invalid content field type in {json_path.name}")
        return None

    def start(self) -> None:
        """Start the scheduler in a background thread."""
        if self._running:
            return

        self.load_tasks()

        if not self.tasks:
            logger.info("[cron] No tasks to schedule")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="CronScheduler")
        self._thread.start()
        logger.info(f"[cron] Scheduler started with {len(self.tasks)} tasks")

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("[cron] Scheduler stopped")

    def get_task(self, name: str) -> CronTask | None:
        """Get a task by name."""
        for task in self.tasks:
            if task.name == name:
                return task
        return None

    def run_task_now(self, name: str) -> dict[str, Any] | None:
        """Manually trigger a task. Returns result or None if not found."""
        task = self.get_task(name)
        if not task:
            return None

        # Run in separate thread to not block
        def _run():
            now = datetime.now()
            result = task.execute()
            task.mark_executed(now, result)
            return result

        thread = threading.Thread(target=_run, daemon=True, name=f"CronTask-{name}")
        thread.start()
        return {"status": "triggered", "message": f"Task {name} triggered"}

    def _run_loop(self) -> None:
        """Main loop: check and execute due tasks."""
        while self._running:
            now = datetime.now()

            with self._lock:
                for task in self.tasks:
                    if task.should_run(now):
                        # Mark next_run as None immediately to prevent re-triggering
                        # if the task takes longer than the scheduling interval
                        task._next_run = None
                        thread = threading.Thread(
                            target=self._execute_task,
                            args=(task, now),
                            daemon=True,
                            name=f"CronTask-{task.name}",
                        )
                        thread.start()

            # Check every 60 seconds (or wake early if stop requested)
            self._wake_event.wait(60)
            self._wake_event.clear()

    def _execute_task(self, task: CronTask, now: datetime) -> None:
        """Execute a single task and update its state. Uses per-workspace lock."""
        ws_uuid = task.workspace_uuid or "system"
        ws_lock = self._get_workspace_lock(ws_uuid)

        if not ws_lock.acquire(blocking=False):
            logger.debug(f"[cron] Skip {task.name}: workspace {ws_uuid} locked (another task running)")
            return

        try:
            # Check and decrement remaining counter
            max_exec = task.config.get("max_executions", 9999)
            remaining = task._remaining if task._remaining is not None else max_exec

            remaining -= 1
            task._remaining = remaining

            # Check if should auto-disable
            if remaining <= 0:
                task.enabled = False
                logger.info(f"[cron] Task {task.name}: remaining=0, auto-disabled")
                # Save state with enabled=False
                task._save_state(now, None)
                # Update user_tasks.json to reflect disabled state
                self._update_task_enabled(task.name, False)
                return

            result = task.execute()
            task.mark_executed(now, result)

            # Log result
            if result.get("status") == "completed":
                logger.info(f"[cron] Task {task.name} completed successfully (remaining={remaining})")
            elif result.get("status") == "skipped":
                logger.info(f"[cron] Task {task.name} skipped: {result.get('message')}")
            else:
                logger.warning(f"[cron] Task {task.name} ended with: {result.get('status')}")

            # Auto-delete one-time tasks after execution
            if task.one_time:
                self._delete_one_time_task(task.name)
        except Exception as e:
            logger.error(f"[cron] Task {task.name} error: {e}")
        finally:
            ws_lock.release()

    def _update_task_enabled(self, name: str, enabled: bool) -> None:
        """Update task enabled state in user_tasks.json."""
        from core.tools.shared.cron_tool import _load_user_tasks, _save_user_tasks

        try:
            tasks = _load_user_tasks()
            for t in tasks:
                if t["name"] == name:
                    t["enabled"] = enabled
                    break
            _save_user_tasks(tasks)
        except Exception as e:
            logger.warning(f"[cron] Failed to update task enabled state: {e}")

    def _delete_one_time_task(self, name: str) -> None:
        """Remove a one-time task from config and state files."""
        # Import here to avoid circular dependency
        from core.tools.shared.cron_tool import _load_user_tasks, _save_user_tasks

        # Remove from user_tasks.json
        try:
            tasks = _load_user_tasks()
            original_count = len(tasks)
            tasks = [t for t in tasks if t["name"] != name]
            if len(tasks) < original_count:
                _save_user_tasks(tasks)
                logger.info(f"[cron] Deleted one-time task '{name}' after execution")
        except Exception as e:
            logger.error(f"[cron] Failed to delete one-time task '{name}' from config: {e}")

        # Remove state file
        try:
            state_path = CRON_STATE_DIR / f"{name}.json"
            if state_path.exists():
                state_path.unlink()
                logger.debug(f"[cron] Deleted state file for task '{name}'")
        except Exception as e:
            logger.warning(f"[cron] Failed to delete state file for task '{name}': {e}")

        # Remove from in-memory tasks list
        self.tasks = [t for t in self.tasks if t.name != name]

    def list_tasks(self) -> list[dict]:
        """List all tasks with their state."""
        return [task.to_dict() for task in self.tasks]


# Global singleton for the application
_scheduler: CronScheduler | None = None


def get_scheduler() -> CronScheduler:
    """Get or create the global cron scheduler."""
    global _scheduler
    if _scheduler is None:
        _scheduler = CronScheduler()
    return _scheduler


def start_scheduler() -> CronScheduler:
    """Start the global cron scheduler."""
    scheduler = get_scheduler()
    scheduler.start()
    return scheduler


def stop_scheduler() -> None:
    """Stop the global cron scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.stop()
        _scheduler = None
