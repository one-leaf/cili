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

from interfaces.qq.access import OwnerTracker
from interfaces.qq.config import QQConfig, load_qq_accounts
from interfaces.qq.runtime import QQRuntime
from interfaces.qq.state import load_account_state

logger = logging.getLogger(__name__)

_hosts: dict[str, QQHost] = {}
_lock = threading.Lock()


class QQHost:
    """一个账号的宿主：拥有宿主 loop、runtime、gateway 与入站处理器。"""

    def __init__(self, cfg: QQConfig) -> None:
        self.cfg = cfg
        self.loop: asyncio.AbstractEventLoop | None = None
        self.runtime: QQRuntime | None = None
        self.gateway = None
        self.handler = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    # ── 启动 ────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._thread_main, name=f"qq-{self.cfg.id}", daemon=True
        )
        self._thread.start()
        # 等宿主 loop 就绪（最多 5s），便于调用方知道是否起来了
        self._started.wait(timeout=5.0)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            loop.run_until_complete(self._async_start())
        except Exception:
            logger.exception(f"[qq/service] 账号 {self.cfg.id} 启动失败")
            self._started.set()
            loop.close()
            return

        self._started.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(self._async_stop())
            except Exception:  # noqa: BLE001
                logger.exception(f"[qq/service] 账号 {self.cfg.id} 关闭失败")
            loop.close()

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
        owner = OwnerTracker(state)

        from interfaces.qq.inbound import InboundHandler

        self.handler = InboundHandler(self.runtime, owner)
        self.gateway = QQGateway(self.runtime, self.handler.handle, app_id=self.cfg.app_id)
        try:
            await self.gateway.start()
        except Exception:
            logger.exception(f"[qq/service] 账号 {self.cfg.id} 网关启动失败（凭据/网络问题？）")

    async def _async_stop(self) -> None:
        if self.gateway is not None:
            await self.gateway.stop()

    # ── 停止 ────────────────────────────────────────────────────────

    def stop(self) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10.0)
        logger.info(f"[qq/service] 账号 {self.cfg.id} 已停止")


# ─── 模块级启停（幂等；account_id 为 None 时作用于全部账号） ──────────


def start_qq_interface(account_id: str | None = None) -> bool:
    """启动 QQ 接入端：启动所有（或指定）enabled 且凭据齐全的账号。

    返回是否至少启动了一个账号。未启用 / 未配置凭据 / 已在跑的账号静默跳过。
    """
    started: list[str] = []
    with _lock:
        for cfg in load_qq_accounts():
            if account_id is not None and cfg.id != account_id:
                continue
            if not cfg.enabled:
                continue
            if not cfg.is_ready():
                logger.warning(
                    f"[qq/service] 账号 {cfg.label or cfg.id} 已启用但 app_id/secret 未配置，跳过"
                )
                continue
            if cfg.id in _hosts:
                continue          # 已在跑（幂等）
            host = QQHost(cfg)
            host.start()
            _hosts[cfg.id] = host
            started.append(cfg.id)
    if started:
        from interfaces.qq.gateway import effective_api_base

        logger.info(
            f"[qq/service] 已启动 {len(started)} 个账号：{started}；"
            f"API 域名：{effective_api_base()}"
        )
    elif account_id is None:
        accounts = load_qq_accounts()
        if not accounts:
            logger.debug("[qq/service] qq 段无账号，跳过启动")
        elif not any(a.enabled for a in accounts):
            logger.debug("[qq/service] 无 enabled 账号，跳过启动")
    return bool(started)


def stop_qq_interface(account_id: str | None = None) -> None:
    """停止 QQ 接入端（幂等）。account_id 为 None 时停止全部。"""
    with _lock:
        if account_id is None:
            hosts = dict(_hosts)
            _hosts.clear()
        else:
            host = _hosts.pop(account_id, None)
            hosts = {account_id: host} if host is not None else {}
    for hid, host in hosts.items():
        try:
            host.stop()
        except Exception:  # noqa: BLE001
            logger.exception(f"[qq/service] 停止账号 {hid} 失败")


def qq_interface_running(account_id: str | None = None) -> bool:
    """是否有账号在跑。account_id 为 None 时指「任一账号」。"""
    if account_id is not None:
        return account_id in _hosts
    return bool(_hosts)
