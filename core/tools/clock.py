"""ClockTool — 获取当前时间（含指定时区）/ 短时等待（sleep）。"""

from __future__ import annotations

import time
from datetime import datetime, timezone as _dt_timezone

from core.tools.base import Tool, ToolResult


class ClockTool(Tool):
    name = "clock"
    description = (
        "Get the current date/time, or pause execution for a short time.\n"
        "- action='now' (default): returns current local time, weekday, UTC offset, and UTC time.\n"
        "  Optional 'timezone' (IANA name, e.g. 'Asia/Ho_Chi_Minh') returns the time in that "
        "timezone directly, so you do NOT need to do UTC offset arithmetic yourself.\n"
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
            "timezone": {
                "type": "string",
                "description": "IANA timezone name for action='now', e.g. 'Asia/Ho_Chi_Minh', "
                "'America/New_York', 'Europe/Paris'. Omit to use the machine's local timezone.",
            },
        },
        "required": [],
    }

    MAX_SLEEP_SECONDS = 60

    def execute(self, action: str = "now", seconds: float = 1.0, timezone: str | None = None, **_kwargs) -> ToolResult:
        if action == "sleep":
            secs = max(0.0, min(float(seconds), self.MAX_SLEEP_SECONDS))
            time.sleep(secs)
            return ToolResult(f"Slept for {secs:.1f} seconds.")
        if action == "now":
            utc = datetime.now(_dt_timezone.utc)
            if timezone:
                try:
                    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                except ImportError:
                    return ToolResult(
                        "Error: zoneinfo not available. Install Python 3.9+ or the 'tzdata' package.",
                        error=True,
                    )
                try:
                    tz = ZoneInfo(timezone)
                except ZoneInfoNotFoundError:
                    return ToolResult(
                        f"Error: unknown timezone '{timezone}'. Use an IANA name like "
                        f"'Asia/Ho_Chi_Minh', 'America/New_York'. On Windows install the "
                        f"'tzdata' package if needed.",
                        error=True,
                    )
                local = datetime.now(tz)
                return ToolResult(
                    f"{timezone} time: {local.strftime('%Y-%m-%d %H:%M:%S')} {local.strftime('%A')} "
                    f"(UTC{self._utc_offset_str(local)})\n"
                    f"UTC time:    {utc.strftime('%Y-%m-%d %H:%M:%S')} {utc.strftime('%A')}"
                )
            local = datetime.now().astimezone()
            return ToolResult(
                f"Local time: {local.strftime('%Y-%m-%d %H:%M:%S')} {local.strftime('%A')} "
                f"(UTC{self._utc_offset_str(local)})\n"
                f"UTC time:   {utc.strftime('%Y-%m-%d %H:%M:%S')} {utc.strftime('%A')}"
            )
        return ToolResult(f"Error: unknown action '{action}'. Use 'now' or 'sleep'.", error=True)

    @staticmethod
    def _utc_offset_str(local: datetime) -> str:
        offset = local.utcoffset()
        total = int(offset.total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return f"{sign}{total // 3600:02d}{total % 3600 // 60:02d}"
