"""回信：线程头、纯文本正文、附件（设计 §八）。

一次回合只回一封信：没有流式、没有进度（异步通道没有人在收件箱前等）。
回信必须落在用户原邮件的会话线程里（``In-Reply-To`` / ``References``），并标注
``Auto-Submitted: auto-replied``（RFC 3834）——与本端入站闸形成闭环，防自动回复环路。

主题是**头注入**的经典入口：拼装前剥离 CR/LF。
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from functools import partial

from interfaces.email.client import EmailAuthError, EmailClientError
from interfaces.email.runtime import EmailRuntime

logger = logging.getLogger(__name__)

_DEFAULT_SUBJECT = "Cili 回复"


@dataclass
class ReplyContext:
    """本回合回信所需的线程上下文（来自触发它的那封来信）。"""

    sender: str                  # 收件人（发件人地址）
    subject: str = ""
    message_id: str = ""
    references: str = ""


def _clean_header_value(value: str, *, limit: int = 400) -> str:
    """单行化并截断（头注入防御）。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def build_reply(
    cfg,
    ctx: ReplyContext,
    body: str,
    attachments: list[str] | None = None,
) -> EmailMessage:
    """组装回信。附件超限抛 ``ValueError``（不静默截断）。"""
    paths = list(attachments or [])
    if len(paths) > cfg.max_outbound_files:
        raise ValueError(
            f"附件数量超出上限（最多 {cfg.max_outbound_files} 个，实际 {len(paths)} 个）"
        )
    total = 0
    for path in paths:
        try:
            total += os.path.getsize(path)
        except OSError as e:
            raise ValueError(f"附件不可读：{os.path.basename(path)}（{e}）") from e
    if total > cfg.max_outbound_bytes:
        raise ValueError(
            f"附件总量超出上限（最多 {cfg.max_outbound_bytes // (1024 * 1024)}MB）"
        )

    subject = _clean_header_value(ctx.subject)
    if not subject:
        subject = _DEFAULT_SUBJECT
    elif not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    msg = EmailMessage()
    msg["From"] = formataddr((cfg.label or cfg.email_address, cfg.email_address))
    msg["To"] = _clean_header_value(ctx.sender)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=cfg.email_address.split("@")[-1] or None)
    msg["Auto-Submitted"] = "auto-replied"
    if ctx.message_id:
        msg["In-Reply-To"] = _clean_header_value(ctx.message_id)
    refs = _references(ctx.references, ctx.message_id)
    if refs:
        msg["References"] = refs

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
    return msg


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


async def send_reply(
    runtime: EmailRuntime,
    ctx: ReplyContext,
    body: str,
    attachments: list[str] | None = None,
) -> bool:
    """把一封信发出去（SMTP 走 executor）。失败只记日志并更新状态。"""
    if not ctx.sender:
        logger.warning("[email/outbound] 缺少收件人，丢弃本次回复")
        return False
    try:
        msg = build_reply(runtime.cfg, ctx, body, attachments)
    except ValueError as e:
        logger.warning(f"[email/outbound] 回信无法组装：{e}")
        return False
    if runtime.client is None:
        logger.warning("[email/outbound] 发送层未就绪，丢弃本次回复")
        return False
    loop = runtime.loop or asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, partial(runtime.client.send, msg))
        return True
    except EmailAuthError as e:
        logger.error(f"[email/outbound] 认证失败，回信未送达：{e}")
    except EmailClientError as e:
        logger.warning(f"[email/outbound] 回信发送失败：{e}")
    except Exception:  # noqa: BLE001 — 发送失败绝不能打断回合收尾
        logger.exception("[email/outbound] 回信发送异常")
    return False


def reply_context_from(item) -> ReplyContext:
    """从触发的来信构造回信上下文。"""
    return ReplyContext(
        sender=item.sender,
        subject=item.subject,
        message_id=item.message_id,
        references=item.references,
    )
