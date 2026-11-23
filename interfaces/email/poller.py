"""轮询循环：游标推进、UIDVALIDITY 处理、批量上限与拦截统计。

每轮（默认 60s）：

1. 读游标（文件不存在 = 首次启动 → 跳过历史，§3.7）
2. executor 里跑 IMAP 扫描（先头部过滤，放行的才下载全文）
3. 逐封处理：被拦截的计数；放行的交给 ``inbound``
4. **逐封推进游标**——处理到第 N 封时崩溃，重启只会从 N+1 继续

额度与去重是进程内的有状态闸门（``filter.RateLimiter`` / ``filter.SeenIds``）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial

from interfaces.email.client import EmailAuthError, EmailClientError, PollResult
from interfaces.email.filter import (
    REASON_LABELS,
    RateLimiter,
    SeenIds,
    check_headers,
    sender_address,
)
from interfaces.email.runtime import EmailRuntime
from interfaces.email.state import CursorData, load_cursor, save_cursor

logger = logging.getLogger(__name__)


class Poller:
    """一个账号的 INBOX 轮询循环（跑在宿主 loop 上，IMAP 走 executor）。"""

    def __init__(self, runtime: EmailRuntime, handler) -> None:
        self._rt = runtime
        self._handler = handler
        cfg = runtime.cfg
        self._limiter = RateLimiter(
            per_sender_per_hour=cfg.turns_per_sender_per_hour,
            per_account_per_day=cfg.turns_per_account_per_day,
        )
        self._seen = SeenIds()
        self._stopped = False

    async def run(self) -> None:
        """轮询循环（不抛异常：失败记状态，下一轮自然重试）。"""
        while not self._stopped:
            try:
                await self._poll_once()
            except EmailAuthError as e:
                self._set_error(str(e))
                logger.error(f"[email/poller] 账号 {self._rt.account_id} 认证失败：{e}")
            except EmailClientError as e:
                self._set_error(str(e))
                logger.warning(f"[email/poller] 账号 {self._rt.account_id} 轮询失败：{e}")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 循环必须活下去
                self._set_error("轮询异常")
                logger.exception(f"[email/poller] 账号 {self._rt.account_id} 轮询异常")
            await asyncio.sleep(self._rt.cfg.poll_interval_seconds)

    async def stop(self) -> None:
        self._stopped = True

    # ── 单轮 ────────────────────────────────────────────────────────

    async def _poll_once(self) -> None:
        rt = self._rt
        cfg = rt.cfg
        if rt.client is None:
            return

        cursor = load_cursor(rt.account_id)
        last_uid = cursor.last_uid if cursor is not None else None
        uidvalidity = cursor.uidvalidity if cursor is not None else 0

        loop = asyncio.get_running_loop()
        result: PollResult = await loop.run_in_executor(
            None,
            partial(
                rt.client.poll,
                last_uid=last_uid,
                uidvalidity=uidvalidity,
                max_messages=cfg.max_messages_per_poll,
                classify=self._classify,
            ),
        )

        cursor = cursor or CursorData()
        if result.reset:
            cursor.uidvalidity = result.uidvalidity
            cursor.last_uid = result.last_uid
            save_cursor(rt.account_id, cursor)
            logger.info(
                f"[email/poller] 账号 {rt.account_id} 首次启动或 UIDVALIDITY 变化，"
                f"跳过历史邮件（游标重置到 UID {result.last_uid}）"
            )
            self._set_ok(cursor)
            return

        cursor.uidvalidity = result.uidvalidity
        for item in result.items:
            if item.reason:
                self._record_ignored(cursor, item.reason, item.sender)
                logger.info(
                    f"[email/poller] 已拦截来信（{REASON_LABELS.get(item.reason, item.reason)}）："
                    f"{item.sender or '(未知发件人)'} · UID {item.uid}"
                )
            else:
                await self._handler.handle(item)
            # 逐封推进：崩溃后不重放已处理的邮件
            cursor.last_uid = max(cursor.last_uid, item.uid)
            save_cursor(rt.account_id, cursor)

        self._set_ok(cursor)

    def _classify(self, msg, size: int) -> str | None:
        """头部闸 → 去重 → 额度（在 executor 线程里执行）。"""
        cfg = self._rt.cfg
        reason = check_headers(
            msg,
            size=size,
            allowed_senders=cfg.allowed_senders,
            max_inbound_bytes=cfg.max_inbound_bytes,
        )
        if reason:
            return reason
        if self._seen.seen_or_add(str(msg.get("Message-ID") or "")):
            return "duplicate"
        if not self._limiter.allow(sender_address(msg)):
            return "rate_limited"
        return None

    # ── 观测 ────────────────────────────────────────────────────────

    def _set_ok(self, cursor: CursorData) -> None:
        self._rt.status = {
            "last_poll_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_error": "",
            "last_uid": cursor.last_uid,
            "uidvalidity": cursor.uidvalidity,
        }

    def _set_error(self, message: str) -> None:
        status = dict(self._rt.status or {})
        status["last_error"] = message
        status.setdefault("last_poll_at", "")
        self._rt.status = status

    @staticmethod
    def _record_ignored(cursor: CursorData, reason: str, sender: str) -> None:
        from interfaces.email.state import add_ignored_event

        add_ignored_event(cursor, reason, sender)
