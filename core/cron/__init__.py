"""Cron v2：调度引擎、执行器、投递与迁移。

对外入口（保持旧调用点可用）：
    start_scheduler() / stop_scheduler() / get_scheduler()
    any_cron_runner_running()
    cron_config_lock()
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_start_lock = threading.Lock()


def start_scheduler():
    """启动调度器：迁移 v1 → v2 → 安装全局种子 → 启动引擎。"""
    from core.cron import engine as engine_mod
    from core.cron import migration, seeds

    with _start_lock:
        try:
            reports = migration.migrate_all()
            failed = [r for r in reports if r.get("status") == "failed"]
            if failed:
                logger.warning("[cron] migration failed for %d workspace(s): %s", len(failed), failed)
        except Exception:  # noqa: BLE001
            logger.exception("[cron] migration failed; continuing with existing v2 data")
        try:
            seeds.install_seeds()
        except Exception:  # noqa: BLE001
            logger.exception("[cron] seed installation failed")
        engine = engine_mod.get_engine()
        engine.start()
        return engine


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


def cron_config_lock():
    from core.cron import store

    return store.config_lock()
