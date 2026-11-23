"""入站链的最后一环：QQ 消息 → 会话回合。

对齐官方插件的 ``dispatch/``：把事件整理成 agent 能吃的入参，再交给
``core.turn_dispatch``（接口无关的回合驱动）跑。

职责：
- 只处理单聊（``chat_scope == "c2c"``），群聊一律忽略
- owner 记录（自动绑定/刷新）→ 命令分流 → 普通消息进回合
- 附件落盘后：图片转 multimodal block，其余把路径写进文本
- 回合结束后取走 ``core.outbox`` 里的待发文件并上传发送
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from core.multimodal import build_user_input
from core.outbox import drain_files, record_delivery_notice
from core.session_registry import registry
from core.turn_dispatch import execute_turn, prepare_turn
from interfaces.common.ask_user import resolve_answer
from interfaces.common.owner import OwnerTracker
from interfaces.common.session_claim import prepare_and_claim
from interfaces.qq import commands, media
from interfaces.qq.inbound_media import InboundMedia, process_attachments
from interfaces.qq.runtime import QQRuntime
from interfaces.qq.sink import QQSink
from interfaces.qq.state import bind_workspace, ensure_session

logger = logging.getLogger(__name__)

# QQ 的 message_type：103=引用消息（正文在 msg_elements 里）
_MSG_TYPE_QUOTED = 103


class InboundHandler:
    """把一条入站私聊消息跑成一个回合。"""

    def __init__(self, runtime: QQRuntime, owner: OwnerTracker) -> None:
        self._rt = runtime
        self._owner = owner

    # ── 入口 ────────────────────────────────────────────────────────

    async def handle(self, event) -> None:
        """处理一条入站事件（SDK 的 InboundEvent）。异常不外抛。"""
        try:
            await self._handle(event)
        except Exception:
            logger.exception("[qq/inbound] 处理入站消息失败")

    async def _handle(self, event) -> None:
        if str(getattr(event, "chat_scope", "") or "") != "c2c":
            # 群聊/频道一律忽略（本次范围仅私聊）
            return

        openid = str(getattr(event, "chat_id", "") or "")
        msg_id = str(getattr(event, "message_id", "") or "")
        if not openid or not msg_id:
            return

        # 记录本账号最近对话的对方（状态展示用，不是访问闸门）
        if not self._owner.track(openid):
            return

        account = self._rt.state
        chat = bind_workspace(account, openid, self._rt.cfg)

        text = self._extract_text(event)
        attachments = list(getattr(event, "attachments", None) or [])

        if not text and not attachments:
            return

        # 指令分流（不进入 AI 回合）
        if text.startswith("/"):
            reply = await commands.dispatch(self._rt, account, openid, text)
            if reply is not None:
                await self._send_text(openid, msg_id, reply)
                return

        media_items = await self._download_attachments(attachments, chat)
        await self._run_turn(openid, msg_id, text, media_items)

    # ── 解析 ────────────────────────────────────────────────────────

    @staticmethod
    def _extract_text(event) -> str:
        """取消息正文。``message_type=103``（引用消息）正文在 ``msg_elements`` 里。"""
        content = str(getattr(event, "content", "") or "").strip()
        if content:
            return content
        if int(getattr(event, "message_type", 0) or 0) == _MSG_TYPE_QUOTED:
            parts = []
            for el in getattr(event, "msg_elements", None) or []:
                c = str(getattr(el, "content", "") or "").strip()
                if c:
                    parts.append(c)
            return "\n".join(parts).strip()
        return ""

    async def _download_attachments(self, attachments: list, chat) -> list[InboundMedia]:
        if not attachments:
            return []
        entry = chat.workspace_uuid
        from core.config import find_workspace_entry

        ws = find_workspace_entry(entry) or {}
        ws_dir = Path(ws.get("directory") or ".")
        return await process_attachments(
            attachments,
            workspace_dir=ws_dir,
            uploads_dir=ws_dir / "uploads",
            max_bytes=self._rt.cfg.max_inbound_bytes,
            client=self._rt.http,
        )

    # ── 回合 ────────────────────────────────────────────────────────

    async def _run_turn(
        self,
        openid: str,
        msg_id: str,
        text: str,
        media_items: list[InboundMedia],
    ) -> None:
        # 真要跑回合了：工作区没有会话时自动建一个（§3.6；指令路径不建）

        async def _notify(t: str) -> None:
            await self._send_text(openid, msg_id, t)

        async def _on_busy() -> None:
            await _notify("⏳ 上一轮还在执行，已排队…")

        prepared = await prepare_and_claim(
            state=self._rt.state,
            cfg=self._rt.cfg,
            account_id=self._rt.account_id,
            chat_id=openid,
            ensure_session=ensure_session,
            notify=_notify,
            on_busy=_on_busy,
        )
        if prepared is None:
            return
        key, runner, chat, workspace_dir = prepared

        try:
            content = resolve_answer(runner, text) if text else ""
            user_input = build_user_input(content, media_items, runner)
            if user_input is None:
                await self._send_text(openid, msg_id, "暂不支持该消息类型。")
                return

            prepared_turn = prepare_turn(runner, content=content, user_input=user_input)
            # 按回合门控：只有 QQ 驱动时 send_user_file 等专属工具才可见
            runner.set_turn_interface("qq")

            sink = QQSink(
                self._rt, openid, msg_id,
                base=getattr(runner, "default_sink", None) or None,
            )
            cb = sink.build()

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: execute_turn(prepared_turn, cb))
        finally:
            runner.set_turn_interface(None)
            registry.release(key)

        await self._deliver_outbox(runner, openid, msg_id, key, workspace_dir)

    # ── 入参构造 ────────────────────────────────────────────────────

    @staticmethod

    # ── 出站辅助 ────────────────────────────────────────────────────

    async def _send_text(self, openid: str, msg_id: str, text: str) -> None:
        """用 SDK 的 send_text 回一条普通消息（自带重试与长度截断）。"""
        if self._rt.api is None or not text:
            return
        try:
            await self._rt.api.send_text(
                "c2c", openid, text, reply_to=msg_id,
                markdown=self._rt.cfg.msg_format == "markdown",
            )
            # 普通文本回复同样消耗被动回复配额，必须记账，否则后续文件发送会超限
            if self._rt.limiter is not None:
                self._rt.limiter.record(msg_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq/inbound] 发送文本失败：{e}")

    async def _deliver_outbox(
        self, runner, openid: str, msg_id: str, key: str, workspace_root: str = ""
    ) -> None:
        """回合末取走 ``send_user_file`` 登记的文件并发送。

        受 ``max_outbound_files`` 与被动回复剩余配额双重限制——一条用户消息只有
        4 次被动回复，流式消息已占 1 次。**条数上限已在工具登记时校验过**，这里
        只剩「流式消息吃掉配额」这个交付期才知道的变量；发不完的件不静默丢：给
        用户一条 ⚠️ 文案，并把交付回执落进会话（UI 可见、不进 LLM 上下文）。
        """
        files = drain_files(key)
        if not files:
            return
        if self._rt.api is None or self._rt.uploader is None:
            logger.warning("[qq/inbound] SDK 未就绪，丢弃待发文件")
            record_delivery_notice(runner.session, [], [f.path for f in files], "发送通道未就绪")
            return

        limit = self._rt.cfg.max_outbound_files
        check = self._rt.limiter.check(msg_id)
        if check.allowed:
            limit = min(limit, max(0, check.remaining - 1))
        else:
            limit = 0

        if limit <= 0:
            await self._send_text(openid, msg_id, "⚠️ 本轮回复次数已用尽，文件未能发送。")
            record_delivery_notice(
                runner.session, [], [f.path for f in files], "本轮被动回复配额已用尽"
            )
            return

        over = files[limit:]
        if over:
            await self._send_text(
                openid, msg_id, f"⚠️ 本轮只能发 {limit} 个文件，其余 {len(over)} 个未发出。"
            )

        sent: list[str] = []
        failed: list[str] = []
        for f in files[:limit]:
            try:
                await media.send_user_file(
                    self._rt.api, self._rt.uploader, openid, f.path,
                    msg_id=msg_id, note=f.note, workspace_root=workspace_root or None,
                )
                self._rt.limiter.record(msg_id)
                sent.append(f.path)
            except media.MediaError as e:
                logger.warning(f"[qq/inbound] 发送文件失败：{e}")
                failed.append(f.path)
                await self._send_text(openid, msg_id, f"⚠️ {e}")
            except Exception as e:  # noqa: BLE001 — 单个文件失败不影响其它
                failed.append(f.path)
                logger.exception(f"[qq/inbound] 发送文件异常：{e}")

        skipped = [f.path for f in over] + failed
        if skipped:
            record_delivery_notice(
                runner.session, sent, skipped, "本轮配额不足" if over else "发送失败"
            )
