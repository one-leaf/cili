"""调度规则：at / interval / cron 的下一次触发计算（含时区与 DST）。

内部时间约定：
- schedule.timezone 为空：使用服务器本地 naive datetime（与 v1 行为一致）；
- 指定 IANA 时区：使用该时区的 aware datetime，持久化为 ISO 8601 带偏移；
- 引擎统一用 epoch 秒比较与睡眠，避免 naive/aware 混用。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.cron.model import CronValidationError, Job

logger = logging.getLogger(__name__)

_FALLBACK_INTERVAL_MINUTES = 60
_MAX_SEARCH_MINUTES = 366 * 24 * 60


def _parse_cron_field(field: str, min_val: int, max_val: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        if "/" in part:
            range_part, step_text = part.split("/", 1)
            step = int(step_text)
            if step <= 0:
                raise CronValidationError(f"invalid cron step: {part!r}")
            if range_part == "*":
                start, end = min_val, max_val
            elif "-" in range_part:
                start, end = map(int, range_part.split("-", 1))
            else:
                start, end = int(range_part), max_val
            values.update(range(start, end + 1, step))
        elif "-" in part:
            start, end = map(int, part.split("-", 1))
            values.update(range(start, end + 1))
        elif part == "*":
            values.update(range(min_val, max_val + 1))
        else:
            values.add(int(part))
    return {v for v in values if min_val <= v <= max_val}


def parse_cron_expr(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """解析 5 字段 cron 表达式（分 时 日 月 周，周 0=周日）。"""
    parts = str(expr or "").strip().split()
    if len(parts) != 5:
        raise CronValidationError(
            "cron expression must have 5 fields (minute hour day month weekday)"
        )
    try:
        minute, hour, day, month, weekday = parts
        return (
            _parse_cron_field(minute, 0, 59),
            _parse_cron_field(hour, 0, 23),
            _parse_cron_field(day, 1, 31),
            _parse_cron_field(month, 1, 12),
            _parse_cron_field(weekday, 0, 6),
        )
    except (ValueError, CronValidationError) as exc:
        raise CronValidationError(f"invalid cron expression {expr!r}: {exc}") from exc


def timezone_for(job: Job) -> ZoneInfo | None:
    name = job.schedule.get("timezone")
    if not name:
        return None
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronValidationError(f"unknown timezone: {name!r}") from exc


def parse_iso(value: str, tz: ZoneInfo | None = None) -> datetime:
    """解析 ISO 时间；无偏移时按 tz（或服务器本地）解释。"""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None and tz is not None:
        dt = dt.replace(tzinfo=tz, fold=0)
    return dt


def dt_to_ts(dt: datetime) -> float:
    return dt.timestamp()


def ts_to_now_for(job: Job) -> datetime:
    tz = timezone_for(job)
    now = datetime.now(timezone.utc) if tz is not None else datetime.now()
    return now.astimezone(tz) if tz is not None else now


def _next_cron_occurrence(
    expr: str, after: datetime, tz: ZoneInfo | None
) -> datetime:
    minutes, hours, days, months, weekdays = parse_cron_expr(expr)

    if tz is not None:
        base_local = (
            after.astimezone(tz).replace(tzinfo=None)
            if after.tzinfo is not None
            else after
        )
    else:
        base_local = after.replace(tzinfo=None) if after.tzinfo is not None else after

    check = base_local.replace(second=0, microsecond=0) + timedelta(minutes=1)
    after_ts = after.timestamp()

    for _ in range(_MAX_SEARCH_MINUTES):
        if (
            check.month in months
            and check.day in days
            and check.hour in hours
            and check.minute in minutes
            and (check.weekday() + 1) % 7 in weekdays
        ):
            if tz is None:
                candidate = check
            else:
                candidate = check.replace(tzinfo=tz, fold=0)
                roundtrip = (
                    candidate.astimezone(timezone.utc)
                    .astimezone(tz)
                    .replace(tzinfo=None)
                )
                if roundtrip != check:
                    # 夏令时缺口：该墙上时间不存在，跳到下一分钟
                    check += timedelta(minutes=1)
                    continue
            if candidate.timestamp() > after_ts:
                return candidate
        check += timedelta(minutes=1)

    logger.warning("[cron] no valid time found for expression %r, fallback +60m", expr)
    return (
        after + timedelta(minutes=_FALLBACK_INTERVAL_MINUTES)
        if after.tzinfo is not None
        else after + timedelta(minutes=_FALLBACK_INTERVAL_MINUTES)
    )


def compute_next_run(
    job: Job,
    base: datetime | None,
    *,
    now: datetime | None = None,
) -> datetime:
    """计算 base 之后的下一次触发时间。

    base 为 None 表示首次计算：
    - interval：now + initial_delay_minutes（缺省用 minutes）；
    - cron：now 之后的下一个匹配时刻；
    - at：schedule.time 本身。
    """
    schedule_type = job.schedule.get("type", "interval")
    tz = timezone_for(job)
    now = now or ts_to_now_for(job)

    if schedule_type == "at":
        value = job.schedule.get("time")
        if not value:
            raise CronValidationError("schedule.time is required for type='at'")
        dt = parse_iso(str(value), tz)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=None)
        return dt

    if schedule_type == "interval":
        try:
            interval = int(job.schedule.get("minutes", _FALLBACK_INTERVAL_MINUTES))
        except (TypeError, ValueError):
            interval = _FALLBACK_INTERVAL_MINUTES
        if interval <= 0:
            interval = _FALLBACK_INTERVAL_MINUTES
        if base is None:
            delay = job.schedule.get("initial_delay_minutes")
            try:
                minutes = int(delay) if delay is not None else interval
            except (TypeError, ValueError):
                minutes = interval
            if minutes <= 0:
                minutes = interval
            return now + timedelta(minutes=minutes)
        return base + timedelta(minutes=interval)

    if schedule_type == "cron":
        return _next_cron_occurrence(str(job.schedule.get("expr", "")), base or now, tz)

    return now + timedelta(minutes=_FALLBACK_INTERVAL_MINUTES)


def validate_schedule(job: Job) -> None:
    """完整校验调度（含时区与 cron 表达式）。"""
    schedule_type = job.schedule.get("type")
    if schedule_type == "cron":
        parse_cron_expr(str(job.schedule.get("expr", "")))
    if schedule_type == "at":
        parse_iso(str(job.schedule.get("time", "")), timezone_for(job))
    timezone_for(job)


def next_run_iso(job: Job, base: datetime | None, *, now: datetime | None = None) -> str:
    return compute_next_run(job, base, now=now).isoformat()


def schedule_summary(job: Job) -> str:
    schedule = job.schedule or {}
    schedule_type = schedule.get("type")
    tz = schedule.get("timezone") or "服务器本地"
    if schedule_type == "interval":
        minutes = schedule.get("minutes", "?")
        initial = schedule.get("initial_delay_minutes")
        text = f"每 {minutes} 分钟"
        if initial:
            text += f"（首次 {initial} 分钟后）"
        return f"{text} · {tz}"
    if schedule_type == "cron":
        return f"cron: {schedule.get('expr', '?')} · {tz}"
    if schedule_type == "at":
        return f"一次性: {schedule.get('time', '?')} · {tz}"
    return "unknown schedule"
