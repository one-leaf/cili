"""Web 文件交付：下载卡片消息、file 事件与 outbox 抽取。

渲染字段（Markdown 卡片 / 事件载荷 / file_card meta）只属于 Web 接入端；
core.outbox 只保留队列与交付回执。
"""

from __future__ import annotations

import logging
import os

from core.outbox import OutboxFile, drain_files
from core.session import session_key

logger = logging.getLogger(__name__)


def build_web_file_card(runner, f: OutboxFile) -> tuple[str, dict, dict]:
    """构建 Web 交付卡片与 ``file`` 事件（请求流与全局流共用）。

    返回 ``(消息内容, file 事件, file_card meta)``；路径统一为工作区相对路径，
    空格做 URL 编码（文件路由按解码后的 path 服务）。
    """
    name = os.path.basename(f.path)
    try:
        rel = os.path.relpath(f.path, runner.cwd).replace("\\", "/")
    except ValueError:
        # 不同盘符（理论不可达：send_user_file 已做工作区边界校验）；退化为裸文件名
        rel = name
    link = rel.replace(" ", "%20")
    note = f" —— {f.note}" if f.note else ""
    content = f"📎 [{name}]({link}){note}"
    try:
        size = os.path.getsize(f.path)
    except OSError:
        size = None
    event = {
        "type": "file",
        "name": name,
        "path": link,
        "size": size,
        "note": f.note,
        "session_id": getattr(runner, "current_session_id", ""),
    }
    meta = {"file_card": {"name": name, "path": link, "note": f.note}}
    return content, event, meta


def drain_web_files(runner, *, publish_global: bool = False) -> list[OutboxFile]:
    """取走并交付 Web 会话的出站文件：落 file_card，可选发全局 file 事件。

    供两种路径使用：
    - 请求级 SSE（``web/sse.drain_outbox_events``）：只落卡片，帧由调用方编码；
    - 后台/自动恢复回合（``core/default_sink``）：落卡片并发全局 file 事件，
      再由 ``turn_complete`` 让前端重拉会话。
    """
    key = session_key(runner.workspace_uuid, runner.current_session_id)
    files = drain_files(key)
    if not files:
        return []

    bus = None
    if publish_global:
        try:
            from core.event_bus import get_event_bus
            bus = get_event_bus()
        except Exception:  # noqa: BLE001 —— 总线不可用不影响落盘
            logger.debug("[outbox] 事件总线不可用", exc_info=True)

    for f in files:
        content, event, meta = build_web_file_card(runner, f)
        if bus is not None:
            bus.publish({
                **event,
                "workspace_uuid": getattr(runner, "workspace_uuid", ""),
            })
        try:
            runner.session.add_message("assistant", content, meta=meta, flush=True)
        except Exception:  # noqa: BLE001 —— 落盘失败只记日志，事件已发出
            logger.exception("[outbox] file_card 消息落盘失败")
    return files
