"""Cron 运行期标记（轻量模块，避免工具层导入执行器造成循环依赖）。"""

from __future__ import annotations

from contextvars import ContextVar

cron_run_active: ContextVar[bool] = ContextVar("cron_run_active", default=False)
