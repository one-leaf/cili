"""runner 的持久输出接收端（default_sink）绑定。

原先这段逻辑住在 ``web/deps.py``，第二个接入端（QQ）要用只能反向依赖 web。
但它的内容其实是**接口无关**的：把状态发布到 ``core/event_bus`` 的全局事件流。
Web 的 ``/api/events`` 只是该总线的订阅者之一；QQ 绑同一个 sink 后，QQ 驱动的
回合在 Web 全局流里同样可见——这正是「共享会话」的可见性保证。

生命周期：``default_sink`` 随 runner 存活，用于工具实时输出与**后台子代理完成
后的自动恢复循环**（那时没有接口在发起回合，只能走持久 sink）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.event_bus import get_event_bus
from core.output_sink import OutputSink

if TYPE_CHECKING:  # 避免运行时循环导入
    from core.session_runner import SessionRunner


def bind_default_sink(runner: "SessionRunner", workspace_uuid: str, session_id: str) -> None:
    """把 runner 的持久输出接收端绑到全局事件总线。

    幂等：重复调用只是重绑同一个 session 的事件发布。
    """
    bus = get_event_bus()

    def _on_tool_output(tool_name: str, content: str, offset: int, tool_use_id: str) -> None:
        bus.publish({
            "type": "tool_output",
            "workspace_uuid": workspace_uuid,
            "session_id": session_id,
            "tool": tool_name,
            "content": content,
            "offset": offset,
            "tool_use_id": tool_use_id,
        })

    def _default_on_text(text: str) -> None:
        bus.publish({
            "type": "text",
            "workspace_uuid": workspace_uuid,
            "session_id": session_id,
            "content": text,
        })

    def _default_on_thinking(text: str) -> None:
        bus.publish({
            "type": "thinking",
            "workspace_uuid": workspace_uuid,
            "session_id": session_id,
            "content": text,
        })

    def _default_on_tool_call(tool_name: str, tool_input: dict, tool_use_id: str) -> None:
        bus.publish({
            "type": "tool_use",
            "workspace_uuid": workspace_uuid,
            "session_id": session_id,
            "tool": tool_name,
            "input": tool_input,
            "tool_use_id": tool_use_id,
        })

    def _default_on_tool_result(tool_name: str, output: str, is_error: bool, tool_use_id: str) -> None:
        if tool_name in ("ask_user", "session"):
            return
        bus.publish({
            "type": "tool_result",
            "workspace_uuid": workspace_uuid,
            "session_id": session_id,
            "tool": tool_name,
            "content": output,
            "is_error": is_error,
            "tool_use_id": tool_use_id,
        })

    def _default_on_turn_complete() -> None:
        # 回合已落盘：前端据此重拉会话。刷新页面后请求级流已断开，
        # 这条全局事件是界面唯一能知道「本回合结束了」的途径。
        bus.publish({
            "type": "turn_complete",
            "workspace_uuid": workspace_uuid,
            "session_id": session_id,
        })

    def _default_on_user_message(payload: dict) -> None:
        # 排队消息投递：聊天区补一条合并用户气泡；刷新后的界面也能看到
        # 这条全局事件（后台泵回合没有前台请求流）。
        bus.publish({
            "type": "user_message",
            "workspace_uuid": workspace_uuid,
            "session_id": session_id,
            **payload,
        })

    runner.default_sink = OutputSink(
        on_text=_default_on_text,
        on_thinking=_default_on_thinking,
        on_tool_call=_default_on_tool_call,
        on_tool_result=_default_on_tool_result,
        on_tool_output=_on_tool_output,
        on_turn_complete=_default_on_turn_complete,
        on_user_message=_default_on_user_message,
    )
    runner.sink = runner.default_sink
