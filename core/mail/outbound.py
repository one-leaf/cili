"""出站邮件组装：新邮件、回复（线程头）、转发附件。

主题是**头注入**的经典入口：拼装前剥离 CR/LF。收件人/主题的最终校验在工具层，
这里再清洗一次作为兜底。

``build_reply`` 默认带 ``Auto-Submitted: auto-replied``（RFC 3834），供接入端
自动回信防环路；``mail`` 工具主动发起的回复传 ``auto_submitted=False``。
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

_DEFAULT_SUBJECT = "Cili 回复"


@dataclass
class ReplyContext:
    """回复所需的线程上下文（来自触发回复的那封来信）。"""

    sender: str                  # 收件人（发件人地址）
    subject: str = ""
    message_id: str = ""
    references: str = ""


def clean_header_value(value, *, limit: int = 400) -> str:
    """单行化并截断（头注入防御；适用于主题这类有长度上限的头部）。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def clean_header_line(value) -> str:
    """单行化但不截断：用于收件人列表这类可能很长的头部。

    截断会把长收件人列表悄悄砍掉一半、甚至切在地址中间（SMTP 仍会按截断后的
    To 投递），所以这里只做 CR/LF 清理。
    """
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def build_message(
    cfg,
    to: str,
    subject: str,
    body: str,
    attachments: list[str] | None = None,
    *,
    extra_headers: dict[str, str] | None = None,
    inline_attachments: list[tuple[str, str, bytes]] | None = None,
) -> EmailMessage:
    """组一封邮件；附件条数/总量超限抛 ``ValueError``（不静默截断）。

    ``inline_attachments`` 是 ``(filename, content_type, payload)``，供转发把原邮件
    的附件原样带入（不落盘）。
    """
    paths = [str(p) for p in attachments or []]
    inline = list(inline_attachments or [])
    if len(paths) + len(inline) > cfg.max_outbound_files:
        raise ValueError(
            f"附件数量超出上限（最多 {cfg.max_outbound_files} 个，"
            f"实际 {len(paths) + len(inline)} 个）"
        )

    total = 0
    for path in paths:
        try:
            total += os.path.getsize(path)
        except OSError as e:
            raise ValueError(f"附件不可读：{os.path.basename(path)}（{e}）") from e
    total += sum(len(payload) for _, _, payload in inline)
    if total > cfg.max_outbound_bytes:
        raise ValueError(
            f"附件总量超出上限（最多 {cfg.max_outbound_bytes // (1024 * 1024)}MB）"
        )

    msg = EmailMessage()
    msg["From"] = formataddr((cfg.label or cfg.email_address, cfg.email_address))
    msg["To"] = clean_header_line(to)
    msg["Subject"] = clean_header_value(subject)
    msg["Message-ID"] = make_msgid(domain=cfg.email_address.split("@")[-1] or None)
    for name, value in (extra_headers or {}).items():
        msg[name] = value

    msg.set_content(body or "（无内容）")

    for path in paths:
        data = _read_file(path)
        ctype, encoding = mimetypes.guess_type(path)
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(
            data, maintype=maintype, subtype=subtype,
            filename=os.path.basename(path),
        )

    for filename, ctype, payload in inline:
        ctype = ctype or "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(
            payload, maintype=maintype, subtype=subtype or "octet-stream",
            filename=filename or "attachment",
        )
    return msg


def build_reply(
    cfg,
    ctx: ReplyContext,
    body: str,
    attachments: list[str] | None = None,
    *,
    auto_submitted: bool = True,
) -> EmailMessage:
    """组装回复：收件人/主题/线程头由 ``ctx`` 推导，其余走 ``build_message``。"""
    subject = clean_header_value(ctx.subject)
    if not subject:
        subject = _DEFAULT_SUBJECT
    elif not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    extra: dict[str, str] = {}
    if ctx.message_id:
        extra["In-Reply-To"] = clean_header_value(ctx.message_id)
    refs = _references(ctx.references, ctx.message_id)
    if refs:
        extra["References"] = refs
    if auto_submitted:
        extra["Auto-Submitted"] = "auto-replied"

    return build_message(
        cfg, clean_header_value(ctx.sender), subject, body,
        attachments, extra_headers=extra,
    )


def _references(existing: str, message_id: str) -> str:
    """References = 原信的 References + 原信 Message-ID（去重保序）。"""
    parts: list[str] = []
    for token in f"{existing or ''} {message_id or ''}".split():
        if token and token not in parts:
            parts.append(token)
    return " ".join(parts[-20:])


def _read_file(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise ValueError(f"附件不可读：{os.path.basename(path)}（{e}）") from e