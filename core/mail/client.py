"""IMAP / SMTP 原语（stdlib ``imaplib`` / ``smtplib``，无第三方依赖）。

**所有方法都是阻塞的**——调用方负责放进宿主 loop 的 executor 里跑。

读侧对邮箱**零副作用**：``SELECT INBOX (readonly)`` + ``BODY.PEEK``，不置 \\Seen、
不改 flag、不动接入端游标。``mail`` 工具与邮件接入端共用这一层；接入端的轮询
逻辑（游标、过滤、回合路由）留在 ``interfaces/email``。
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage, Message
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30.0
_SIZE_RE = re.compile(r"RFC822\.SIZE\s+(\d+)")
_UIDNEXT_RE = re.compile(r"UIDNEXT\s+(\d+)")

# IMAP 的日期格式固定用英文月份；strftime("%b") 受系统 locale 影响，不能用
_IMAP_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


class EmailClientError(Exception):
    """IMAP/SMTP 操作失败（连接、协议、服务端错误）。"""


class EmailAuthError(EmailClientError):
    """登录/认证失败（密码或授权码错误）。"""


@dataclass
class MailSummary:
    """列表/检索用的一封邮件摘要（只取头部）。"""

    uid: int
    sender: str = ""
    subject: str = ""
    date: str = ""
    size: int = 0


@dataclass
class ReadResult:
    """读单封的结果：``too_large`` 表示超过调用方给的上限、未下载正文。

    ``headers`` 始终带回（即使正文被跳过），调用方可以在超大邮件时展示头部。
    """

    message: Message | None = None
    headers: Message | None = None
    size: int = 0
    too_large: bool = False


@dataclass
class SearchCriteria:
    """发件人/主题子串 + 日期区间（YYYY-MM-DD）。"""

    from_text: str = ""
    subject_text: str = ""
    since: str = ""
    before: str = ""


def imap_date(value: str) -> str:
    """``YYYY-MM-DD`` → IMAP 的 ``DD-Mon-YYYY``；格式非法抛 ``ValueError``。"""
    try:
        day = datetime.strptime(str(value).strip(), "%Y-%m-%d")
    except (TypeError, ValueError) as e:
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{value!r}") from e
    return f"{day.day:02d}-{_IMAP_MONTHS[day.month - 1]}-{day.year:04d}"


def _header_text(msg: Message, name: str) -> str:
    return str(msg.get(name) or "").strip()


def _summary_from_header(uid: int, size: int, msg: Message) -> MailSummary:
    raw_date = _header_text(msg, "Date")
    date_text = raw_date
    if raw_date:
        try:
            date_text = parsedate_to_datetime(raw_date).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OverflowError):
            pass
    return MailSummary(
        uid=uid,
        sender=_header_text(msg, "From"),
        subject=_header_text(msg, "Subject"),
        date=date_text,
        size=size,
    )


def _quote(value: str) -> str:
    """IMAP quoted string：去掉 CR/LF 并转义反斜杠与双引号。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_ascii(value: str) -> bool:
    try:
        str(value or "").encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


class MailClient:
    """一个邮箱账号的 IMAP/SMTP 客户端（每次操作新建连接，无长连接状态机）。"""

    def __init__(self, cfg) -> None:
        self._cfg = cfg

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
                quiet_logout(imap)
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

    # ── 只读查询（mail 工具用） ──────────────────────────────────────

    def list_recent(
        self, limit: int = 20, *, unread_only: bool = False,
    ) -> tuple[int, list[MailSummary]]:
        """返回 ``(总数, 最近 limit 封摘要)``；``unread_only`` 走 SEARCH UNSEEN。"""
        imap = self._open_inbox()
        try:
            uids = self._search_uids(imap, "UNSEEN" if unread_only else "ALL")
            total = len(uids)
            count = max(0, int(limit))
            recent = uids[-count:] if count else []
            items: list[MailSummary] = []
            for uid in recent:
                header = self._fetch_header(imap, uid)
                if header is None:
                    continue
                size, msg = header
                items.append(_summary_from_header(uid, size, msg))
            return total, items
        except imaplib.IMAP4.error as e:
            raise self._map_imap_error(e) from e
        except (OSError, ssl.SSLError) as e:
            raise EmailClientError(f"IMAP 连接失败：{e}") from e
        finally:
            quiet_logout(imap)

    def search_recent(
        self, criteria: SearchCriteria, limit: int = 20,
    ) -> tuple[list[MailSummary], bool]:
        """按发件人/主题/日期检索，返回 ``(摘要列表, 候选是否被截断)``。

        结构性条件（ASCII 的发件人、日期区间）走 IMAP SEARCH；主题与非 ASCII
        发件人做本地子串过滤（规避中文关键字的 ``CHARSET UTF-8`` 兼容问题）。
        候选最多取最近 50 封头部。
        """
        server_parts: list[str] = []
        if criteria.from_text and _is_ascii(criteria.from_text):
            server_parts.append(f"FROM {_quote(criteria.from_text)}")
        if criteria.since:
            server_parts.append(f"SINCE {imap_date(criteria.since)}")
        if criteria.before:
            server_parts.append(f"BEFORE {imap_date(criteria.before)}")
        query = " ".join(server_parts) or "ALL"

        imap = self._open_inbox()
        try:
            uids = self._search_uids(imap, query)
            candidates = uids[-50:]
            truncated = len(uids) > len(candidates)
            matched: list[MailSummary] = []
            needle_from = criteria.from_text.strip().lower()
            needle_subject = criteria.subject_text.strip().lower()
            for uid in candidates:
                header = self._fetch_header(imap, uid)
                if header is None:
                    continue
                size, msg = header
                summary = _summary_from_header(uid, size, msg)
                if needle_from and needle_from not in summary.sender.lower():
                    continue
                if needle_subject and needle_subject not in summary.subject.lower():
                    continue
                matched.append(summary)
            if limit > 0:
                matched = matched[-limit:]
            return matched, truncated
        except imaplib.IMAP4.error as e:
            raise self._map_imap_error(e) from e
        except (OSError, ssl.SSLError) as e:
            raise EmailClientError(f"IMAP 连接失败：{e}") from e
        finally:
            quiet_logout(imap)

    def read_message(self, uid: int, *, max_bytes: int = 0) -> ReadResult:
        """读单封；先看 ``RFC822.SIZE``，超过 ``max_bytes`` 则不下载正文。"""
        imap = self._open_inbox()
        try:
            header = self._fetch_header(imap, uid)
            if header is None:
                return ReadResult()
            size, head_msg = header
            if max_bytes and size > max_bytes:
                return ReadResult(headers=head_msg, size=size, too_large=True)
            raw = self._fetch_raw(imap, uid)
            if raw is None:
                return ReadResult(headers=head_msg, size=size)
            return ReadResult(
                message=email.message_from_bytes(raw, policy=email.policy.default),
                headers=head_msg,
                size=size,
            )
        except imaplib.IMAP4.error as e:
            raise self._map_imap_error(e) from e
        except (OSError, ssl.SSLError) as e:
            raise EmailClientError(f"IMAP 连接失败：{e}") from e
        finally:
            quiet_logout(imap)

    # ── 连接与协议细节 ──────────────────────────────────────────────

    def _open_inbox(self) -> imaplib.IMAP4:
        """连接并打开只读 INBOX；连接级异常统一映射。"""
        try:
            imap = self._connect_imap()
        except imaplib.IMAP4.error as e:
            raise self._map_imap_error(e) from e
        except (OSError, ssl.SSLError) as e:
            raise EmailClientError(f"IMAP 连接失败：{e}") from e
        try:
            self._select_inbox(imap)
        except Exception:
            quiet_logout(imap)
            raise
        return imap

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
    def _search_uids(imap: imaplib.IMAP4, criteria: str) -> list[int]:
        typ, data = imap.uid("SEARCH", None, criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        try:
            return sorted(int(x) for x in data[0].split())
        except ValueError:
            return []

    @staticmethod
    def _search_after(imap: imaplib.IMAP4, last_uid: int) -> list[int]:
        uids = MailClient._search_uids(imap, f"UID {last_uid + 1}:*")
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


def quiet_logout(imap: imaplib.IMAP4) -> None:
    try:
        imap.logout()
    except Exception:  # noqa: BLE001 — 关闭失败不影响本轮结果
        pass