"""QQ 接入端生命周期：宿主线程 + 宿主 loop + 启停。

**线程模型**（§5.1）：两个线程，只有一个是我们的。
- **宿主线程（我们的）**：起一个守护线程跑 ``asyncio.new_event_loop()``，这个 loop
  就是宿主 loop——``turn_dispatch``、附件下载、出站上传都在它上面。
- **传输线程（SDK 的）**：``QQWebSocket`` 自带，处理网络/心跳/重连，入站回调经
  ``run_coroutine_threadsafe`` 派回宿主 loop。

**沙箱**（§4.11 #4）：SDK **没有**沙箱开关——``API_BASE`` 在 ``import`` 时从环境
变量 ``QQ_API_BASE`` 取值。所以必须在**导入 SDK 之前**设置它，这也是本模块不在
顶层 import ``gateway`` 的原因。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from interfaces.qq.access import OwnerTracker
from interfaces.qq.config import QQConfig, load_qq_config
from interfaces.qq.runtime import QQRuntime
from interfaces.qq.state import load_state

logger = logging.getLogger(__name__)

_host: "QQHost | None" = None
_lock = threading.Lock()


class QQHost:
    """宿主：拥有宿主 loop、runtime、gateway 与入站处理器。"""

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
            target=self._thread_main, name="qq-interface", daemon=True
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
            logger.exception("[qq/service] 启动失败")
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
                logger.exception("[qq/service] 关闭失败")
            loop.close()

    async def _async_start(self) -> None:
        from interfaces.qq.gateway import QQGateway, sdk_available, sdk_import_error

        if not sdk_available():
            logger.error(
                f"[qq/service] 未安装 qqbot-agent-sdk，QQ 接入端不启动：{sdk_import_error()}"
            )
            return

        state = load_state()
        self.runtime = QQRuntime(cfg=self.cfg, state=state, loop=asyncio.get_running_loop())
        owner = OwnerTracker(state)

        from interfaces.qq.inbound import InboundHandler

        self.handler = InboundHandler(self.runtime, owner)
        self.gateway = QQGateway(self.runtime, self.handler.handle, app_id=self.cfg.app_id)
        try:
            await self.gateway.start()
        except Exception:
            logger.exception("[qq/service] 网关启动失败（凭据/网络问题？）")

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
        logger.info("[qq/service] QQ 接入端已停止")


# ─── 模块级启停（幂等） ───────────────────────────────────────────────


def start_qq_interface() -> bool:
    """启动 QQ 接入端。未启用/未配置凭据时静默跳过，返回是否真的启动。"""
    global _host
    with _lock:
        if _host is not None:
            return True

        cfg = load_qq_config()
        if not cfg.enabled:
            logger.debug("[qq/service] 未启用（setting.json 的 qq.enabled=false）")
            return False
        if not cfg.is_ready():
            logger.warning("[qq/service] 已启用但 app_id/secret 未配置，跳过启动")
            return False

        # ⚠️ 环境变量必须在 import SDK **之前**设置：API_BASE 在模块导入时求值。
        # 之后再由 apply_api_base 给模块里那份按值导入的副本打补丁（运行时切换用）。
        from interfaces.qq.gateway import (
            PROD_API_BASE,
            SANDBOX_API_BASE,
            apply_api_base,
            effective_api_base,
        )

        os.environ["QQ_API_BASE"] = SANDBOX_API_BASE if cfg.use_sandbox else PROD_API_BASE
        apply_api_base(cfg.use_sandbox)
        logger.info(f"[qq/service] API 域名：{effective_api_base()}")

        host = QQHost(cfg)
        host.start()
        _host = host
        return True


def stop_qq_interface() -> None:
    """停止 QQ 接入端（幂等）。"""
    global _host
    with _lock:
        host, _host = _host, None
    if host is not None:
        host.stop()


def qq_interface_running() -> bool:
    return _host is not None
