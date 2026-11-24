"""AGENTS.md 生成任务：后台 worker 扫描工程并写项目指令文件。

任务表持有线程与结果，保留最近 N 条已结束任务（否则每次生成都会累积）。
"""

from __future__ import annotations

import logging
import secrets
import threading

logger = logging.getLogger(__name__)

_GENERATE_TASKS: dict[str, dict] = {}
_LOCK = threading.Lock()
_MAX_FINISHED = 20

_TASK_PROMPT = (
    "Scan the project in the current working directory and generate an AGENTS.md file.\n\n"
    "First, use skill(action='read', skill_id='generate-sessions-md') to get the detailed instructions.\n"
    "Then follow the skill instructions to scan the project and generate AGENTS.md.\n\n"
    "Write the generated AGENTS.md to the workspace root directory using the write tool."
)


def _evict_finished_locked() -> None:
    """淘汰最旧的已结束任务。调用方须持 _LOCK。"""
    finished = [
        tid for tid, t in _GENERATE_TASKS.items()
        if t["result"].get("status") != "running"
    ]
    excess = len(finished) - _MAX_FINISHED
    # 负数切片会反向多删，必须加正数守卫
    if excess > 0:
        for tid in finished[:excess]:
            _GENERATE_TASKS.pop(tid, None)


def start_generation(workspace_uuid: str, workspace_dir: str) -> str:
    """启动后台生成任务，返回 exec_id。"""
    from core.config import load_config
    from core.session_runner import SessionRunner

    config = load_config()
    exec_id = f"gen-{secrets.token_hex(4)}"
    runner = SessionRunner(
        config=config,
        role="worker",
        task=_TASK_PROMPT,
        workspace_uuid=workspace_uuid,
        cwd=workspace_dir,
        exec_id=exec_id,
    )

    result_holder = {"status": "running", "summary": ""}

    def run_agent() -> None:
        try:
            result = runner.run()
            result_holder["status"] = result.get("status", "completed")
            result_holder["summary"] = result.get("summary", "")
        except Exception as e:  # noqa: BLE001
            result_holder["status"] = "error"
            result_holder["summary"] = str(e)

    thread = threading.Thread(target=run_agent, daemon=True)
    thread.start()

    with _LOCK:
        _GENERATE_TASKS[exec_id] = {
            "status": "running",
            "thread": thread,
            "result": result_holder,
            "workspace_dir": workspace_dir,
        }
        _evict_finished_locked()
    return exec_id


def generation_status(task_id: str) -> dict | None:
    """任务状态；未知 task_id 返回 None。"""
    with _LOCK:
        task = _GENERATE_TASKS.get(task_id)
    if not task:
        return None
    result = task["result"]
    return {
        "task_id": task_id,
        "status": result.get("status", "running"),
        "summary": result.get("summary", ""),
    }