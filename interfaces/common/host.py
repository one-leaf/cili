"""接入端宿主线程通用骨架：loop 生命周期 + 就绪/存活判定。

QQ 与微信的宿主线程只有线程名前缀、日志标签和 `_async_start/_async_stop`
实现不同；启停与异常收敛逻辑完全一致，统一收敛在这里。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class HostBase:
    """一个账号的宿主：独立 event loop + 守护线程 + ready 生命周期。"""

    _thread_prefix = "host"
    _log_tag = "interface"

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self.ready = False

    def start(self) -> bool:
        """启动宿主线程；等待 loop 就绪（最多 5s），返回是否真正可用。"""
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"{self._thread_prefix}-{self.cfg.id}",
            daemon=True,
        )
        self._thread.start()
        self._started.wait(timeout=5.0)
        return self.is_alive()

    def is_alive(self) -> bool:
        """宿主线程存活且启动成功（未就绪/已退出都返回 False）。"""
        return self.ready and self._thread is not None and self._thread.is_alive()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            loop.run_until_complete(self._async_start())
        except Exception:
            logger.exception(f"[{self._log_tag}/service] 账号 {self.cfg.id} 启动失败")
            self._started.set()
            loop.close()
            return

        self._started.set()
        if not self.ready:
            try:
                loop.run_until_complete(self._async_stop())
            except Exception:
                pass
            loop.close()
            return
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(self._async_stop())
            except Exception:  # noqa: BLE001
                logger.exception(f"[{self._log_tag}/service] 账号 {self.cfg.id} 关闭失败")
            loop.close()

    def stop(self) -> None:
        self.ready = False
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10.0)
        logger.info(f"[{self._log_tag}/service] 账号 {self.cfg.id} 已停止")

    async def _async_start(self) -> None:
        raise NotImplementedError

    async def _async_stop(self) -> None:
        raise NotImplementedError
