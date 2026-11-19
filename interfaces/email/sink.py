"""EmailSink：只缓冲、不发送的输出接收端。

邮件是异步通道：回合期间没有任何进度反馈（没有流式、没有打字状态、没有中间
消息——每多发一封都是噪音）。本 sink 只做两件事：

1. 累积正文（只含回答与 ask_user 提示，不含工具轨迹）；
2. 把全部事件**扇出到 base**（Web 的全局事件流仍能看到这次回合的实时轨迹）。

发送统一在 ``inbound`` 的回合收尾做（正文 + ``send_user_file`` 附件装进同
一封信），所以这里没有 ``on_turn_complete`` 的提交动作。
"""

from __future__ import annotations

import threading

from core.base_session_runner import RETRY_CLEAR_SENTINEL
from core.output_sink import OutputSink
from interfaces.common.ask_user import render_questions


class EmailSink:
    """一次邮件回合的输出接收端（正文缓冲 + 全局事件扇出）。"""

    def __init__(self, *, base: OutputSink | None = None) -> None:
        self._base = base or OutputSink()
        self._buffer = ""
        self._lock = threading.Lock()

    # ── OutputSink 回调 ─────────────────────────────────────────────

    def on_text(self, text: str) -> None:
        if text == RETRY_CLEAR_SENTINEL:
            with self._lock:
                self._buffer = ""
            self._base.on_text(text)
            return
        self._base.on_text(text)
        with self._lock:
            self._buffer += text

    def on_thinking(self, text: str) -> None:
        self._base.on_thinking(text)

    def on_tool_call(self, tool_name: str, tool_input: dict, tool_use_id: str) -> None:
        self._base.on_tool_call(tool_name, tool_input, tool_use_id)
        if tool_name == "ask_user":
            # 交互提示折进回信正文；用户回一封「1」即视为答案（common/ask_user 映射）
            block = render_questions(tool_input)
            if block:
                with self._lock:
                    self._buffer += block

    def on_tool_result(
        self, tool_name: str, output: str, is_error: bool, tool_use_id: str,
    ) -> None:
        self._base.on_tool_result(tool_name, output, is_error, tool_use_id)

    def on_session_start(self, exec_id: str, task_summary: str) -> None:
        self._base.on_session_start(exec_id, task_summary)

    def on_session_complete(self, exec_id: str, status: str = "completed") -> None:
        self._base.on_session_complete(exec_id, status)

    def on_turn_complete(self) -> None:
        self._base.on_turn_complete()

    # ── 组装与读取 ──────────────────────────────────────────────────

    def build(self) -> OutputSink:
        return OutputSink(
            on_text=self.on_text,
            on_thinking=self.on_thinking,
            on_tool_call=self.on_tool_call,
            on_tool_result=self.on_tool_result,
            on_session_start=self.on_session_start,
            on_session_complete=self.on_session_complete,
            on_turn_complete=self.on_turn_complete,
        )

    def final_text(self) -> str:
        """取走累积的正文。"""
        with self._lock:
            text = self._buffer
            self._buffer = ""
        return text
