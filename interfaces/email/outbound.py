"""回信：把组装好的回复通过 SMTP 发出去（组装逻辑在 ``core.mail.outbound``）。

一次回合只回一封信：没有流式、没有进度（异步通道没有人在收件箱前等）。
回信必须落在用户原邮件的会话线程里（``In-Reply-To`` / ``References``），并标注
``Auto-Submitted: auto-replied``（RFC 3834）——与本端入站闸形成闭环，防自动回复环路。

``ReplyContext`` / ``build_reply`` 由 core 提供并在此 re-export，保持既有导入路径。
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial

from core.mail.outbound import ReplyContext, build_reply
from interfaces.email.client import EmailAuthError, EmailClientError
from interfaces.email.runtime import EmailRuntime

logger = logging.getLogger(__name__)

__all__ = ["ReplyContext", "build_reply", "reply_context_from", "send_reply"]


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