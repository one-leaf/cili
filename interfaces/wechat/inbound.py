"""入站链的最后一环：微信消息 → 会话回合。

与 ``interfaces/qq/inbound.py`` 同构：把消息整理成 agent 能吃的入参，再交给
``core.turn_dispatch``（接口无关的回合驱动）跑。

职责：
- 只处理私聊——群聊与 bot 自己的消息在 ``poller.py`` 就已经丢掉了
- owner 记录（自动绑定/刷新）→ 命令分流 → 普通消息进回合
- 媒体落盘后：图片转 multimodal block，其余把路径写进正文
- 回合结束后取走 ``core.outbox`` 里的待发文件，加密上传后发送
- 用户再次说话（带来新 context_token）时，重放之前因配额受限被推迟的回复
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from core.multimodal import build_user_input
from core.outbox import drain_files
from core.session_registry import registry, session_key
from core.turn_dispatch import execute_turn, prepare_turn
from interfaces.wechat import commands, media
from interfaces.wechat.access import OwnerTracker
from interfaces.wechat.protocol import InboundMessage
from interfaces.wechat.runtime import WechatRuntime
from interfaces.wechat.sink import WechatSink
from interfaces.wechat.state import resolve_current

logger = logging.getLogger(__name__)

# 会话被占用时的重试：总等待 ≈ 6s，之后放弃并告知用户
BUSY_RETRIES = 12
BUSY_RETRY_INTERVAL = 0.5


class InboundHandler:
    """把一条入站私聊消息跑成一个回合。"""

    def __init__(self, runtime: WechatRuntime, owner: OwnerTracker) -> None:
        self._rt = runtime
        self._owner = owner

    # ── 入口 ────────────────────────────────────────────────────────

    async def handle(self, inbound: InboundMessage) -> None:
        """处理一条已解析的入站消息。异常不外抛。"""
        try:
            await self._handle(inbound)
        except Exception:
            logger.exception("[wechat/inbound] 处理入站消息失败")

    async def _handle(self, inbound: InboundMessage) -> None:
        user_id = inbound.from_user_id
        if not user_id:
            return

        # 记录本账号最近对话的对方（状态展示用，不是访问闸门）
        if not self._owner.track(user_id):
            return

        account = self._rt.state
        chat = resolve_current(account, user_id, self._rt.cfg)

        # 用户又说话了 → 这就是新的 context_token；先把之前被推迟的回复补上。
        # 必须在命令分流之前：命令回复也要用这个 token 才发得出去。
        if inbound.context_token:
            self._rt.tokens.update(user_id, inbound.context_token)
            if self._rt.outbound is not None:
                await self._rt.outbound.replay_deferred(user_id)

        text = inbound.text.strip()
        if not text and not inbound.media:
            return

        # 指令分流（不进入 AI 回合）
        if text.startswith("/"):
            reply = await commands.dispatch(self._rt, account, user_id, text)
            if reply is not None:
                await self._send_text(user_id, reply)
                return

        media_items, failed = await self._download_media(inbound, chat)
        if failed:
            note = f"[有 {failed} 个附件未能下载]"
            text = f"{text}\n{note}" if text else note
        await self._run_turn(user_id, text, media_items, chat)

    # ── 媒体 ────────────────────────────────────────────────────────

    async def _download_media(self, inbound: InboundMessage, chat) -> tuple[list, int]:
        """下载入站媒体。返回 ``(已落盘的媒体, 失败个数)``。"""
        if not inbound.media:
            return [], 0

        from core.config import find_workspace_entry

        entry = find_workspace_entry(chat.workspace_uuid) or {}
        ws_dir = Path(entry.get("directory") or ".")

        items = []
        for ref in inbound.media:
            item = await media.download_media(
                ref,
                cfg=self._rt.cfg,
                client=self._rt.client,
                uploads_dir=ws_dir / "uploads",
                workspace_dir=ws_dir,
            )
            if item is not None:
                items.append(item)
        return items, len(inbound.media) - len(items)

    # ── 回合 ────────────────────────────────────────────────────────

    async def _run_turn(self, user_id: str, text: str, media_items: list, chat) -> None:
        # 真要跑回合了：工作区没有会话时自动建一个（§3.6；指令路径不建）
        from interfaces.wechat.state import ensure_session

        chat = ensure_session(self._rt.state, user_id, self._rt.cfg)
        workspace_uuid, session_id = chat.workspace_uuid, chat.session_id
        if not workspace_uuid or not session_id:
            await self._send_text(user_id, "尚未确定工作区/会话，请先执行 `/w`。")
            return

        from core.config import find_workspace_entry

        entry = find_workspace_entry(workspace_uuid) or {}
        workspace_dir = str(entry.get("directory") or "")
        if not workspace_dir or not Path(workspace_dir).exists():
            await self._send_text(user_id, f"工作区目录不存在：{workspace_dir}")
            return

        key = session_key(workspace_uuid, session_id)
        runner = await registry.get_or_create(
            workspace_uuid, session_id, workspace_dir,
            on_create=lambda r: _bind_default_sink(r, workspace_uuid, session_id),
        )

        # 会话忙 → 排队等待（可能是 Web 或上一个微信回合在跑）
        claimed = False
        for attempt in range(BUSY_RETRIES):
            if registry.claim(key):
                claimed = True
                break
            if attempt == 0:
                await self._send_text(user_id, "⏳ 上一轮还在执行，已排队…")
            await asyncio.sleep(BUSY_RETRY_INTERVAL)
        if not claimed:
            await self._send_text(user_id, "当前会话仍在执行中，请稍后再发。")
            return

        try:
            content = _resolve_answer(runner, text) if text else ""
            user_input = build_user_input(content, media_items, runner)
            if user_input is None:
                await self._send_text(user_id, "暂不支持该消息类型。")
                return

            prepared = prepare_turn(runner, content=content, user_input=user_input)
            # 按回合门控：只有微信驱动时 send_file 才可见
            runner.set_turn_interface("wechat")

            sink = WechatSink(
                self._rt, user_id,
                base=getattr(runner, "default_sink", None) or None,
            )
            cb = sink.build()

            # 回合内先亮出「正在输入…」——微信侧唯一的进度信号
            if self._rt.typing is not None:
                await self._rt.typing.start(user_id, self._rt.tokens.token(user_id))

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: execute_turn(prepared, cb))
        finally:
            runner.set_turn_interface(None)
            registry.release(key)

        await self._send_outbox_files(user_id, key, chat)

    # ── 出站辅助 ────────────────────────────────────────────────────

    async def _send_text(self, user_id: str, text: str) -> None:
        """用发送层回一条普通消息（自带 token 刷新与配额处理）。"""
        if not text or self._rt.outbound is None:
            return
        try:
            await self._rt.outbound.send_text(user_id, text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[wechat/inbound] 发送文本失败：{e}")

    async def _send_outbox_files(self, user_id: str, key: str, chat) -> None:
        """回合末取走 ``send_file`` 登记的文件并加密上传发送。

        受 ``max_outbound_files`` 与剩余发送预算双重限制——文本分片与出站文件共享
        同一个 context_token 的额度。
        """
        files = drain_files(key)
        if not files:
            return
        if self._rt.outbound is None:
            logger.warning("[wechat/inbound] 发送层未就绪，丢弃待发文件")
            return

        # 留 1 条给可能的提示文案
        limit = min(
            self._rt.cfg.max_outbound_files,
            max(0, self._rt.outbound.budget_left(user_id, reserve=1)),
        )
        if limit <= 0:
            await self._send_text(user_id, "⚠️ 本轮发送配额已用尽，文件未能发送。")
            return
        if len(files) > limit:
            await self._send_text(
                user_id, f"⚠️ 本轮只能发 {limit} 个文件，其余 {len(files) - limit} 个已跳过。"
            )
            files = files[:limit]

        from core.config import find_workspace_entry

        entry = find_workspace_entry(chat.workspace_uuid) or {}
        workspace_root = str(entry.get("directory") or "")

        for f in files:
            try:
                await media.send_file(
                    self._rt.outbound, user_id, f.path,
                    workspace_root=workspace_root or None, reserve=1,
                )
            except media.MediaError as e:
                logger.warning(f"[wechat/inbound] 发送文件失败：{e}")
                await self._send_text(user_id, f"⚠️ {e}")
            except Exception as e:  # noqa: BLE001 — 单个文件失败不影响其它
                logger.exception(f"[wechat/inbound] 发送文件异常：{e}")


# ─── 模块级辅助 ───────────────────────────────────────────────────────


def _bind_default_sink(runner, workspace_uuid: str, session_id: str) -> None:
    """与 Web 绑同一个 default_sink（发布到全局事件总线），实现跨接口可见。"""
    from core.default_sink import bind_default_sink

    bind_default_sink(runner, workspace_uuid, session_id)


def _resolve_answer(runner, content: str) -> str:
    """ask_user 用编号文本渲染；用户回 "1" 时映射回选项 label。

    审批卡的三档中文 label 也走这条路——``inject_ask_user_answer`` 靠
    ``endswith(APPROVE_LABEL/REMEMBER_LABEL)`` 判定，所以必须先还原成 label。

    **只在「恰好一个问题」时做映射**：多问题时编号在每个问题内重新开始，单个数字是
    歧义的——与其猜错，不如原样透传（模型看到用户就回了 "1"，可以据此再问一次）。

    与 ``sink.render_questions`` 是**一对**：那边的编号必须和这里一致。
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
