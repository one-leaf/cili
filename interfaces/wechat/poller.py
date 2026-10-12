"""长轮询：收消息 → 交给入站处理器。

**与 QQ 的差别**：QQ 的传输层由 SDK 提供（WebSocket + 心跳 + 重连），我们只接回调；
iLink 是 HTTP 长轮询（服务端 hold ≤35s），循环得自己写：

- 每轮带 ``get_updates_buf`` 游标——丢了会把服务端积压的消息重收一遍，用户看到重复回复
- 连续失败 3 次退避 30s；单次失败只等 2s（长轮询超时是常态，不算故障）
- ``-14``（token 失效）→ 停止轮询并向上抛，由服务层提示重新扫码

过滤链的前三步也在这里（``from_bot`` / 群聊 / 去重）——它们不需要会话上下文，
放在轮询层能最早丢掉无用消息；owner 绑定与回合驱动在 ``inbound.py``。
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from interfaces.wechat.account import save_account
from interfaces.wechat.protocol import WechatAuthError, parse_inbound
from interfaces.wechat.runtime import WechatRuntime

if TYPE_CHECKING:
    from interfaces.wechat.inbound import InboundHandler

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 3
BACKOFF_DELAY_S = 30.0
RETRY_DELAY_S = 2.0
DEDUPE_LRU_SIZE = 1000


class Poller:
    """长轮询循环（跑在宿主 loop 上）。"""

    def __init__(self, runtime: WechatRuntime, handler: "InboundHandler") -> None:
        self._rt = runtime
        self._handler = handler
        self._running = False
        self._consecutive_failures = 0
        # 去重：服务端可能重发同一条消息（游标回退、重连）
        self._processed_ids: OrderedDict[str, None] = OrderedDict()

    # ── 主循环 ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """一直轮询到 ``stop()`` 或登录失效。"""
        self._running = True
        await self._notify("start")
        logger.info("[wechat/poller] 开始长轮询")

        try:
            while self._running:
                try:
                    await self._poll_once()
                    self._consecutive_failures = 0
                except asyncio.CancelledError:
                    raise
                except WechatAuthError:
                    # 登录失效：轮询没法继续，交给上层去提示重新扫码
                    logger.error("[wechat/poller] 登录已失效，停止轮询")
                    self._running = False
                    raise
                except Exception as e:  # noqa: BLE001 — 任何异常都不该让轮询线程死掉
                    if not self._running:
                        break
                    self._consecutive_failures += 1
                    delay = (
                        BACKOFF_DELAY_S
                        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                        else RETRY_DELAY_S
                    )
                    logger.warning(
                        f"[wechat/poller] 轮询失败（第 {self._consecutive_failures} 次），"
                        f"{delay:.0f}s 后重试：{e}"
                    )
                    await asyncio.sleep(delay)
        finally:
            await self._notify("stop")

    async def stop(self) -> None:
        """请求停止（循环会在下一次检查时退出）。"""
        self._running = False

    # ── 单轮 ────────────────────────────────────────────────────────

    async def _poll_once(self) -> None:
        rt = self._rt
        if rt.client is None:
            raise RuntimeError("HTTP 客户端未就绪")

        data = await rt.client.get_updates(rt.account.get_updates_buf)

        # 游标必须落盘：丢了重启后会把积压消息重收一遍
        new_cursor = str(data.get("get_updates_buf", "") or "")
        if new_cursor and new_cursor != rt.account.get_updates_buf:
            rt.account.get_updates_buf = new_cursor
            save_account(rt.account)

        for msg in data.get("msgs") or []:
            if not isinstance(msg, dict):
                continue
            await self._dispatch(msg)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        inbound = parse_inbound(msg)

        if inbound.from_bot:
            return                       # 自己发的
        if inbound.is_group:
            return                       # 群聊（本次范围仅私聊）
        if not inbound.from_user_id:
            return
        if self._seen(inbound.message_id):
            return

        try:
            await self._handler.handle(inbound)
        except Exception:  # noqa: BLE001 — 单条消息失败不该影响后续
            logger.exception("[wechat/poller] 处理入站消息失败")

    def _seen(self, message_id: str) -> bool:
        """去重：同一 message_id 只处理一次（LRU 上限 ``DEDUPE_LRU_SIZE``）。"""
        if not message_id:
            return False
        if message_id in self._processed_ids:
            return True
        self._processed_ids[message_id] = None
        while len(self._processed_ids) > DEDUPE_LRU_SIZE:
            self._processed_ids.popitem(last=False)
        return False

    async def _notify(self, action: str) -> None:
        """上下线通知（best-effort，失败只记日志）。"""
        client = self._rt.client
        if client is None:
            return
        with suppress(Exception):
            await client.notify(action)

    # ── 状态（供 /status 与测试） ───────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def seen_count(self) -> int:
        return len(self._processed_ids)

    def mark_seen(self, message_id: str) -> bool:
        """测试/外部注入用：见 ``_seen``。"""
        return self._seen(message_id)


__all__ = ["Poller", "MAX_CONSECUTIVE_FAILURES", "BACKOFF_DELAY_S", "RETRY_DELAY_S"]
