"""出站发送：context_token 的刷新、发送预算、defer 队列。

**配额模型与 QQ 完全不同**：QQ 是「每条用户消息 4 次被动回复」；iLink 是「每个
context_token 约 8 条」且 token 约 90–160s 就失效。所以这里做三件事：

1. 发之前看 token 年龄，老到阈值就先 ``getconfig`` 换一个新的
2. **本地**预算兜底（超了直接拒，不打 API）——等 API 报错时那条消息已经白发一次了
3. 服务端回 ``-2``（受限/配额用尽）→ 把消息放进 defer 队列，等用户下一条消息带来
   新 token 时按序重放；**``client_id`` 复用**，服务端据此幂等去重
"""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass

from interfaces.wechat.account import ContextTokenStore
from interfaces.wechat.client import WechatClient
from interfaces.wechat.config import WechatConfig
from interfaces.wechat.protocol import ITEM_TEXT, WechatQuotaError, build_message

logger = logging.getLogger(__name__)


def new_client_id() -> str:
    """生成一条消息的 ``client_id``。

    重试/重放时**必须复用同一个**——服务端据此把重复投递识别为同一条消息。
    """
    return f"cili-{uuid.uuid4().hex[:12]}"


@dataclass
class _Pending:
    """一条因配额被推迟的回复。"""

    user_id: str
    text: str
    client_id: str


def _text_of(item: dict) -> str:
    """从 item 里取文本。

    只有文本项会进 defer 队列——重放的是文本，媒体项不走 defer（见
    ``send_media_item``）。
    """
    text_item = item.get("text_item")
    if not isinstance(text_item, dict):
        return ""
    return str(text_item.get("text") or "")


class OutboundSender:
    """按用户发文本：管 token 刷新、预算与 defer。"""

    def __init__(
        self,
        cfg: WechatConfig,
        client: WechatClient,
        tokens: ContextTokenStore,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._tokens = tokens
        self._deferred: dict[str, OrderedDict[str, _Pending]] = {}

    # ── 发送 ────────────────────────────────────────────────────────

    async def send_text(
        self,
        user_id: str,
        text: str,
        *,
        client_id: str = "",
        reserve: int = 0,
    ) -> bool:
        """发一条文本。返回是否真的发出去了。

        配额不足或服务端受限时返回 False，并把这条转入 defer（不丢内容）。
        """
        if not text:
            return True
        item = {"type": ITEM_TEXT, "text_item": {"text": text}}
        return await self._send_item(
            user_id, item, client_id=client_id, reserve=reserve, defer=True,
        )

    async def send_media_item(
        self,
        user_id: str,
        item: dict,
        *,
        client_id: str = "",
        reserve: int = 0,
    ) -> bool:
        """发一个媒体项。

        **不进 defer 队列**：defer 存的是文本、重放的是文本；出站文件数量少且由
        outbox 侧的预算检查把关，发不出去时直接告诉用户更清楚。
        """
        return await self._send_item(
            user_id, item, client_id=client_id, reserve=reserve, defer=False,
        )

    async def _send_item(
        self,
        user_id: str,
        item: dict,
        *,
        client_id: str = "",
        reserve: int = 0,
        defer: bool = True,
    ) -> bool:
        client_id = client_id or new_client_id()

        context_token = await self._fresh_token(user_id)
        if not context_token:
            logger.warning(
                f"[wechat/outbound] 没有可用的 context_token，放弃发送给 {user_id}"
            )
            return False

        if not self._tokens.can_send(user_id, reserve=reserve):
            logger.warning(
                f"[wechat/outbound] 发送预算已用尽（reserve={reserve}）：{user_id}"
            )
            if defer:
                self._defer(_Pending(user_id, _text_of(item), client_id))
            return False

        msg = build_message(
            to_user_id=user_id,
            item_list=[item],
            context_token=context_token,
            client_id=client_id,
        )
        try:
            await self._client.send_message(msg)
        except WechatQuotaError:
            # 服务端说这个 context 受限 → 等用户下一条消息带来的新 token
            logger.warning(f"[wechat/outbound] 服务端受限（-2）：{user_id}")
            if defer:
                self._defer(_Pending(user_id, _text_of(item), client_id))
            return False

        self._tokens.record_send(user_id)
        return True

    # ── 供 media.py 使用的只读入口 ──────────────────────────────────

    @property
    def cfg(self) -> WechatConfig:
        return self._cfg

    @property
    def client(self) -> WechatClient:
        return self._client

    def budget_left(self, user_id: str, *, reserve: int = 0) -> int:
        """还剩几条可发（``reserve`` 是留给后续步骤的余量）。"""
        return self._tokens.budget_left(user_id, reserve=reserve)

    # ── token 刷新 ──────────────────────────────────────────────────

    async def _fresh_token(self, user_id: str) -> str:
        """取当前可用的 context_token；太老就先刷一次。"""
        token = self._tokens.token(user_id)
        if not token:
            return ""
        if not self._tokens.needs_refresh(user_id):
            return token
        try:
            data = await self._client.get_config(user_id, token)
        except Exception as e:  # noqa: BLE001 — 刷新失败就先用旧的，也许还能用
            logger.warning(f"[wechat/outbound] 刷新 context_token 失败：{e}")
            return token
        new_token = str(data.get("context_token", "") or "")
        self._tokens.mark_refreshed(user_id, new_token or token)
        return new_token or token

    # ── defer 队列 ──────────────────────────────────────────────────

    def _defer(self, pending: _Pending) -> None:
        queue = self._deferred.setdefault(pending.user_id, OrderedDict())
        queue[pending.client_id] = pending
        queue.move_to_end(pending.client_id)
        while len(queue) > self._cfg.defer_queue_max:
            queue.popitem(last=False)
            logger.warning(
                f"[wechat/outbound] defer 队列超过 {self._cfg.defer_queue_max} 条，丢弃最旧的一条"
            )

    def deferred_count(self, user_id: str) -> int:
        return len(self._deferred.get(user_id) or ())

    async def replay_deferred(self, user_id: str) -> None:
        """用户又说话了（带来新 context_token）→ 把之前没发出去的补上。"""
        queue = self._deferred.get(user_id)
        if not queue:
            return
        logger.info(f"[wechat/outbound] 重放 {len(queue)} 条被推迟的回复：{user_id}")
        for client_id, pending in list(queue.items()):
            if not self._tokens.can_send(user_id):
                break
            ok = await self.send_text(
                user_id, pending.text, client_id=client_id,
            )
            if not ok:
                break
            queue.pop(client_id, None)
        if not queue:
            self._deferred.pop(user_id, None)

    def drop_deferred(self, user_id: str) -> None:
        """清掉某用户的 defer 队列（登出/重启时用）。"""
        self._deferred.pop(user_id, None)
