"""微信的「正在输入…」提示。

iLink **不支持更新已发送的消息**，所以 QQ 那种「单条流式消息」在微信上做不出来。
回合内唯一的进度反馈就是 typing 状态：``sendtyping status=1`` 表示正在输入，每几秒
续一次（服务端会自己超时），回合结束 ``status=2`` 取消。

票据（``typing_ticket``）由 ``getconfig`` 下发，官方实现缓存 24 小时——这里也缓存，
并在取票失败时进入冷却（拿不到票据不该让整条回复失败，也不该把失败打成高频轮询）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import suppress

from interfaces.wechat.client import WechatClient
from interfaces.wechat.config import WechatConfig

logger = logging.getLogger(__name__)

STATUS_TYPING = 1
STATUS_CANCEL = 2

TICKET_TTL_S = 24 * 60 * 60      # 官方实现的票据缓存时长
TICKET_RETRY_S = 60.0            # 取票失败后的冷却时间


class TypingManager:
    """按用户维护 typing 票据与续期任务。"""

    def __init__(self, cfg: WechatConfig, client: WechatClient) -> None:
        self._cfg = cfg
        self._client = client
        self._tickets: dict[str, tuple[str, float]] = {}    # user -> (ticket, next_fetch_at)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # ── 票据 ────────────────────────────────────────────────────────

    async def ticket(self, user_id: str, context_token: str = "") -> str:
        """取 typing 票据（命中缓存就直接返回，失败返回旧票或空串）。"""
        now = time.time()
        cached = self._tickets.get(user_id)
        if cached and now < cached[1]:
            return cached[0]

        try:
            data = await self._client.get_config(user_id, context_token)
        except Exception as e:  # noqa: BLE001 — 拿不到票据不该影响回复
            logger.warning(f"[wechat/typing] 取 typing 票据失败：{e}")
            if cached:
                self._tickets[user_id] = (cached[0], now + TICKET_RETRY_S)
                return cached[0]
            return ""

        ticket = str(data.get("typing_ticket", "") or "")
        # TTL 上打散，避免所有用户在同一时刻集中续票
        self._tickets[user_id] = (ticket, now + random.random() * TICKET_TTL_S)
        return ticket

    # ── 输入状态 ────────────────────────────────────────────────────

    async def start(self, user_id: str, context_token: str = "") -> None:
        """开始显示「正在输入…」，并起一个续期任务。"""
        if not self._cfg.typing_enabled:
            return
        await self.stop(user_id)

        ticket = await self.ticket(user_id, context_token)
        if not ticket:
            return
        with suppress(Exception):
            await self._client.send_typing(user_id, ticket, STATUS_TYPING)
        self._tasks[user_id] = asyncio.create_task(self._keepalive(user_id, ticket))

    async def stop(self, user_id: str) -> None:
        """停掉续期任务并取消「正在输入…」。"""
        task = self._tasks.pop(user_id, None)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        ticket = (self._tickets.get(user_id) or ("", 0.0))[0]
        if ticket:
            with suppress(Exception):
                await self._client.send_typing(user_id, ticket, STATUS_CANCEL)

    async def stop_all(self) -> None:
        """停掉所有用户的续期任务（接入端关闭时用）。"""
        for user_id in list(self._tasks):
            await self.stop(user_id)

    async def _keepalive(self, user_id: str, ticket: str) -> None:
        """每 N 秒续一次——服务端会自己超时，不续就提前消失。"""
        interval = max(1.0, float(self._cfg.typing_keepalive_seconds))
        while True:
            await asyncio.sleep(interval)
            with suppress(Exception):
                await self._client.send_typing(user_id, ticket, STATUS_TYPING)
