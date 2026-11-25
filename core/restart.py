"""启动脚本托管的重启协议与空闲重启调度。

start.ps1 以 CILI_LAUNCHER=ps1 启动；服务请求重启时以 RESTART_EXIT_CODE 退出，
启动脚本据此重新走「应用升级包 → 启动」循环。直接 python main.py 启动时无循环
托管，自动/手动重启不可用。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

LAUNCHER_ENV = "CILI_LAUNCHER"
LAUNCH_RESTART_ENV = "CILI_LAUNCH_RESTART"
RESTART_EXIT_CODE = 42
# 环境重建退出码：managed Python 环境损坏且自动修复无效时以此退出；
# start.ps1 删除 data/deps/python 重装后重启（与 42 的升级重启区分）。
ENV_REBUILD_EXIT_CODE = 43

# 本次启动应用升级包是否失败（失败时不安排自动重启，避免重启循环）
_apply_failed_this_boot = False


def note_apply_failed(failed: bool) -> None:
    global _apply_failed_this_boot
    _apply_failed_this_boot = failed


_uvicorn_server: Any = None
_restart_requested: str | None = None
_restart_lock = threading.Lock()


def is_launcher_managed() -> bool:
    """当前服务是否由 start.ps1 启动循环托管（决定能否自动重启）。"""
    return os.environ.get(LAUNCHER_ENV, "") == "ps1"


def _auto_restart_enabled() -> bool:
    """读取 setting.json 的 system.auto_restart_after_upgrade，未配置时默认开启。"""
    try:
        from core.config import load_global_config
        cfg = load_global_config()
        return bool((cfg.get("system") or {}).get("auto_restart_after_upgrade", True))
    except Exception:  # noqa: BLE001
        return True

def set_uvicorn_server(server: Any) -> None:
    """main.py 启动 uvicorn 后注入 Server 句柄（request_restart 需要它触发优雅停机）。

    若重启请求在注入前已到达（启动窗口内触发自动重启），这里补发停机通知，
    避免请求只记了标志却没人通知 uvicorn。
    """
    global _uvicorn_server
    _uvicorn_server = server
    if _restart_requested is not None:
        server.should_exit = True


def request_restart(reason: str) -> bool:
    """请求优雅重启：记录原因并通知 uvicorn 停机。

    仅在 start.ps1 托管下可用（退出码协议需要启动循环接住）。重启流程已在
    进行中时幂等返回 True。
    """
    global _restart_requested
    if not is_launcher_managed():
        logger.warning("[updater] 非 start.ps1 托管启动，无法自动重启（请改用 start.cmd/start.ps1）")
        return False
    with _restart_lock:
        if _restart_requested:
            return True
        _restart_requested = reason
    logger.info(f"[updater] 请求重启: {reason}")
    if _uvicorn_server is not None:
        _uvicorn_server.should_exit = True
    return True


def shutdown_and_restart_if_requested() -> None:
    """main.py 在 uvicorn.run 返回后调用：有未决重启请求时以退出码交回启动循环。

    此刻进程已完成优雅停机（lifespan shutdown / app_context.shutdown 均已
    执行），sys.exit 即可安全退出。
    """
    global _restart_requested
    with _restart_lock:
        reason = _restart_requested
        _restart_requested = None
    if reason:
        logger.info(
            f"[updater] {reason}，以退出码 {RESTART_EXIT_CODE} 交回启动脚本"
            "（应用升级包后以新代码重启）"
        )
        sys.exit(RESTART_EXIT_CODE)





_idle_restart_scheduled = False
_idle_restart_lock = threading.Lock()


def is_idle_restart_waiting() -> bool:
    """是否已有「等空闲自动重启」任务在等待中。"""
    return _idle_restart_scheduled


def schedule_idle_restart(reason: str, poll_interval: float = 30.0,
                          max_wait: float = 1800.0,
                          confirm_delay: float = 3.0) -> bool:
    """升级包下载完成后安排「等空闲即优雅重启」。

    空闲判定见 all_sessions_idle；确认空闲后由 request_restart 触发优雅停机，
    进程以 RESTART_EXIT_CODE 退出、由 start.ps1 循环重新拉起（先应用升级包）。
    超过 max_wait 仍未空闲则放弃（升级包保留，下次启动或手动重启时应用）。
    同一时刻只允许一个等待任务；仅在 start.ps1 托管且未关闭自动重启时生效。

    Args:
        reason: 重启原因（日志用）。
        poll_interval: 空闲轮询间隔（秒）。
        max_wait: 最长等待空闲时间（秒），超时放弃。
        confirm_delay: 空闲确认后的二次确认前等待（秒），避免恰好踩中
            「刚认领运行权但尚未标记运行」的会话窗口。
    """
    from core.session_control import all_sessions_idle

    global _idle_restart_scheduled
    if not is_launcher_managed():
        return False
    if not _auto_restart_enabled():
        logger.info("[updater] 自动重启已关闭（system.auto_restart_after_upgrade=false），跳过")
        return False
    if _apply_failed_this_boot:
        logger.warning("[updater] 本次启动升级包应用失败，暂不安排自动重启（避免重启循环）")
        return False
    with _idle_restart_lock:
        if _idle_restart_scheduled:
            return False
        _idle_restart_scheduled = True

    def _worker() -> None:
        global _idle_restart_scheduled
        deadline = time.monotonic() + max_wait
        try:
            while time.monotonic() < deadline:
                if all_sessions_idle():
                    time.sleep(confirm_delay)
                    if all_sessions_idle():
                        if request_restart(reason):
                            return
                        logger.warning("[updater] 发起重启失败，放弃自动重启（升级包保留）")
                        return
                time.sleep(poll_interval)
            logger.warning("[updater] 等待空闲超时，取消自动重启（升级包保留，重启服务后应用）")
        finally:
            with _idle_restart_lock:
                _idle_restart_scheduled = False

    threading.Thread(target=_worker, daemon=True, name="UpgradeIdleRestart").start()
    return True
