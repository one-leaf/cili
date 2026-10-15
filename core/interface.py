"""接入端抽象：Interface ABC + Attachment。

Cili 的会话层是接口无关的（``core/session_registry.py`` 持有 runner 池与运行权
认领），本模块补上「接入端」这一层抽象——Web / QQ 各自是一个 ``Interface``
实现，共享同一份会话。

为什么 ABC 只有一个方法
-----------------------
``OutputSink``（``core/output_sink.py``）**已经就是**渲染 seam，且已被 Web 验证过：
Web 的 ``make_sse_callbacks(queue, runner)`` 与 QQ 的 ``QQSink(...)`` 是同一角色的
两个实现，``make_sink`` 只是把它收敛成一个接口方法。

设计文档草案里的 ``ask_user(attachment, questions) -> str`` /
``request_approval(...) -> str`` 被刻意放弃：两个实现都**无法在回调里同步返回
答案**——Web 的答案经 ``POST /answer-ask-user`` 异步回来，QQ 的答案是下一条
消息。把它们做成「返回答案」的方法是在说谎。交互提示的渲染本来就在各自 sink
内部（Web 在 ``on_tool_call`` 推 ``tool_use`` 帧、前端画卡片；QQ 在
``on_tool_call`` 里发编号文本），不需要独立的 prompt 方法。

入站侧也不进 ABC：Web 走 HTTP 路由、QQ 走 SDK 回调，形态差太远，强行统一会造出
两边都不顺的形状。两者收敛到同一个 ``core/turn_dispatch.py`` 即可。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.output_sink import OutputSink

if TYPE_CHECKING:  # 避免运行时循环导入
    from core.session_runner import SessionRunner


@dataclass
class Attachment:
    """一个接入端对某会话的附着记录（多接口共享会话时做输出路由/生命周期）。

    ``meta`` 承载接入端私有路由信息——Web 放 ``event_queue``，QQ 放
    ``openid`` / ``msg_id``——由 ``Interface.make_sink`` 据此构造 sink。
    """

    attachment_id: str
    interface: str          # "web" / "qq"
    session_key: str        # "{workspace_uuid}:{session_id}"
    meta: dict[str, Any] = field(default_factory=dict)


class Interface(ABC):
    """一个接入端：把会话状态渲染给它的 attachment，并作为用户输入来源。"""

    name: str = "base"

    # 能力档案名（core.interface_profile 注册表的键）。接入端在自己模块里
    # 声明，与 make_sink 同处——能力与渲染路径一体。None 表示无档案
    # （cron 等无客户端驱动方）。当前 web/qq/wechat 尚未以 Interface 子类
    # 形态落地（各自是路由/inbound 模块），此属性供新接入端（如终端）遵循。
    profile: str | None = None

    @abstractmethod
    def make_sink(self, attachment: Attachment, runner: "SessionRunner") -> OutputSink:
        """构造本次 run/resume 的活动 sink（渲染会话状态到该 attachment）。"""
