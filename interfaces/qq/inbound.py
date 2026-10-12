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
from core.outbox import drain_files
from core.session_registry import registry, session_key
from core.turn_dispatch import execute_turn, prepare_turn
from interfaces.qq import commands, media
from interfaces.qq.access import OwnerTracker
from interfaces.qq.attachment import InboundMedia, process_attachments
from interfaces.qq.runtime import QQRuntime
from interfaces.qq.sink import QQSink
from interfaces.qq.state import QQState, resolve_current, save_state

logger = logging.getLogger(__name__)

# 会话被占用时的重试：总等待 ≈ 6s，之后放弃并告知用户
BUSY_RETRIES = 12
BUSY_RETRY_INTERVAL = 0.5

# QQ 的 message_type：0=普通文本，103=引用消息（正文在 msg_elements 里）
_MSG_TYPE_TEXT = 0
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

        # 单用户：每次对话把 owner 更新为当前发送者（自动绑定 / 保持最新）
        if not self._owner.track(openid):
            return

        state = self._rt.state
        resolve_current(state)

        text = self._extract_text(event)
        attachments = list(getattr(event, "attachments", None) or [])

        if not text and not attachments:
            return

        # 指令分流（不进入 AI 回合）
        if text.startswith("/"):
            reply = await commands.dispatch(self._rt, state, text)
            if reply is not None:
                await self._send_text(openid, msg_id, reply)
                return

        media_items = await self._download_attachments(attachments)
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

    async def _download_attachments(self, attachments: list) -> list[InboundMedia]:
        if not attachments:
            return []
        entry = self._rt.state.workspace_uuid
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
        state = self._rt.state
        workspace_uuid, session_id = state.workspace_uuid, state.session_id
        if not workspace_uuid or not session_id:
            await self._send_text(openid, msg_id, "尚未确定工作区/会话，请先执行 `/w`。")
            return

        from core.config import find_workspace_entry

        entry = find_workspace_entry(workspace_uuid) or {}
        workspace_dir = str(entry.get("directory") or "")
        if not workspace_dir or not Path(workspace_dir).exists():
            await self._send_text(openid, msg_id, f"工作区目录不存在：{workspace_dir}")
            return

        key = session_key(workspace_uuid, session_id)
        runner = await registry.get_or_create(
            workspace_uuid, session_id, workspace_dir,
            on_create=lambda r: _bind_default_sink(r, workspace_uuid, session_id),
        )

        # 会话忙 → 排队等待（可能是 Web 或上一个 QQ 回合在跑）
        claimed = False
        for attempt in range(BUSY_RETRIES):
            if registry.claim(key):
                claimed = True
                break
            if attempt == 0:
                await self._send_text(openid, msg_id, "⏳ 上一轮还在执行，已排队…")
            await asyncio.sleep(BUSY_RETRY_INTERVAL)
        if not claimed:
            await self._send_text(openid, msg_id, "当前会话仍在执行中，请稍后再发。")
            return

        try:
            content = self._resolve_answer(runner, text) if text else ""
            user_input = build_user_input(content, media_items, runner)
            if user_input is None:
                await self._send_text(openid, msg_id, "暂不支持该消息类型。")
                return

            prepared = prepare_turn(runner, content=content, user_input=user_input)
            # 按回合门控：只有 QQ 驱动时 send_file 等专属工具才可见
            runner.set_turn_interface("qq")

            sink = QQSink(
                self._rt, openid, msg_id,
                base=getattr(runner, "default_sink", None) or None,
            )
            cb = sink.build()

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: execute_turn(prepared, cb))
        finally:
            runner.set_turn_interface(None)
            registry.release(key)

        await self._send_outbox_files(openid, msg_id, key)

    # ── 入参构造 ────────────────────────────────────────────────────

    # ── 入参构造 ────────────────────────────────────────────────────

    @staticmethod
    def _resolve_answer(runner, content: str) -> str:
        """ask_user 用编号文本渲染；用户回 "1" 时映射回选项 label。

        审批卡的三档中文 label 也走这条路——``inject_ask_user_answer`` 靠
        ``endswith(APPROVE_LABEL/REMEMBER_LABEL)`` 判定，所以必须先还原成 label。

        **只在「恰好一个问题」时做映射**：多问题时编号在每个问题内重新开始，
        单个数字是歧义的——与其猜错，不如原样透传（模型会看到用户就回了 "1"，
        可以据此再问一次）。
        """
        if not content or not content.strip().isdigit():
            return content
        from core.tools.ask_user import find_pending_ask_user

        pending = find_pending_ask_user(runner.session)
        if not pending:
            return content
        questions = _questions_of(runner.session, pending)
        if len(questions) != 1:
            return content
        idx = int(content.strip()) - 1
        options = questions[0].get("options") or []
        if 0 <= idx < len(options):
            return str(options[idx].get("label") or content)
        return content

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
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[qq/inbound] 发送文本失败：{e}")

    async def _send_outbox_files(self, openid: str, msg_id: str, key: str) -> None:
        """回合末取走 ``send_file`` 登记的文件并发送。

        受 ``max_outbound_files`` 与被动回复剩余配额双重限制——一条用户消息只有
        4 次被动回复，流式消息已占 1 次。
        """
        files = drain_files(key)
        if not files:
            return
        if self._rt.api is None or self._rt.uploader is None:
            logger.warning("[qq/inbound] SDK 未就绪，丢弃待发文件")
            return

        limit = self._rt.cfg.max_outbound_files
        check = self._rt.limiter.check(msg_id)
        if check.allowed:
            limit = min(limit, max(0, check.remaining - 1))
        else:
            limit = 0

        if limit <= 0:
            await self._send_text(openid, msg_id, "⚠️ 本轮回复次数已用尽，文件未能发送。")
            return
        if len(files) > limit:
            await self._send_text(
                openid, msg_id, f"⚠️ 本轮只能发 {limit} 个文件，其余 {len(files) - limit} 个已跳过。"
            )
            files = files[:limit]

        for f in files:
            try:
                await media.send_file_message(
                    self._rt.api, self._rt.uploader, openid, f.path,
                    msg_id=msg_id, note=f.note,
                )
                self._rt.limiter.record(msg_id)
            except media.MediaError as e:
                logger.warning(f"[qq/inbound] 发送文件失败：{e}")
                await self._send_text(openid, msg_id, f"⚠️ {e}")
            except Exception as e:  # noqa: BLE001 — 单个文件失败不影响其它
                logger.exception(f"[qq/inbound] 发送文件异常：{e}")


# ─── 模块级辅助 ───────────────────────────────────────────────────────


def _bind_default_sink(runner, workspace_uuid: str, session_id: str) -> None:
    """与 Web 绑同一个 default_sink（发布到全局事件总线），实现跨接口可见。"""
    from core.default_sink import bind_default_sink

    bind_default_sink(runner, workspace_uuid, session_id)


def _questions_of(session, tool_use_id: str) -> list[dict]:
    """从会话里取某个 ask_user 工具调用的 questions。"""
    for msg in session.messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") in ("tool_use", "tool_call") and block.get("id") == tool_use_id:
                return list((block.get("input") or {}).get("questions") or [])
    return []
