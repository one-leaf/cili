"""微信接入端生命周期：宿主线程 + 宿主 loop + 启停。

**线程模型**（与 QQ 一致）：起一个守护线程跑 ``asyncio.new_event_loop()``，这个 loop
就是宿主 loop——长轮询、入站回合、出站发送都在它上面。iLink 没有 SDK，所以**没有**
第二个传输线程（QQ 那侧的 WebSocket 线程由 SDK 自带，我们这里就是自己的 httpx）。

**没有沙箱开关**：iLink 是纯 HTTP，端点由配置的 ``base_url`` 决定（登录时服务端可能
把接入点改到别的机房，那个值记在 ``account.json`` 里、启动时优先生效）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import suppress

from interfaces.wechat.access import OwnerTracker
from interfaces.wechat.account import ContextTokenStore, load_account
from interfaces.wechat.config import WechatConfig, load_wechat_config
from interfaces.wechat.protocol import WechatAuthError
from interfaces.wechat.state import load_state

logger = logging.getLogger(__name__)

_host: "WechatHost | None" = None
_lock = threading.Lock()


class WechatHost:
    """宿主：拥有宿主 loop、runtime、长轮询任务与入站处理器。"""

    def __init__(self, cfg: WechatConfig) -> None:
        self.cfg = cfg
        self.loop: asyncio.AbstractEventLoop | None = None
        self.runtime = None
        self.poller = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._poll_task: asyncio.Task[None] | None = None

    # ── 启动 ────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._thread_main, name="wechat-interface", daemon=True
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
            logger.exception("[wechat/service] 启动失败")
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
                logger.exception("[wechat/service] 关闭失败")
            loop.close()

    async def _async_start(self) -> None:
        from interfaces.wechat.client import WechatClient
        from interfaces.wechat.inbound import InboundHandler
        from interfaces.wechat.outbound import OutboundSender
        from interfaces.wechat.poller import Poller
        from interfaces.wechat.typing import TypingManager

        account = load_account()
        if not account.has_token():
            logger.warning("[wechat/service] 尚未扫码登录，微信接入端不启动")
            return

        # 登录时服务端可能把接入点换到别的机房，以账号里的为准
        if account.base_url:
            self.cfg.base_url = account.base_url

        client = WechatClient(self.cfg, token=account.bot_token)
        tokens = ContextTokenStore(
            max_age_seconds=self.cfg.context_token_max_age_seconds,
            budget=self.cfg.context_message_budget,
        )
        runtime = _build_runtime(
            cfg=self.cfg,
            account=account,
            tokens=tokens,
            client=client,
            loop=asyncio.get_running_loop(),
        )
        runtime.typing = TypingManager(self.cfg, client)
        runtime.outbound = OutboundSender(self.cfg, client, tokens)

        owner = OwnerTracker(runtime.state)
        handler = InboundHandler(runtime, owner)
        poller = Poller(runtime, handler)

        self.runtime = runtime
        self.poller = poller
        self._poll_task = asyncio.create_task(self._run_poller(poller))
        logger.info("[wechat/service] 接入端已启动，开始长轮询")

    async def _run_poller(self, poller) -> None:
        """跑长轮询；登录失效时给出可操作的提示而不是抛栈。"""
        try:
            await poller.run()
        except WechatAuthError:
            logger.error("[wechat/service] 登录已失效，请到设置页重新扫码登录")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[wechat/service] 长轮询异常退出")

    async def _async_stop(self) -> None:
        if self.poller is not None:
            with suppress(Exception):
                await self.poller.stop()
        if self._poll_task is not None:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._poll_task
            self._poll_task = None
        runtime = self.runtime
        if runtime is not None:
            if runtime.typing is not None:
                with suppress(Exception):
                    await runtime.typing.stop_all()
            if runtime.client is not None:
                with suppress(Exception):
                    await runtime.client.aclose()
        self.runtime = None
        self.poller = None

    # ── 停止 ────────────────────────────────────────────────────────

    def stop(self) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10.0)
        logger.info("[wechat/service] 微信接入端已停止")


def _build_runtime(*, cfg, account, tokens, client, loop):
    """装配运行时容器（单独一层便于测试替换 client）。"""
    from interfaces.wechat.runtime import WechatRuntime

    return WechatRuntime(
        cfg=cfg,
        state=load_state(),
        account=account,
        tokens=tokens,
        loop=loop,
        client=client,
    )


# ─── 模块级启停（幂等） ───────────────────────────────────────────────


def start_wechat_interface() -> bool:
    """启动微信接入端。未启用/未登录时静默跳过，返回是否真的启动。"""
    global _host
    with _lock:
        if _host is not None:
            return True

        cfg = load_wechat_config()
        if not cfg.enabled:
            logger.debug("[wechat/service] 未启用（setting.json 的 wechat.enabled=false）")
            return False
        if not cfg.is_ready():
            logger.warning("[wechat/service] 已启用但 base_url 未配置，跳过启动")
            return False
        if not load_account().has_token():
            logger.warning("[wechat/service] 已启用但尚未扫码登录，跳过启动")
            return False

        host = WechatHost(cfg)
        host.start()
        _host = host
        return True


def stop_wechat_interface() -> None:
    """停止微信接入端（幂等）。"""
    global _host
    with _lock:
        host, _host = _host, None
    if host is not None:
        host.stop()


def wechat_interface_running() -> bool:
    return _host is not None
