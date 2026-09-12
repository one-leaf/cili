"""ClockTool — 获取当前时间 / 短时等待（sleep）。"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from core.tools.base import Tool, ToolResult


class ClockTool(Tool):
    name = "clock"
    description = (
        "Get the current date/time, or pause execution for a short time.\n"
        "- action='now' (default): returns current local time, weekday, UTC offset, and UTC time.\n"
        "- action='sleep': waits for the given number of seconds (max 60). Useful when waiting "
        "for an external process to finish.\n"
        "For long-running scheduled tasks use the 'cron' tool instead of a long clock sleep."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["now", "sleep"],
                "description": "'now' reads the current time (default); 'sleep' waits.",
            },
            "seconds": {
                "type": "number",
                "description": "Seconds to wait when action='sleep'. Max 60. Default 1.",
            },
        },
        "required": [],
    }

    MAX_SLEEP_SECONDS = 60

    def execute(self, action: str = "now", seconds: float = 1.0, **_kwargs) -> ToolResult:
        if action == "sleep":
            secs = max(0.0, min(float(seconds), self.MAX_SLEEP_SECONDS))
            time.sleep(secs)
            return ToolResult(f"Slept for {secs:.1f} seconds.")
        if action == "now":
            local = datetime.now()
            utc = datetime.now(timezone.utc)
            return ToolResult(
                f"Local time: {local.strftime('%Y-%m-%d %H:%M:%S')} {local.strftime('%A')} "
                f"(UTC{self._utc_offset_str()})\n"
                f"UTC time:   {utc.strftime('%Y-%m-%d %H:%M:%S')} {utc.strftime('%A')}"
            )
        return ToolResult(f"Error: unknown action '{action}'. Use 'now' or 'sleep'.", error=True)

    @staticmethod
    def _utc_offset_str() -> str:
        offset = datetime.now().astimezone().utcoffset()
        total = int(offset.total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return f"{sign}{total // 3600:02d}{total % 3600 // 60:02d}"
