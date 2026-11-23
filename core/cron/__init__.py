"""Cron v2：调度引擎、执行器、投递与迁移。

对外入口（保持旧调用点可用）：
    start_scheduler() / stop_scheduler() / get_scheduler()
    any_cron_runner_running()
    store_lock()
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def start_scheduler():
    """启动调度器（迁移/种子/引擎启动见 ``core.cron.bootstrap``）。"""
    from core.cron.bootstrap import start

    return start()


def stop_scheduler() -> None:
    from core.cron import engine as engine_mod

    engine_mod.get_engine().stop()


def get_scheduler():
    from core.cron import engine as engine_mod

    return engine_mod.get_engine()


def any_cron_runner_running() -> bool:
    try:
        from core.cron import engine as engine_mod

        return engine_mod.get_engine().any_running()
    except Exception:  # noqa: BLE001
        return False


def store_lock():
    from core.cron import store

    return store.store_lock()
