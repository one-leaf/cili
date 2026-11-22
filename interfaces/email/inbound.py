"""入站链的最后一环：邮件 → 会话回合。

与 ``interfaces/wechat/inbound.py`` 同构，差别在于**出站节奏**：微信在 sink 收尾
时先发正文、回合后再补文件；邮件则把正文与 ``send_user_file`` 附件装进**同一封信**
（一次回合只回一封）。

职责：
- 剥引文、截断正文、附件落盘（图片转 multimodal block）
- 指令分流（``/`` 开头的首行）→ 回信即指令回复
- 回合后：组装「正文 + 附件」一次发出；发不完的附件落交付回执
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from core.multimodal import build_user_input
from core.outbox import drain_files, record_delivery_notice
from core.session_registry import registry
from core.turn_dispatch import execute_turn, prepare_turn
from interfaces.common.ask_user import resolve_answer
from interfaces.common.owner import OwnerTracker
from interfaces.common.session_claim import prepare_and_claim
from interfaces.email import commands, media
from interfaces.email.client import MailItem
from interfaces.email.outbound import reply_context_from, send_reply
from interfaces.email.runtime import EmailRuntime
from interfaces.email.sink import EmailSink
from interfaces.email.state import bind_workspace, ensure_session

logger = logging.getLogger(__name__)


class InboundHandler:
    """把一封已放行的来信跑成一个回合。"""

    def __init__(self, runtime: EmailRuntime, owner: OwnerTracker) -> None:
        self._rt = runtime
        self._owner = owner

    # ── 入口 ────────────────────────────────────────────────────────

    async def handle(self, item: MailItem) -> None:
        """处理一封已放行的邮件。异常不外抛（游标照常推进）。"""
        try:
            await self._handle(item)
        except Exception:
            logger.exception("[email/inbound] 处理入站邮件失败")

    async def _handle(self, item: MailItem) -> None:
        if item.message is None or not item.sender:
            return
        if not self._owner.track(item.sender):
            return

        cfg = self._rt.cfg
        account = self._rt.state
        chat = bind_workspace(account, item.sender, cfg)

        text = media.truncate_body(media.extract_text(item.message), cfg.max_body_chars)
        attachments = await self._save_attachments(item, chat)
        if not text and not attachments:
            return

        ctx = reply_context_from(item)

        # 指令分流：只看来信首行
        if commands.is_command(text):
            first_line = text.strip().splitlines()[0]
            reply = await commands.dispatch(self._rt, account, item.sender, first_line)
            if reply is not None:
                await send_reply(self._rt, ctx, reply)
                return

        await self._run_turn(item, ctx, text, attachments)

    # ── 附件 ────────────────────────────────────────────────────────

    async def _save_attachments(self, item: MailItem, chat) -> list:
        """落盘附件到工作区 ``uploads/``；无附件或工作区未知时返回空。"""
        try:
            from core.config import find_workspace_entry

            entry = find_workspace_entry(chat.workspace_uuid) or {}
            ws_dir = Path(entry.get("directory") or "")
            if not ws_dir or not ws_dir.exists():
                return []
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: media.save_attachments(
                    item.message, uploads_dir=ws_dir / "uploads", workspace_dir=ws_dir,
                ),
            )
        except Exception:  # noqa: BLE001 — 附件失败只降级为无附件
            logger.exception("[email/inbound] 附件处理失败")
            return []

    # ── 回合 ────────────────────────────────────────────────────────

    async def _run_turn(
        self, item: MailItem, ctx, text: str, media_items: list,
    ) -> None:
        # 真要跑回合了：工作区没有会话时自动建一个（指令路径不建）

        async def _notify(t: str) -> None:
            await send_reply(self._rt, ctx, t)

        prepared = await prepare_and_claim(
            state=self._rt.state,
            cfg=self._rt.cfg,
            account_id=self._rt.account_id,
            chat_id=item.sender,
            ensure_session=ensure_session,
            notify=_notify,
            log_tag="email",
        )
        if prepared is None:
            return
        key, runner, chat, _workspace_dir = prepared

        sink = EmailSink(base=getattr(runner, "default_sink", None) or None)
        cb = sink.build()
        errors: list[str] = []
        try:
            content = resolve_answer(runner, text) if text else ""
            user_input = build_user_input(content, media_items, runner)
            if user_input is None:
                await send_reply(self._rt, ctx, "暂不支持该消息类型。")
                return

            prepared_turn = prepare_turn(runner, content=content, user_input=user_input)
            # 按回合门控：只有邮件驱动时 send_user_file 才可见
            runner.set_turn_interface("email")

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: execute_turn(prepared_turn, cb, on_error=errors.append),
            )
        finally:
            runner.set_turn_interface(None)
            registry.release(key)

        body = sink.final_text().strip()
        if not body and errors:
            body = f"回合执行出错：{errors[0]}"
        await self._deliver_letter(runner, key, ctx, body, chat)

    # ── 出站：正文 + 附件装进同一封信 ───────────────────────────────

    async def _deliver_letter(self, runner, key: str, ctx, body: str, chat) -> None:
        files = drain_files(key)
        selected, skipped = self._fit_files(files)
        if skipped:
            body = (
                f"{body}\n\n⚠️ 有 {len(skipped)} 个文件超出邮件附件限额，未能随信发出："
                + "、".join(os.path.basename(p) for p in skipped)
            ).strip()
            record_delivery_notice(
                runner.session, selected, skipped, "超出邮件附件限额"
            )
        if not body and not selected:
            return
        await send_reply(self._rt, ctx, body, selected)

    def _fit_files(self, files) -> tuple[list[str], list[str]]:
        """按条数与总量挑选附件（工具登记时已校验，这里防交付期变量）。"""
        cfg = self._rt.cfg
        selected: list[str] = []
        skipped: list[str] = []
        total = 0
        for f in files:
            try:
                size = os.path.getsize(f.path)
            except OSError:
                skipped.append(f.path)
                continue
            if len(selected) >= cfg.max_outbound_files \
                    or total + size > cfg.max_outbound_bytes:
                skipped.append(f.path)
                continue
            total += size
            selected.append(f.path)
        return selected, skipped
