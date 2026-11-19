"""入站回合的准备段：建会话 → 校验工作区 → 青少年检查 → 取 runner → 认领。

QQ / 微信 / 邮件三接口的 ``_run_turn`` 开头是同一段机械流程（约 60 行），
本模块把它收敛为一个函数。各接口保留：提示文案的发送回调、媒体与回合
执行、出站交付。

``notify(text)`` 由各接口适配自己的发送原语（QQ 带被动回复 msg_id、微信
纯文本、邮件即时回信）；``on_busy`` 在首次认领失败时触发——QQ/微信
给用户发 ⏳ 提示，邮件传 None 改记日志（不回信，免得一次失败变成一通邮件）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, NamedTuple

from core.config import find_workspace_entry
from core.default_sink import bind_default_sink
from core.session_registry import registry, session_key
from core.teen_mode import TeenModeDenied, get_teen_mode

logger = logging.getLogger(__name__)

# 会话被占用时的重试：总等待 ≈ 6s，之后放弃并告知用户
BUSY_RETRIES = 12
BUSY_RETRY_INTERVAL = 0.5


class PreparedTurn(NamedTuple):
    """prepare_and_claim 的产出：调用方据此跑回合与出站交付。"""

    key: str                 # session_key（认领凭证，回合后必须 release）
    runner: Any
    chat: Any                # 账号状态里的聊天切片（含 workspace_uuid/session_id）
    workspace_dir: str       # 工作区目录（出站交付需要；其余调用方可忽略）


async def prepare_and_claim(
    *,
    state: Any,
    cfg: Any,
    account_id: str,
    chat_id: str,
    ensure_session: Callable[..., Any],
    notify: Callable[[str], Awaitable[None]],
    on_busy: Callable[[], Awaitable[None]] | None = None,
    log_tag: str = "interface",
) -> PreparedTurn | None:
    """准备并认领一个会话回合。

    返回 ``PreparedTurn``；任何一步失败都已通过 ``notify`` 告知用户并返回
    None（调用方直接 return）。
    """
    chat = ensure_session(state, chat_id, cfg, account_id=account_id)
    workspace_uuid, session_id = chat.workspace_uuid, chat.session_id
    if not workspace_uuid or not session_id:
        await notify("尚未确定工作区/会话，请先执行 `/w`。")
        return None

    entry = find_workspace_entry(workspace_uuid) or {}
    workspace_dir = str(entry.get("directory") or "")
    if not workspace_dir or not Path(workspace_dir).exists():
        await notify(f"工作区目录不存在：{workspace_dir}")
        return None

    # 青少年模式：时长用尽/时段外直接回提示（进行中的回合不受影响）
    try:
        get_teen_mode().check_send_allowed()
    except TeenModeDenied as e:
        await notify(str(e))
        return None

    key = session_key(workspace_uuid, session_id)
    try:
        runner = await registry.get_or_create(
            workspace_uuid, session_id, workspace_dir,
            on_create=lambda r: bind_default_sink(r, workspace_uuid, session_id),
        )
    except TeenModeDenied as e:
        await notify(f"🔒 {e}")
        return None

    # 会话忙 → 排队等待（可能是 Web 或上一个接口回合在跑）
    claimed = False
    for attempt in range(BUSY_RETRIES):
        if registry.claim(key):
            claimed = True
            break
        if attempt == 0:
            if on_busy is not None:
                await on_busy()
            else:
                logger.info(f"[{log_tag}/inbound] 会话被占用，排队等待")
        await asyncio.sleep(BUSY_RETRY_INTERVAL)
    if not claimed:
        await notify("当前会话仍在执行中，请稍后再发。")
        return None

    return PreparedTurn(key, runner, chat, workspace_dir)
