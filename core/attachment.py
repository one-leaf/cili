"""接入端附着记录：Attachment。

Cili 的会话层是接口无关的（``core/session_registry.py`` 持有 runner 池与运行权
认领），本模块承载「接入端附着」记录 ``Attachment``。

曾经设想的 ``Interface`` ABC（2026-11-19 删除）
----------------------------------------------
``OutputSink``（``core/output_sink.py``）就是渲染 seam：Web 的
``make_sse_callbacks(queue, runner)``、QQ 的 ``QQSink(...)``、微信/邮件的 Sink
都是同一角色的实现。原设想每个接入端做成 ``Interface`` 子类、实现 ``make_sink``
——实际落地时（web/qq/wechat/email）没有一个继承它：接入端的真实扩展点是
「inbound 模块 + Sink 实现 + ``core.interface_profile`` 能力档案」，回合驱动
统一走 ``core/turn_dispatch.py``。抽象没有消费者，故删除（设计历史见 git）。

设计文档草案里的 ``ask_user(attachment, questions) -> str`` /
``request_approval(...) -> str`` 被刻意放弃：各实现都**无法在回调里同步返回
答案**——Web 的答案经 ``POST .../ask-user/answers`` 异步回来，QQ 的答案是下一条
消息。把它们做成「返回答案」的方法是在说谎。交互提示的渲染本来就在各自 sink
内部（Web 在 ``on_tool_call`` 推 ``tool_use`` 帧、前端画卡片；QQ 在
``on_tool_call`` 里发编号文本），不需要独立的 prompt 方法。

入站侧不进抽象：Web 走 HTTP 路由、QQ 走 SDK 回调，形态差太远，强行统一会造出
两边都不顺的形状。两者收敛到同一个 ``core/turn_dispatch.py`` 即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Attachment:
    """一个接入端对某会话的附着记录（多接口共享会话时做输出路由/生命周期）。

    ``meta`` 承载接入端私有路由信息——Web 放 ``event_queue``，QQ 放
    ``openid`` / ``msg_id``——供接入端自己的 sink 构造逻辑使用。
    """

    attachment_id: str
    interface: str          # "web" / "qq" / "wechat" / "email"
    session_key: str        # "{workspace_uuid}:{session_id}"
    meta: dict[str, Any] = field(default_factory=dict)
