"""回合后钩子：记忆提取与工作区 git 自动提交（均后台线程，不阻塞）。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注，避免运行期循环导入
    from core.session_runner import SessionRunner

logger = logging.getLogger(__name__)


def schedule_after_turn_hooks(runner: SessionRunner) -> None:
    """回合后的收尾钩子：记忆提取 + git 自动提交（均后台线程，不阻塞）。"""
    _schedule_memory_extraction(runner)
    _schedule_git_commit(runner)


def _schedule_memory_extraction(runner: SessionRunner) -> None:
    """v3 记忆：回合结束后后台提取（不阻塞；失败只记日志）。"""
    try:
        sm = getattr(runner, "session", None)
        if sm is None:
            return
        from core.memory_service import memory_enabled, schedule_extraction
        if memory_enabled(runner.workspace_uuid or ""):
            schedule_extraction(
                runner.workspace_uuid or "",
                runner.current_session_id or "",
                list(sm.messages),
            )
    except Exception:
        logger.exception("Failed to schedule memory extraction")


def _schedule_git_commit(runner: SessionRunner) -> None:
    """Git 版本管理：回合结束后自动提交并同步远程（后台线程，不阻塞）。"""
    try:
        from core.config import get_workspace_meta
        workspace_cfg = get_workspace_meta(runner.workspace_uuid or "")
        if not workspace_cfg.get("git_enabled", False):
            return
        import threading

        from core.workspace_git import auto_commit_workspace
        threading.Thread(
            target=auto_commit_workspace,
            args=(runner.workspace_uuid, runner.current_session_id or ""),
            kwargs={"sync_remote": True},
            daemon=True,
        ).start()
    except Exception:
        logger.exception("Failed to schedule git auto-commit")
