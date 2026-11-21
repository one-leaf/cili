"""权限运行时状态：会话批准存储与审批审计。

归属 ``core/security``（权限子系统），工具层只保留给 LLM 的门面与卡片文案。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.time_utils import now_str

logger = logging.getLogger(__name__)


def append_permission_audit(session_dir, event: str, data: dict[str, Any]) -> None:
    """审计事件追加到 ``{session_dir}/approval_audit.jsonl``；失败静默。"""
    try:
        if not session_dir:
            return
        d = Path(session_dir)
        d.mkdir(parents=True, exist_ok=True)
        record = {"ts": now_str(), "event": event, **data}
        with open(d / "approval_audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        logger.debug("[permission-audit] 写审计事件失败", exc_info=True)


class PermissionStore:
    """会话级批准存储：decision_id → {command, kind}，批准不消费；单槽 pending。"""

    def __init__(self) -> None:
        self._approved: dict[str, dict[str, str]] = {}
        self.pending: dict[str, Any] | None = None

    def is_approved(self, decision_id: str) -> bool:
        return decision_id in self._approved

    def approve(self, decision_id: str, command: str, kind: str = "command") -> None:
        self._approved[decision_id] = {"command": command, "kind": kind}

    def approved_commands(self) -> list[str]:
        return [v["command"] for v in self._approved.values() if v["kind"] == "command"]

    def approved_navigations(self) -> list[str]:
        return [v["command"] for v in self._approved.values() if v["kind"] == "browser:navigate"]

    def approved_path_rules(self) -> list[dict[str, str]]:
        return [
            {"kind": v["kind"], "command": v["command"]}
            for v in self._approved.values() if v["kind"].startswith("path:")
        ]

    def set_pending(self, permission: dict[str, Any]) -> None:
        self.pending = permission

    def clear_pending(self) -> None:
        self.pending = None
