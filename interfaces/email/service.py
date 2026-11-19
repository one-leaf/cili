"""邮件接入端生命周期：每个账号一个宿主线程 + 宿主 loop。

与微信同构（``interfaces/wechat/service.py``）：iLink/IMAP 都是纯协议，没有 SDK
传输线程——宿主 loop 既是轮询循环也是回合驱动。IMAP/SMTP 的阻塞调用全部走
executor，不卡宿主 loop。

多账号：``_hosts`` 按 account_id 持有宿主；start / stop / running 的
``account_id`` 参数为 None 时作用于**全部**账号。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import suppress

from interfaces.common.access import OwnerTracker
from interfaces.common.host import HostBase
from interfaces.common.service import accounts_running, start_accounts, stop_accounts
from interfaces.email.config import EmailConfig, load_email_accounts

logger = logging.getLogger(__name__)

_hosts: dict[str, "EmailHost"] = {}
_lock = threading.Lock()


class EmailHost(HostBase):
    """一个账号的宿主：拥有宿主 loop、runtime、轮询任务与入站处理器。"""

    _thread_prefix = "email"
    _log_tag = "email"

    def __init__(self, cfg: EmailConfig) -> None:
        super().__init__(cfg)
        self.runtime = None
        self.poller = None
        self._poll_task: asyncio.Task[None] | None = None

    async def _async_start(self) -> None:
        from interfaces.email.client import EmailClient
        from interfaces.email.inbound import InboundHandler
        from interfaces.email.poller import Poller
        from interfaces.email.runtime import EmailRuntime
        from interfaces.email.state import load_account_state

        if not self.cfg.is_ready():
            self.ready = False
            logger.warning(
                f"[email/service] 账号 {self.cfg.label or self.cfg.id} 配置不完整"
                "（邮箱地址/服务器/密码），邮件接入端不启动"
            )
            return

        client = EmailClient(self.cfg)
        runtime = EmailRuntime(
            account_id=self.cfg.id,
            cfg=self.cfg,
            state=load_account_state(self.cfg.id),
            loop=asyncio.get_running_loop(),
            client=client,
        )
        owner = OwnerTracker(runtime.state, log_tag="email")
        handler = InboundHandler(runtime, owner)
        poller = Poller(runtime, handler)

        runtime.poller = poller
        runtime.handler = handler
        self.runtime = runtime
        self.poller = poller
        self._poll_task = asyncio.create_task(self._run_poller(poller))
        self.ready = True
        logger.info(
            f"[email/service] 账号 {self.cfg.label or self.cfg.id} 已启动，"
            f"每 {self.cfg.poll_interval_seconds:.0f}s 轮询一次"
        )

    async def _run_poller(self, poller) -> None:
        try:
            await poller.run()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception(f"[email/service] 账号 {self.cfg.id} 轮询异常退出")
        finally:
            self.ready = False

    async def _async_stop(self) -> None:
        if self.poller is not None:
            with suppress(Exception):
                await self.poller.stop()
        if self._poll_task is not None:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._poll_task
            self._poll_task = None
        self.runtime = None
        self.poller = None


# ─── 模块级启停（幂等；account_id 为 None 时作用于全部账号） ──────────


def start_email_interface(account_id: str | None = None) -> bool:
    """启动邮件接入端：启动所有（或指定）enabled 且配置完整的账号。

    返回是否至少启动了一个账号。未启用 / 配置不完整 / 已在跑的账号静默跳过。
    """
    return start_accounts(
        account_id=account_id,
        hosts=_hosts,
        lock=_lock,
        load_accounts=load_email_accounts,
        host_cls=EmailHost,
        not_ready=lambda cfg: None if cfg.is_ready() else "已启用但配置不完整",
        log_tag="email",
    )


def stop_email_interface(account_id: str | None = None) -> None:
    """停止邮件接入端（幂等）。account_id 为 None 时停止全部。"""
    stop_accounts(account_id=account_id, hosts=_hosts, lock=_lock, log_tag="email")


def email_interface_running(account_id: str | None = None) -> bool:
    """是否有账号在跑。account_id 为 None 时指「任一账号」。"""
    return accounts_running(account_id=account_id, hosts=_hosts, lock=_lock)


def email_status_snapshot(account_id: str) -> dict:
    """一个账号的运行状态（供 ``GET /api/email/status``）。"""
    with _lock:
        host = _hosts.get(account_id)
        running = bool(host is not None and host.is_alive())
        status = dict(getattr(host.runtime, "status", {}) or {}) if host else {}
    return {
        "running": running,
        "last_poll_at": str(status.get("last_poll_at") or ""),
        "last_error": str(status.get("last_error") or ""),
    }
