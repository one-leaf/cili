"""Cron 启动编排：迁移 v1 → v2、安装全局种子、启动引擎。"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_start_lock = threading.Lock()


def start():
    """启动调度器；迁移/种子失败只告警，不阻断引擎启动。"""
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
