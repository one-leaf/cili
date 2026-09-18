"""GoalManager — master agent 目标驱动循环的状态机（纯逻辑，可单测）。

/goal 的本质是轮次制目标循环：每轮 = 一次 agent.run(goal_round_msg)，
一轮结束（自然完成或迭代预算耗尽）后由 GoalRunner 检查完成标记，未达成
则重注入下一轮。本模块只负责状态与轮次提示，循环编排在 web/goal_runner.py。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from core.fs_utils import atomic_write_json, load_json_or_backup

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETE = "complete"
STATUS_BLOCKED = "blocked"
STATUS_STOPPED = "stopped"

DEFAULT_MAX_ROUNDS = 20

GOAL_FILE = "goal.json"

# 完成标记：goal_round 提示要求 agent 在最终回复末尾单独一行写此标记
COMPLETE_MARKER = "状态: 已完成"


@dataclass
class GoalState:
    objective: str = ""
    status: str = STATUS_ACTIVE
    round: int = 0
    max_rounds: int = DEFAULT_MAX_ROUNDS
    armed: bool = False  # 重启后 load 时置 False（disarm），需手动 resume
    created_at: str = ""
    updated_at: str = ""
    last_summary: str = ""  # 上一轮最终文本，用于轮次提示衔接
    blocked_reason: str = ""


class GoalManager:
    """目标状态机：set/pause/resume/clear/complete/block + JSON 持久化。"""

    def __init__(self, session_dir: Path | str):
        self.session_dir = Path(session_dir)
        self.path = self.session_dir / GOAL_FILE
        self.state = self._load()

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _load(self) -> GoalState:
        """读盘；无论盘中 armed 为何值都 disarm（进程重启 → 不自动续跑）。"""
        data = load_json_or_backup(self.path, {})
        data.pop("armed", None)
        fields = GoalState.__dataclass_fields__
        return GoalState(**{k: v for k, v in data.items() if k in fields})

    def save(self) -> None:
        self.state.updated_at = self._now()
        try:
            atomic_write_json(self.path, asdict(self.state))
        except Exception as e:
            logger.error(f"[goal] 保存状态失败: {e}")

    # ── 查询 ─────────────────────────────────────────────

    @property
    def objective(self) -> str:
        return self.state.objective

    def exists(self) -> bool:
        return bool(self.state.objective)

    def is_active(self) -> bool:
        """目标存在、armed 且状态 active 才继续循环。"""
        return self.exists() and self.state.armed and self.state.status == STATUS_ACTIVE

    def status_text(self) -> str:
        label = {
            STATUS_ACTIVE: "执行中",
            STATUS_PAUSED: "已暂停",
            STATUS_COMPLETE: "已完成",
            STATUS_BLOCKED: "受阻",
            STATUS_STOPPED: "已清除",
        }.get(self.state.status, self.state.status)
        if self.state.status == STATUS_BLOCKED and self.state.blocked_reason:
            label += f"（{self.state.blocked_reason}）"
        return label

    # ── 变更 ─────────────────────────────────────────────

    def set(self, objective: str) -> None:
        objective = objective.strip()
        if not objective:
            return
        now = self._now()
        self.state = GoalState(
            objective=objective,
            status=STATUS_ACTIVE,
            round=0,
            max_rounds=self.state.max_rounds,
            armed=True,
            created_at=now,
            updated_at=now,
        )
        self.save()

    def pause(self) -> None:
        if not self.exists():
            return
        self.state.status = STATUS_PAUSED
        self.state.armed = False
        self.save()

    def resume(self) -> None:
        if not self.exists():
            return
        self.state.status = STATUS_ACTIVE
        self.state.armed = True
        self.save()

    def clear(self) -> None:
        self.state = GoalState()
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception as e:
            logger.warning(f"[goal] 清除状态文件失败: {e}")

    def mark_complete(self, summary: str = "") -> None:
        self.state.status = STATUS_COMPLETE
        self.state.armed = False
        if summary:
            self.state.last_summary = summary
        self.save()

    def block(self, reason: str) -> None:
        self.state.status = STATUS_BLOCKED
        self.state.armed = False
        self.state.blocked_reason = reason
        self.save()

    def set_last_summary(self, summary: str) -> None:
        self.state.last_summary = summary

    def save_round_progress(self) -> None:
        """每轮结束后推进轮次计数并落盘（round 计数由 runner 自增后调用）。"""
        self.save()

    # ── 轮次提示 / 完成检测 ──────────────────────────────

    def next_round_prompt(self) -> str:
        s = self.state
        prev = s.last_summary.strip() or "无"
        return (
            f"<goal_round>\n"
            f"目标: {s.objective}\n"
            f"轮次: {s.round}/{s.max_rounds}\n"
            f"上一轮进展: {prev}\n"
            f"\n"
            f"自主执行，使用工具向目标推进，不要询问用户。\n"
            f"如果目标已完全达成，请在最终回复末尾单独一行写上：{COMPLETE_MARKER}"
        )

    @staticmethod
    def is_complete(final_text: str) -> bool:
        return COMPLETE_MARKER in (final_text or "")


# 每 session_dir 单例：disarm 只在进程内首次构造时发生，避免同一会话
# 多次 /goal 命令误清 armed。
_managers: dict[str, GoalManager] = {}
_managers_lock = threading.Lock()


def get_goal_manager(session_dir: Path | str) -> GoalManager:
    key = str(Path(session_dir))
    with _managers_lock:
        if key not in _managers:
            _managers[key] = GoalManager(key)
        return _managers[key]
