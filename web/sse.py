"""SSE 适配层：把 runner 的同步回调桥接为 SSE 流响应。

与 core 解耦：core 只提供接口无关的 ``OutputSink`` 与领域事件；本模块负责
「runner 回调 → SSE 帧」的编码与「同步执行 → 异步流」的桥接。
其他接入端（如 QQ）应各自实现自己的适配层，不复用本模块。
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
from typing import Callable

from fastapi.responses import StreamingResponse

from core.base_session_runner import RETRY_CLEAR_SENTINEL
from core.output_sink import OutputSink
from core.tools.todo import todo_update_event

logger = logging.getLogger(__name__)


def make_sse_callbacks(event_queue: queue.Queue[str | None], runner) -> OutputSink:
    """构造 SSE 输出接收端（OutputSink），同步 runner 回调 → 队列。

    正文/思考/工具卡片**同时扇出**到 ``runner.default_sink``（全局事件流）：
    请求级流是临时的，页面一刷新即断，届时界面只能靠全局流继续渲染本回合输出。
    前端以 isSending 去重——有前台请求流时不重复渲染全局流的同类事件。
    """
    # 持久 sink（Web 层绑到 /api/events）；runner 未绑定时退化为 no-op
    base = getattr(runner, "default_sink", None) or OutputSink()
    # 帧里带上 session_id：请求级流不会因前端切会话而中断，前端据此丢弃
    # 不属于当前会话的帧，否则切走后本回合输出会渲染到另一个会话界面上
    sid = getattr(runner, "current_session_id", "") or ""

    def on_text(text: str) -> None:
        # Sentinel: 413 retry needs frontend to clear already-streamed text
        if text == RETRY_CLEAR_SENTINEL:
            # 控制信号只走请求级流：刷新后已渲染的正文会在回合结束时由
            # 重拉会话覆盖，不值得为这个罕见路径再引入一套全局清理协议
            event = json.dumps({"type": "retry_clear", "session_id": sid}, ensure_ascii=False)
            event_queue.put(f"data: {event}\n\n")
            return
        base.on_text(text)
        event = json.dumps({"type": "text", "content": text, "session_id": sid}, ensure_ascii=False)
        event_queue.put(f"data: {event}\n\n")

    def on_thinking(text: str) -> None:
        base.on_thinking(text)
        event = json.dumps({"type": "thinking", "content": text, "session_id": sid}, ensure_ascii=False)
        event_queue.put(f"data: {event}\n\n")

    def on_tool_call(tool_name: str, tool_input: dict, tool_use_id: str) -> None:
        base.on_tool_call(tool_name, tool_input, tool_use_id)
        event = json.dumps({"type": "tool_use", "tool": tool_name, "input": tool_input, "tool_use_id": tool_use_id, "session_id": sid}, ensure_ascii=False)
        event_queue.put(f"data: {event}\n\n")

    def on_tool_result(tool_name: str, output: str, is_error: bool, tool_use_id: str) -> None:
        # Skip tool_result SSE for placeholder tools (they have dedicated SSE events)
        if tool_name in ("ask_user", "session"):
            return
        base.on_tool_result(tool_name, output, is_error, tool_use_id)
        event = json.dumps({"type": "tool_result", "tool": tool_name, "content": output, "is_error": is_error, "tool_use_id": tool_use_id, "session_id": sid}, ensure_ascii=False)
        event_queue.put(f"data: {event}\n\n")

        # todo 更新事件（工具名判断与数据提取在 core，传输层只做序列化）
        todo_ev = todo_update_event(tool_name, is_error, runner.session, runner.workspace_uuid or "")
        if todo_ev:
            event_queue.put(f"data: {json.dumps(todo_ev, ensure_ascii=False)}\n\n")

    def on_session_start(exec_id: str, task_summary: str) -> None:
        event = json.dumps({"type": "session_start", "exec_id": exec_id, "task_summary": task_summary, "session_id": sid}, ensure_ascii=False)
        event_queue.put(f"data: {event}\n\n")

    def on_session_complete(exec_id: str) -> None:
        event = json.dumps({"type": "session_complete", "exec_id": exec_id, "session_id": sid}, ensure_ascii=False)
        event_queue.put(f"data: {event}\n\n")

    return OutputSink(
        on_text=on_text,
        on_thinking=on_thinking,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
        on_session_start=on_session_start,
        on_session_complete=on_session_complete,
    )


async def sse_stream(*events: dict):
    """Yield SSE events followed by a done event."""
    for event in events:
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"


def sse_run_response(
    run_fn: Callable[[], None],
    event_queue: queue.Queue[str | None],
    *,
    cancel_on_disconnect: bool = False,
    on_disconnect: Callable[[], None] | None = None,
    poll_timeout: float = 0.5,
) -> StreamingResponse:
    """把一次同步 runner 执行（run_fn，经 event_queue 推帧）桥接为 SSE 流响应。

    Args:
        run_fn: 在后台线程执行 runner 的同步入口（内部负责把事件写入 event_queue，
            并以 None 作结束哨兵）。
        event_queue: run_fn 写入 SSE 帧字符串的队列。
        cancel_on_disconnect: 客户端断开时是否请求停止后台执行。默认 False（长任务继续跑，
            需用户显式 /stop）；answer-ask-user 等无后续输入来源的场景传 True。
            注意：run_in_executor 的工作线程不可取消，``task.cancel()`` 只能取消
            future 本身，真正停止执行要靠 ``on_disconnect``。
        on_disconnect: 客户端断开时调用的停止回调（如 ``runner.stop``）。
        poll_timeout: 队列轮询间隔（秒）。
    """
    async def generate():
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(loop.run_in_executor(None, run_fn))
        try:
            while True:
                try:
                    # to_thread：避免阻塞的 queue.get 卡住事件循环
                    # （走默认 executor，线程复用，不是每轮新建）
                    event = await asyncio.to_thread(event_queue.get, True, poll_timeout)
                    if event is None:
                        break
                    yield event
                except queue.Empty:
                    continue
        except asyncio.CancelledError:
            if cancel_on_disconnect:
                task.cancel()
                if on_disconnect is not None:
                    try:
                        on_disconnect()
                    except Exception as e:
                        logger.warning(f"[sse] on_disconnect 回调失败: {e}")
            else:
                # 客户端断开 → runner 继续在后台跑，仅 /stop 能中断
                logger.info("Client disconnected, runner continues running in background")
            return

        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
