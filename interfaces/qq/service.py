"""QQ 接入端生命周期：每个账号一个宿主线程 + 宿主 loop。

**线程模型**：每个账号两个线程，只有一个是我们的。
- **宿主线程（我们的）**：起一个守护线程跑 ``asyncio.new_event_loop()``，这个 loop
  就是宿主 loop——``turn_dispatch``、附件下载、出站上传都在它上面。
- **传输线程（SDK 的）**：``QQWebSocket`` 自带，处理网络/心跳/重连，入站回调经
  ``run_coroutine_threadsafe`` 派回宿主 loop。

**多账号**（设计 §4.4）：``_hosts`` 按 account_id 持有宿主；start / stop /
running 的 ``account_id`` 参数为 None 时作用于**全部**账号——``core.app_context``
的无参调用因此一字不改，按账号操作的调用方（Web 路由）传 id。

**API 域名**：进程级资源（SDK 在 import 时求值），默认沙箱，环境变量
``QQ_API_BASE`` 可覆盖。``gateway.py`` 在导入 SDK 之前 setdefault，所以本
模块不再需要「先设环境变量再导入 SDK」的次序约束。
"""

from __future__ import annotations

import asyncio
import logging
import threading

from interfaces.common.host import HostBase
from interfaces.common.owner import OwnerTracker
from interfaces.common.service import accounts_running, start_accounts, stop_accounts
from interfaces.qq.config import QQConfig, load_qq_accounts
from interfaces.qq.runtime import QQRuntime
from interfaces.qq.state import load_account_state

logger = logging.getLogger(__name__)

_hosts: dict[str, QQHost] = {}
_lock = threading.Lock()


class QQHost(HostBase):
    """一个账号的宿主：拥有宿主 loop、runtime、gateway 与入站处理器。"""

    _thread_prefix = "qq"
    _log_tag = "qq"

    def __init__(self, cfg: QQConfig) -> None:
        super().__init__(cfg)
        self.runtime: QQRuntime | None = None
        self.gateway = None
        self.handler = None

    async def _async_start(self) -> None:
        from interfaces.qq.gateway import QQGateway, sdk_available, sdk_import_error

        if not sdk_available():
            logger.error(
                f"[qq/service] 未安装 qqbot-agent-sdk，QQ 接入端不启动：{sdk_import_error()}"
            )
            return

        state = load_account_state(self.cfg.id)
        self.runtime = QQRuntime(
            account_id=self.cfg.id,
            cfg=self.cfg,
            state=state,
            loop=asyncio.get_running_loop(),
        )
        owner = OwnerTracker(state, log_tag="qq")

        from interfaces.qq.inbound import InboundHandler

        self.handler = InboundHandler(self.runtime, owner)
        self.gateway = QQGateway(self.runtime, self.handler.handle, app_id=self.cfg.app_id)
        try:
            await self.gateway.start()
        except Exception:
            self.ready = False
            logger.exception(f"[qq/service] 账号 {self.cfg.id} 网关启动失败（凭据/网络问题？）")
        else:
            self.ready = True

    async def _async_stop(self) -> None:
        if self.gateway is not None:
            await self.gateway.stop()


# ─── 模块级启停（幂等；account_id 为 None 时作用于全部账号） ──────────


def start_qq_interface(account_id: str | None = None) -> bool:
    """启动 QQ 接入端：启动所有（或指定）enabled 且凭据齐全的账号。

    返回是否至少启动了一个账号。未启用 / 未配置凭据 / 已在跑的账号静默跳过。
    """

    def _started_suffix() -> str:
        from interfaces.qq.gateway import effective_api_base

        return f"；API 域名：{effective_api_base()}"

    def _idle_hint() -> str:
        accounts = load_qq_accounts()
        if not accounts:
            return "qq 段无账号，跳过启动"
        if not any(a.enabled for a in accounts):
            return "无 enabled 账号，跳过启动"
        return ""

    return start_accounts(
        account_id=account_id,
        hosts=_hosts,
        lock=_lock,
        load_accounts=load_qq_accounts,
        host_cls=QQHost,
        not_ready=lambda cfg: None if cfg.is_ready() else "已启用但 app_id/secret 未配置",
        log_tag="qq",
        started_suffix=_started_suffix,
        idle_hint=_idle_hint,
    )


def stop_qq_interface(account_id: str | None = None) -> None:
    """停止 QQ 接入端（幂等）。account_id 为 None 时停止全部。"""
    stop_accounts(account_id=account_id, hosts=_hosts, lock=_lock, log_tag="qq")


def qq_interface_running(account_id: str | None = None) -> bool:
    """是否有账号在跑。account_id 为 None 时指「任一账号」。"""
    return accounts_running(account_id=account_id, hosts=_hosts, lock=_lock)
