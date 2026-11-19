"""IMAP 拉取 + SMTP 发送（stdlib ``imaplib`` / ``smtplib``，无第三方依赖）。

**所有方法都是阻塞的**——调用方（``poller`` / ``outbound``）负责放进宿主 loop 的
executor 里跑。

拉取分两步：先取头部与 ``RFC822.SIZE``，过滤闸（``filter.check_message``）只靠
头部就能拦掉绝大多数垃圾；只有放行的邮件才下载全文（``BODY.PEEK[]``）。
``BODY.PEEK`` 保证**不置 \\Seen**——cili 对用户邮箱的副作用为零，它只是「在旁边看」。
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage, Message
from typing import Callable

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30.0
_SIZE_RE = re.compile(r"RFC822\.SIZE\s+(\d+)")
_UIDNEXT_RE = re.compile(r"UIDNEXT\s+(\d+)")


class EmailClientError(Exception):
    """IMAP/SMTP 操作失败（连接、协议、服务端错误）。"""


class EmailAuthError(EmailClientError):
    """登录/认证失败（密码或授权码错误）。"""


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


class EmailClient:
    """一个邮箱账号的 IMAP/SMTP 客户端（每次操作新建连接，无长连接状态机）。"""

    def __init__(self, cfg) -> None:
        self._cfg = cfg

    # ── IMAP ────────────────────────────────────────────────────────

    def poll(
        self,
        *,
        last_uid: int | None,
        uidvalidity: int,
        max_messages: int,
        decide: Callable[[Message, int], str | None],
    ) -> PollResult:
        """扫描 INBOX 中 UID > last_uid 的邮件（最多 ``max_messages`` 封）。

        ``decide(headers_msg, size)`` 返回拦截原因或 None；只有放行的邮件才下载
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
                reason = decide(head_msg, size)
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
            _quiet_logout(imap)

    # ── SMTP ────────────────────────────────────────────────────────

    def send(self, msg: EmailMessage) -> None:
        """发送一封邮件（连接 → 登录 → 发送 → 断开）。"""
        try:
            smtp = self._connect_smtp()
        except smtplib.SMTPAuthenticationError as e:
            raise EmailAuthError(f"SMTP 认证失败：{e}") from e
        except (OSError, ssl.SSLError, smtplib.SMTPException) as e:
            raise EmailClientError(f"SMTP 连接失败：{e}") from e
        try:
            smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            raise EmailAuthError(f"SMTP 认证失败：{e}") from e
        except (OSError, smtplib.SMTPException) as e:
            raise EmailClientError(f"SMTP 发送失败：{e}") from e
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001 — 关闭失败不影响已发送事实
                pass

    def test(self) -> dict:
        """连接测试：IMAP 登录 + SELECT INBOX、SMTP 登录认证（**不发信**）。"""
        result = {"ok": False, "imap": "", "smtp": "", "error": ""}
        try:
            imap = self._connect_imap()
            try:
                uidvalidity = self._select_inbox(imap)
                uidnext = self._uidnext(imap)
                result["imap"] = f"OK（UIDVALIDITY {uidvalidity}，UIDNEXT {uidnext}）"
            finally:
                _quiet_logout(imap)
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as e:
            result["imap"] = f"失败：{e}"
            result["error"] = "IMAP 连接失败"
            return result
        try:
            smtp = self._connect_smtp()
            try:
                result["smtp"] = "OK（认证通过，未发送测试邮件）"
            finally:
                try:
                    smtp.quit()
                except Exception:  # noqa: BLE001
                    pass
        except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
            result["smtp"] = f"失败：{e}"
            result["error"] = "SMTP 连接失败"
            return result
        result["ok"] = True
        return result

    # ── 连接与协议细节 ──────────────────────────────────────────────

    def _connect_imap(self) -> imaplib.IMAP4:
        ctx = ssl.create_default_context()
        if self._cfg.imap_ssl:
            imap = imaplib.IMAP4_SSL(
                self._cfg.imap_host, self._cfg.imap_port,
                ssl_context=ctx, timeout=_CONNECT_TIMEOUT,
            )
        else:
            imap = imaplib.IMAP4(
                self._cfg.imap_host, self._cfg.imap_port, timeout=_CONNECT_TIMEOUT,
            )
            imap.starttls(ssl_context=ctx)
        imap.login(self._cfg.username, self._cfg.password)
        return imap

    def _connect_smtp(self) -> smtplib.SMTP:
        ctx = ssl.create_default_context()
        if self._cfg.smtp_security == "starttls":
            smtp: smtplib.SMTP = smtplib.SMTP(
                self._cfg.smtp_host, self._cfg.smtp_port, timeout=_CONNECT_TIMEOUT,
            )
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.ehlo()
        else:
            smtp = smtplib.SMTP_SSL(
                self._cfg.smtp_host, self._cfg.smtp_port,
                context=ctx, timeout=_CONNECT_TIMEOUT,
            )
        smtp.login(self._cfg.username, self._cfg.password)
        return smtp

    @staticmethod
    def _select_inbox(imap: imaplib.IMAP4) -> int:
        typ, data = imap.select("INBOX", readonly=True)
        if typ != "OK":
            raise EmailClientError(f"无法打开 INBOX：{data}")
        resp = imap.response("UIDVALIDITY")
        try:
            return int(resp[1][0]) if resp and resp[1] else 0
        except (TypeError, ValueError, IndexError):
            return 0

    @staticmethod
    def _uidnext(imap: imaplib.IMAP4) -> int:
        typ, data = imap.status("INBOX", "(UIDNEXT)")
        if typ != "OK" or not data:
            return 0
        match = _UIDNEXT_RE.search(
            data[0].decode("utf-8", "replace") if isinstance(data[0], bytes) else str(data[0])
        )
        return int(match.group(1)) if match else 0

    @staticmethod
    def _search_after(imap: imaplib.IMAP4, last_uid: int) -> list[int]:
        typ, data = imap.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        if typ != "OK" or not data or not data[0]:
            return []
        try:
            uids = sorted(int(x) for x in data[0].split())
        except ValueError:
            return []
        # "UID 1000:*" 在服务端最大 UID 小于 1000 时仍会返回最后一封（RFC 3501
        # 的已知行为），所以必须显式过滤
        return [u for u in uids if u > last_uid]

    @staticmethod
    def _fetch_header(imap: imaplib.IMAP4, uid: int) -> tuple[int, Message] | None:
        typ, data = imap.uid("FETCH", str(uid), "(RFC822.SIZE BODY.PEEK[HEADER])")
        if typ != "OK" or not data:
            return None
        meta_bytes, header_bytes = None, b""
        for part in data:
            if isinstance(part, tuple):
                meta_bytes, header_bytes = part
                break
        if meta_bytes is None:
            return None
        meta = meta_bytes.decode("utf-8", "replace") if isinstance(meta_bytes, bytes) else str(meta_bytes)
        match = _SIZE_RE.search(meta)
        size = int(match.group(1)) if match else 0
        return size, email.message_from_bytes(
            header_bytes or b"", policy=email.policy.default
        )

    @staticmethod
    def _fetch_raw(imap: imaplib.IMAP4, uid: int) -> bytes | None:
        typ, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data:
            return None
        for part in data:
            if isinstance(part, tuple):
                return part[1]
        return None

    @staticmethod
    def _map_imap_error(err: imaplib.IMAP4.error) -> EmailClientError:
        text = str(err)
        lowered = text.lower()
        if "auth" in lowered or "login" in lowered or "invalid" in lowered \
                or "credential" in lowered:
            return EmailAuthError(f"IMAP 认证失败：{text}")
        return EmailClientError(f"IMAP 操作失败：{text}")


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


def _quiet_logout(imap: imaplib.IMAP4) -> None:
    try:
        imap.logout()
    except Exception:  # noqa: BLE001 — 关闭失败不影响本轮结果
        pass
