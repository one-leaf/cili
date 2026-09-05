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

# Cron agent cache: workspace_uuid → RootAgent (reused across cron runs)
_cron_agents: dict[str, Any] = {}
_cron_agents_lock = threading.Lock()


class CronTask:
    """A single scheduled task."""

    def __init__(self, config: dict, task_fn=None):
        self.name: str = config["name"]
        self.description: str = config.get("description", "")
        self.enabled: bool = config.get("enabled", True)
        self.schedule: dict = config["schedule"]  # {"type": "interval", "minutes": 60}
        self.config: dict = config.get("config", {})  # Extra config (max_executions, etc.)
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
        """在 workspace 的 cron session 中通过 RootAgent 执行任务。

        Cron 采用 RootAgent 策略：
        1. 解析目标 workspace，复用或创建 cron session
        2. 获取或创建 RootAgent（按 workspace 缓存）
        3. 切换到 cron session，标记旧消息无效
        4. 注入 cron 任务消息，运行 agent loop（非流式）
        5. Agent 自主调用 subagent 工具执行任务，工具内部同步等待结果
        6. Agent loop 正常完成后返回
        """
        from core.root_agent import RootAgent
        from core.config import load_config, load_workspace_config

        # 1. 解析 workspace（数据目录用于 session）
        ws_uuid = workspace_uuid or "system"
        ws_data_dir = self._resolve_workspace_dir(ws_uuid)
        sessions_dir = Path(ws_data_dir) / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        # 2. 获取 workspace 的实际工作目录（用于 agent cwd）
        workspace_dir = ws_data_dir  # fallback
        if ws_uuid != "system":
            ws_config = load_workspace_config(ws_uuid)
            if ws_config:
                workspace_dir = ws_config.get("directory", ws_data_dir)

        # 3. 查找或创建 cron session
        cron_session_id = self._resolve_cron_session(sessions_dir)

        # 3. 构造任务信息
        task_desc = task_item.get("task", "")
        plan = task_item.get("plan", [])
        task_brief = task_desc[:200]

        logger.info(f"[cron] [{self.name}] Starting RootAgent in session {cron_session_id}: {task_brief[:50]}...")

        # 4. 获取或创建 RootAgent（cwd 是 workspace 实际工作目录，不是数据目录）
        agent = _get_or_create_root_agent(ws_uuid, workspace_dir)
        if agent is None:
            logger.warning(f"[cron] [{self.name}] RootAgent is busy for workspace {ws_uuid}, skipping")
            return {"status": "skipped", "message": "RootAgent is busy", "workspace_uuid": ws_uuid}

        try:
            # 5. 切换到 cron session（sessions_dir 在数据目录下，cwd 已正确指向 workspace）
            if agent.current_session_id != cron_session_id:
                agent.switch_session(cron_session_id)

            # 6. 标记旧消息无效（每次 cron 运行上下文干净）
            agent.invalidate_all_messages()
            agent.session_manager._messages_dirty = True

            # 7. 运行 agent loop（非流式，无回调）
            cron_message = self._build_cron_message(task_item)
            agent.run(cron_message, streaming=False)

            logger.info(f"[cron] [{self.name}] RootAgent completed in session {cron_session_id}")
            return {"status": "completed", "workspace_uuid": ws_uuid, "session_id": cron_session_id,
                    "iterations": 0}

        except Exception as e:
            logger.error(f"[cron] [{self.name}] RootAgent failed: {e}")
            return {"status": "error", "error": str(e), "workspace_uuid": ws_uuid}

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
        lines.append("请使用 subagent 工具执行以下任务：")
        lines.append(f"\n## 任务描述\n{task}")

        if plan:
            lines.append("\n## 执行计划")
            for i, step in enumerate(plan, 1):
                lines.append(f"{i}. {step}")

        lines.append("\n请根据任务描述和计划执行。")
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


def _get_or_create_root_agent(workspace_uuid: str, ws_dir: str):
    """获取或创建 workspace 对应的 RootAgent（cron 专用缓存）。

    - 如果 agent 存在且未在运行 → 复用
    - 如果 agent 不存在 → 创建新的并缓存
    - 如果 agent 正在运行 → 返回 None（跳过本次执行）
    """
    from core.root_agent import RootAgent
    from core.config import load_config

    with _cron_agents_lock:
        existing = _cron_agents.get(workspace_uuid)
        if existing is not None:
            if existing.is_running():
                return None  # 正在运行，跳过
            return existing

        # 创建新的 RootAgent
        try:
            config = load_config()
            agent = RootAgent(config, cwd=ws_dir, workspace_uuid=workspace_uuid)
            _cron_agents[workspace_uuid] = agent
            logger.info(f"[cron] Created RootAgent for workspace {workspace_uuid}")
            return agent
        except Exception as e:
            logger.error(f"[cron] Failed to create RootAgent for workspace {workspace_uuid}: {e}")
            return None


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
            # Update _next_run even on failure to prevent rapid re-triggering
            try:
                task.mark_executed(now, None)
            except Exception:
                pass
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
