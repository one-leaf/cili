"""接入端生命周期三件套：start / stop / running（QQ / 微信 / 邮件共用）。

每个接入端模块持有自己的 ``_hosts``（account_id → HostBase）与 ``_lock``，
模块级 ``start_xxx_interface`` / ``stop_xxx_interface`` / ``xxx_interface_running``
三件套的**语义完全一致**：

- start：启动所有（或指定）enabled 且就绪的账号；已在跑的幂等跳过；启动
  失败的宿主 stop 后不登记。返回是否至少启动了一个账号。
- stop：停止所有（或指定）账号（幂等）。
- running：是否有账号在跑（顺带清掉已死宿主）。

差异全部经参数注入（账号加载器、宿主类、就绪检查、日志标签），共享层
不感知通道。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from interfaces.common.host import HostBase

logger = logging.getLogger(__name__)


def start_accounts(
    *,
    account_id: str | None,
    hosts: dict[str, HostBase],
    lock: threading.Lock,
    load_accounts: Callable[[], list],
    host_cls: Callable[[Any], HostBase],
    not_ready: Callable[[Any], str | None],
    log_tag: str,
    started_suffix: Callable[[], str] | None = None,
    idle_hint: Callable[[], str] | None = None,
) -> bool:
    """启动接入端账号（幂等）。返回是否至少启动了一个账号。

    ``not_ready(cfg)`` 返回不可就绪的原因文案（就绪返回 None），原因会拼进
    跳过日志；``started_suffix`` 附加在「已启动 N 个账号」之后（如 QQ 的
    API 域名）；``idle_hint`` 在「按全部启动但没启动任何账号」时给出原因
    文案（返回空串则不打日志）。
    """
    started: list[str] = []
    with lock:
        for cfg in load_accounts():
            if account_id is not None and cfg.id != account_id:
                continue
            if not cfg.enabled:
                continue
            reason = not_ready(cfg)
            if reason is not None:
                logger.warning(
                    f"[{log_tag}/service] 账号 {cfg.label or cfg.id} {reason}，跳过"
                )
                continue
            existing = hosts.get(cfg.id)
            if existing is not None and existing.is_alive():
                continue          # 已在跑（幂等）
            hosts.pop(cfg.id, None)
            host = host_cls(cfg)
            if not host.start():
                logger.warning(
                    f"[{log_tag}/service] 账号 {cfg.label or cfg.id} 启动失败，未登记"
                )
                host.stop()
                continue
            hosts[cfg.id] = host
            started.append(cfg.id)
    if started:
        suffix = started_suffix() if started_suffix is not None else ""
        logger.info(f"[{log_tag}/service] 已启动 {len(started)} 个账号：{started}{suffix}")
    elif account_id is None and idle_hint is not None:
        hint = idle_hint()
        if hint:
            logger.debug(f"[{log_tag}/service] {hint}")
    return bool(started)


def stop_accounts(
    *,
    account_id: str | None,
    hosts: dict[str, HostBase],
    lock: threading.Lock,
    log_tag: str,
) -> None:
    """停止接入端账号（幂等）。account_id 为 None 时停止全部。"""
    with lock:
        if account_id is None:
            targets = dict(hosts)
            hosts.clear()
        else:
            host = hosts.pop(account_id, None)
            targets = {account_id: host} if host is not None else {}
    for hid, host in targets.items():
        try:
            host.stop()
        except Exception:  # noqa: BLE001
            logger.exception(f"[{log_tag}/service] 停止账号 {hid} 失败")


def accounts_running(
    *,
    account_id: str | None,
    hosts: dict[str, HostBase],
    lock: threading.Lock,
) -> bool:
    """是否有账号在跑。account_id 为 None 时指「任一账号」。"""
    with lock:
        for hid, host in list(hosts.items()):
            if not host.is_alive():
                hosts.pop(hid, None)
        if account_id is not None:
            return account_id in hosts
        return bool(hosts)
