"""审批看门狗：超时的审批占位结算 + 条件自动恢复。

cili 的审批采用「退出循环 + 占位符 + 恢复」：卡片发出后回合结束，runner
仍留在 SessionRegistry 里。本模块周期扫描这些空闲 runner，把过了期限的审批
结算为「超时未响应，未执行」，并按接口能力决定是否自动 resume：

- Web：有常驻 SSE 推送通道，结算后直接 resume，模型能及时看到超时结果；
- IM（QQ/微信）/ TUI：没有新的入站消息就没有可回复的 msg_id / context_token，
  自动恢复的产出送不出去，还会耗被动回复配额——只结算，等用户下次入站带走；
- 服务重启：启动时只结算不恢复，避免隔夜无人跑模型。

设计文档：``workspace/default/approval-timeout-abort-plan-2026-11-14.md``。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 5.0
# 进程启动时刻：期限在此之前的审批属于「历史遗留」，只结算不自动恢复，
# 避免服务重启后隔夜无人值守地跑模型。
_PROCESS_STARTED_AT = time.time()

# auto_resume 策略 → 允许自动恢复的接口集合
_RESUME_INTERFACES = {
    "web": frozenset({"web"}),
    "all": frozenset({"web", "tui"}),
}


def _can_auto_resume(permission: dict) -> bool:
    policy = str(permission.get("auto_resume") or "web")
    if policy == "none":
        return False
    interface = str(permission.get("interface") or "")
    return interface in _RESUME_INTERFACES.get(policy, frozenset())


def _is_stale(permission: dict) -> bool:
    """期限早于本进程启动 → 历史遗留，只结算不恢复。"""
    try:
        deadline = float(permission.get("deadline_ts") or 0)
    except (TypeError, ValueError):
        return False
    return deadline > 0 and deadline <= _PROCESS_STARTED_AT


def _teen_allows_turn() -> bool:
    """青少年模式时段/时长限制：被拒时只结算，不自动恢复。"""
    try:
        from core.teen_mode import get_teen_mode
        get_teen_mode().check_send_allowed()
        return True
    except Exception:  # noqa: BLE001 —— 任何限制/异常都按不允许恢复处理
        return False


class ApprovalWatchdog:
    """周期扫描 SessionRegistry 中的过期审批。"""

    def __init__(self, interval: float = DEFAULT_INTERVAL_SECONDS):
        self.interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sweep_lock = threading.Lock()

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="approval-watchdog", daemon=True)
        self._thread.start()
        logger.info("[approval-watchdog] 已启动")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        logger.info("[approval-watchdog] 已停止")

    def _run(self) -> None:
        # 启动先做一次「只结算」：历史过期审批不自动跑模型
        try:
            self.sweep_once(settle_only=True)
        except Exception:  # noqa: BLE001
            logger.exception("[approval-watchdog] 启动结算失败")
        while not self._stop.wait(self.interval):
            try:
                self.sweep_once()
            except Exception:  # noqa: BLE001 —— 单次扫描失败不终止看门狗
                logger.exception("[approval-watchdog] 扫描失败")

    # ---------- 扫描 ----------

    def sweep_once(self, *, settle_only: bool = False, now: float | None = None,
                   registry_obj=None) -> int:
        """扫描一轮，返回结算的审批数量（供测试直接调用）。"""
        if not self._sweep_lock.acquire(blocking=False):
            return 0  # 上一轮尚未结束（正常不会发生：扫描本身很快）
        try:
            from core.session_registry import registry as default_registry
            registry = registry_obj or default_registry
            from core.tools.ask_user import (
                find_pending_approval,
                is_expired,
                settle_expired_approvals,
            )

            settled_count = 0
            for key, runner in list(registry.items()):
                try:
                    pending = find_pending_approval(
                        getattr(runner, "session", None))
                    if pending is None or not is_expired(pending, now=now):
                        continue
                    is_running = getattr(runner, "is_running", None)
                    if callable(is_running) and is_running():
                        continue
                    if not registry.claim(key):
                        # 用户答复/另一回合正在处理：交给它结算，不抢
                        continue
                    try:
                        settled = settle_expired_approvals(
                            getattr(runner, "session"), permission_store=getattr(
                                runner, "permission_store", None), now=now)
                        if not settled:
                            registry.release(key)
                            continue
                        settled_count += len(settled)
                        permission = pending.get("permission") or {}
                        logger.info(
                            "[approval-watchdog] 审批超时已结算: session=%s tool=%s id=%s",
                            key, permission.get("tool", ""),
                            permission.get("decision_id", ""))
                        if settle_only or _is_stale(permission) \
                                or not _can_auto_resume(permission) \
                                or not _teen_allows_turn():
                            registry.release(key)
                            continue
                        self._resume_async(registry, key, runner, permission)
                    except Exception:  # noqa: BLE001 —— 单个会话失败不影响其他
                        logger.exception("[approval-watchdog] 会话处理失败: %s", key)
                        registry.release(key)
                except Exception:  # noqa: BLE001
                    logger.debug("[approval-watchdog] 跳过会话 %s", key, exc_info=True)
            return settled_count
        finally:
            self._sweep_lock.release()

    @staticmethod
    def _resume_async(registry, key: str, runner, permission: dict) -> None:
        """在后台线程恢复循环；执行权在 finally 释放。"""

        def _resume() -> None:
            set_iface = getattr(runner, "set_turn_interface", None)
            interface = str(permission.get("interface") or "")
            try:
                resume = getattr(runner, "resume_after_ask_user", None)
                if not callable(resume):
                    logger.warning("[approval-watchdog] runner 不支持 resume: %s", key)
                    return
                # 恢复的回合沿用发卡接口：接口专属工具可见性与后续卡的
                # interface 快照都依赖它（否则新卡不会再自动恢复）。
                if callable(set_iface) and interface:
                    set_iface(interface)
                logger.info("[approval-watchdog] 自动恢复循环: session=%s", key)
                resume()
            except Exception:  # noqa: BLE001
                logger.exception("[approval-watchdog] 自动恢复失败: %s", key)
            finally:
                if callable(set_iface):
                    try:
                        set_iface(None)
                    except Exception:  # noqa: BLE001
                        pass
                registry.release(key)

        threading.Thread(
            target=_resume, name=f"approval-resume-{key}", daemon=True).start()


_watchdog: ApprovalWatchdog | None = None
_watchdog_lock = threading.Lock()


def get_approval_watchdog() -> ApprovalWatchdog:
    global _watchdog
    with _watchdog_lock:
        if _watchdog is None:
            _watchdog = ApprovalWatchdog()
        return _watchdog


def start_approval_watchdog() -> None:
    get_approval_watchdog().start()


def stop_approval_watchdog() -> None:
    global _watchdog
    with _watchdog_lock:
        watchdog = _watchdog
        _watchdog = None
    if watchdog is not None:
        watchdog.stop()
