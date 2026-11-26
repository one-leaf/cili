"""邮件接入端的 IMAP/SMTP 客户端：IMAP 协议细节在 ``core.mail.client``。

本模块只保留接入端特有的部分——轮询流程（「UID 大于游标」的增量扫描、过滤闸
回调、拦截原因）与其数据结构 ``MailItem`` / ``PollResult``。SMTP 发送、IMAP 只读
查询等原语由 ``MailClient`` 提供，``mail`` 工具也直接使用同一层。

``BODY.PEEK`` 保证**不置 \\Seen**——cili 对用户邮箱的副作用为零，它只是「在旁边看」。
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import ssl
from dataclasses import dataclass, field
from email.message import Message
from typing import Callable

from core.mail.client import (
    EmailAuthError,
    EmailClientError,
    MailClient,
    quiet_logout,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EmailAuthError",
    "EmailClient",
    "EmailClientError",
    "MailItem",
    "PollResult",
]


@dataclass
class MailItem:
    """一封已扫描的邮件：被拦截的有 ``reason``，放行的带完整 ``message``。"""

    uid: int
    sender: str = ""
    subject: str = ""
    message_id: str = ""
    references: str = ""
    size: int = 0
    reason: str | None = None            # 拦截原因；None = 放行
    message: Message | None = None       # 放行邮件的完整解析结果


@dataclass
class PollResult:
    """一次轮询的结果。"""

    uidvalidity: int = 0
    last_uid: int = 0
    reset: bool = False                  # 首次启动 / UIDVALIDITY 变化：游标已重置
    uidnext: int = 0
    items: list[MailItem] = field(default_factory=list)


class EmailClient(MailClient):
    """接入端客户端：在 ``MailClient`` 上补轮询流程。"""

    def poll(
        self,
        *,
        last_uid: int | None,
        uidvalidity: int,
        max_messages: int,
        classify: Callable[[Message, int], str | None],
    ) -> PollResult:
        """扫描 INBOX 中 UID > last_uid 的邮件（最多 ``max_messages`` 封）。

        ``classify(headers_msg, size)`` 返回拦截原因或 None；只有放行的邮件才下载
        全文。``last_uid is None`` 或 ``uidvalidity`` 与服务端不一致时**跳过历史**：
        游标重置到当前 UIDNEXT - 1，不重放旧邮件。
        """
        try:
            imap = self._connect_imap()
        except imaplib.IMAP4.error as e:
            raise self._map_imap_error(e) from e
        except (OSError, ssl.SSLError) as e:
            raise EmailClientError(f"IMAP 连接失败：{e}") from e
        try:
            current_uidvalidity = self._select_inbox(imap)
            uidnext = self._uidnext(imap)

            if last_uid is None or (uidvalidity and uidvalidity != current_uidvalidity):
                return PollResult(
                    uidvalidity=current_uidvalidity,
                    last_uid=max(0, uidnext - 1),
                    reset=True,
                    uidnext=uidnext,
                )

            uids = self._search_after(imap, last_uid)[:max_messages]
            items: list[MailItem] = []
            scanned_last = last_uid
            for uid in uids:
                header = self._fetch_header(imap, uid)
                scanned_last = uid
                if header is None:
                    items.append(MailItem(uid=uid, reason="fetch_failed"))
                    continue
                size, head_msg = header
                item = _item_from_headers(uid, size, head_msg)
                reason = classify(head_msg, size)
                if reason:
                    item.reason = reason
                    items.append(item)
                    continue
                raw = self._fetch_raw(imap, uid)
                if raw is None:
                    item.reason = "fetch_failed"
                    items.append(item)
                    continue
                item.message = email.message_from_bytes(
                    raw, policy=email.policy.default
                )
                items.append(item)
            return PollResult(
                uidvalidity=current_uidvalidity,
                last_uid=scanned_last,
                uidnext=uidnext,
                items=items,
            )
        except imaplib.IMAP4.error as e:
            raise self._map_imap_error(e) from e
        except (OSError, ssl.SSLError) as e:
            raise EmailClientError(f"IMAP 连接失败：{e}") from e
        finally:
            quiet_logout(imap)


def _item_from_headers(uid: int, size: int, msg: Message) -> MailItem:
    from interfaces.email.filter import sender_address

    return MailItem(
        uid=uid,
        sender=sender_address(msg),
        subject=str(msg.get("Subject") or ""),
        message_id=str(msg.get("Message-ID") or ""),
        references=str(msg.get("References") or ""),
        size=size,
    )