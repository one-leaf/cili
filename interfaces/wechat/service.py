"""微信接入端生命周期：每个账号一个宿主线程 + 宿主 loop。

**线程模型**（与 QQ 一致）：每个账号起一个守护线程跑
``asyncio.new_event_loop()``，这个 loop 就是宿主 loop——长轮询、入站回合、
出站发送都在它上面。iLink 没有 SDK，所以**没有**第二个传输线程。

**多账号**（设计 §5.3）：``_hosts`` 按 account_id 持有宿主；start / stop /
running 的 ``account_id`` 参数为 None 时作用于**全部**账号。

**没有沙箱开关**：iLink 是纯 HTTP，端点由配置的 ``base_url`` 决定（登录时
服务端可能把接入点改到别的机房，那个值记在账号凭证文件里、启动时优先生效）。
每个 host 持有**独立的 cfg 实例**，这里的改写不会串到别的账号。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import suppress

from interfaces.wechat.access import OwnerTracker
from interfaces.wechat.account import ContextTokenStore, load_account
from interfaces.wechat.config import WechatConfig, load_wechat_accounts
from interfaces.wechat.protocol import WechatAuthError
from interfaces.wechat.state import load_account_state

logger = logging.getLogger(__name__)

_hosts: dict[str, "WechatHost"] = {}
_lock = threading.Lock()


class WechatHost:
    """一个账号的宿主：拥有宿主 loop、runtime、长轮询任务与入站处理器。"""

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
            target=self._thread_main, name=f"wechat-{self.cfg.id}", daemon=True
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
            logger.exception(f"[wechat/service] 账号 {self.cfg.id} 启动失败")
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
                logger.exception(f"[wechat/service] 账号 {self.cfg.id} 关闭失败")
            loop.close()

    async def _async_start(self) -> None:
        from interfaces.wechat.client import WechatClient
        from interfaces.wechat.inbound import InboundHandler
        from interfaces.wechat.outbound import OutboundSender
        from interfaces.wechat.poller import Poller
        from interfaces.wechat.typing import TypingManager

        account = load_account(self.cfg.id)
        if not account.has_token():
            logger.warning(
                f"[wechat/service] 账号 {self.cfg.label or self.cfg.id} 尚未扫码登录，微信接入端不启动"
            )
            return

        # 登录时服务端可能把接入点换到别的机房，以账号凭证里的为准。
        # cfg 是本账号独立实例（§3.4），改写不会串到别的账号。
        if account.base_url:
            self.cfg.base_url = account.base_url

        client = WechatClient(self.cfg, token=account.bot_token)
        tokens = ContextTokenStore(
            max_age_seconds=self.cfg.context_token_max_age_seconds,
            budget=self.cfg.context_message_budget,
        )
        runtime = _build_runtime(
            account_id=self.cfg.id,
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
        logger.info(f"[wechat/service] 账号 {self.cfg.label or self.cfg.id} 已启动，开始长轮询")

    async def _run_poller(self, poller) -> None:
        """跑长轮询；登录失效时给出可操作的提示而不是抛栈。"""
        try:
            await poller.run()
        except WechatAuthError:
            logger.error(
                f"[wechat/service] 账号 {self.cfg.id} 登录已失效，请到设置页重新扫码登录"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"[wechat/service] 账号 {self.cfg.id} 长轮询异常退出")

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
        logger.info(f"[wechat/service] 账号 {self.cfg.id} 已停止")


def _build_runtime(*, account_id, cfg, account, tokens, client, loop):
    """装配运行时容器（单独一层便于测试替换 client）。"""
    from interfaces.wechat.runtime import WechatRuntime

    return WechatRuntime(
        account_id=account_id,
        cfg=cfg,
        state=load_account_state(account_id),
        account=account,
        tokens=tokens,
        loop=loop,
        client=client,
    )


# ─── 模块级启停（幂等；account_id 为 None 时作用于全部账号） ──────────


def start_wechat_interface(account_id: str | None = None) -> bool:
    """启动微信接入端：启动所有（或指定）enabled 且已扫码登录的账号。

    返回是否至少启动了一个账号。未启用 / 未登录 / 已在跑的账号静默跳过。
    """
    started: list[str] = []
    with _lock:
        for cfg in load_wechat_accounts():
            if account_id is not None and cfg.id != account_id:
                continue
            if not cfg.enabled:
                continue
            if not load_account(cfg.id).has_token():
                logger.warning(
                    f"[wechat/service] 账号 {cfg.label or cfg.id} 已启用但尚未扫码登录，跳过"
                )
                continue
            if cfg.id in _hosts:
                continue          # 已在跑（幂等）
            host = WechatHost(cfg)
            host.start()
            _hosts[cfg.id] = host
            started.append(cfg.id)
    if started:
        logger.info(f"[wechat/service] 已启动 {len(started)} 个账号：{started}")
    elif account_id is None:
        accounts = load_wechat_accounts()
        if not accounts:
            logger.debug("[wechat/service] wechat 段无账号，跳过启动")
        elif not any(a.enabled for a in accounts):
            logger.debug("[wechat/service] 无 enabled 账号，跳过启动")
    return bool(started)


def stop_wechat_interface(account_id: str | None = None) -> None:
    """停止微信接入端（幂等）。account_id 为 None 时停止全部。"""
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
            logger.exception(f"[wechat/service] 停止账号 {hid} 失败")


def wechat_interface_running(account_id: str | None = None) -> bool:
    """是否有账号在跑。account_id 为 None 时指「任一账号」。"""
    if account_id is not None:
        return account_id in _hosts
    return bool(_hosts)
