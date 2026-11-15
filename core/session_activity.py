"""会话活动快照与后台停止（接口无关）。

P0 不引入完整 ExecutionRegistry：把 ``BackgroundTaskManager`` 当作后台执行
注册表，在它之上提供：

- ``snapshot``：会话后台任务快照（/status 用）；
- ``stop_turn``：只停 master 回合；
- ``stop_background``：停本会话全部后台执行（agent + shell）；
- ``stop_session``：内部使用（删除会话/重置工作区）的全停。

设计约束：
- 红方块只停 master，黄方块只停后台；两者都不得误伤对方；
- 停后台必须显式遍历所有活跃任务，不能依赖父级 stop 标志偶然传播；
- 被停任务 ``suppress_auto_resume=True``，完成后不发布 resume、不唤醒 master。
"""

from __future__ import annotations

import logging
from typing import Any

from core.event_bus import get_event_bus
from core.tools.background import BackgroundTask, BackgroundTaskManager
from core.tools.shell import ShellMixin

logger = logging.getLogger(__name__)


def snapshot(workspace_uuid: str, session_id: str, *, running: bool) -> dict[str, Any]:
    """会话活动快照：master 运行状态 + 后台任务摘要。"""
    background = BackgroundTaskManager.session_snapshot(workspace_uuid, session_id)
    return {
        "running": bool(running),
        "busy": bool(running) or background["active"] > 0,
        "background": background,
    }


def stop_turn(workspace_uuid: str, session_id: str) -> bool:
    """只停 master 回合；后台任务保留。"""
    from core.session_registry import registry, session_key

    runner = registry.get(session_key(workspace_uuid, session_id))
    if runner is None:
        return False
    is_running = getattr(runner, "is_running", None)
    if callable(is_running) and not is_running():
        return False
    stopper = getattr(runner, "stop", None)
    if not callable(stopper):
        return False
    stopper()
    return True


def _publish_stopping(task: BackgroundTask) -> None:
    """让前端卡片立即进入「停止中…」，不必等线程收尾。"""
    if not task.exec_id:
        return
    try:
        get_event_bus().publish({
            "type": "session_progress",
            "workspace_uuid": task.workspace_uuid,
            "session_id": task.session_id,
            "exec_id": task.exec_id,
            "status": "running",
            "phase": "stopping",
            "current_tool": "",
            "stats": {},
        })
    except Exception:  # noqa: BLE001 —— 展示失败不影响停止
        logger.debug("[session-activity] publish stopping failed", exc_info=True)


def _stop_shell(task: BackgroundTask) -> None:
    if task.stop_requested or task.status in ("killed", "completed", "error"):
        return
    task.stop_requested = True
    process = task.process
    if process is not None:
        try:
            ShellMixin._kill_process_tree(process)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[session-activity] kill shell {task.task_id} failed: {e}")
    task.status = "killed"


def _stop_agent(task: BackgroundTask) -> None:
    if task.stop_requested or task.result is not None:
        return
    task.stop_requested = True
    task.suppress_auto_resume = True
    task.status = "stopping"
    runner = task.runner
    stopper = getattr(runner, "stop", None)
    if callable(stopper):
        try:
            stopper()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[session-activity] stop runner {task.task_id} failed: {e}")
    _publish_stopping(task)


def stop_background(workspace_uuid: str, session_id: str) -> int:
    """停本会话全部活跃后台执行（agent + shell + 其他 task_type）。

    返回请求停止的任务数。shell 立即杀进程树；agent 置停止标志后由线程
    自然收尾并写终态。
    """
    tasks = BackgroundTaskManager.session_tasks(workspace_uuid, session_id, active_only=True)
    for task in tasks:
        if task.task_type == "session":
            _stop_agent(task)
        else:
            _stop_shell(task)
    # 幂等：已处于 stopping 的任务仍计入，重复点击黄态也返回成功；
    # 任务真正收尾后才会从 active 快照消失。
    return len(tasks)


def stop_session(workspace_uuid: str, session_id: str) -> dict[str, Any]:
    """内部全停：master 回合 + 全部后台。删除会话/重置工作区使用。"""
    turn = stop_turn(workspace_uuid, session_id)
    background = stop_background(workspace_uuid, session_id)
    return {"turn": turn, "background": background}
