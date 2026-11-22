"""SSE 帧 → 渲染回调的路由（方案 §六映射表的落点）。

M1 状态机：
- ``text`` 帧累积进正文缓冲，``thinking`` 帧累积进思考缓冲——两者在
  **提交点**（tool_use / file / error / done / retry_clear）一次性渲染；
- ``retry_clear``（413 重试信号）清空两个缓冲重流——终端可清空缓冲，
  这是相对 QQ 流式前缀不可变的本质差异；
- ask_user 的 ``tool_use`` 渲染问题卡并记下编号表，答案由 app 层经
  :func:`render.resolve_answer` 映射后作为下一条消息发送（服务端
  prepare_turn 自动判 resume）。
"""

from __future__ import annotations

from typing import Any

from interfaces.tui.render import Renderer


class SseFrameRouter:
    def __init__(self, renderer: Renderer):
        self.renderer = renderer
        self.text_buf: list[str] = []
        self.thinking_buf: list[str] = []
        # 最近一张 ask_user 卡的选项 label 表（编号 → label），供 app 层映射答案
        self.ask_labels: list[str] = []

    # ---------- 帧入口 ----------

    def handle(self, frame: dict) -> None:
        ftype = frame.get("type")
        if ftype == "text":
            self.text_buf.append(frame.get("content", ""))
        elif ftype == "thinking":
            self.thinking_buf.append(frame.get("content", ""))
        elif ftype == "retry_clear":
            self.text_buf.clear()
            self.thinking_buf.clear()
        elif ftype == "tool_use":
            self.flush()
            self.renderer.tool_line(frame.get("tool", ""), frame.get("input"))
            if frame.get("tool") == "ask_user":
                card_input = frame.get("input") or {}
                self.ask_labels = _labels_of(card_input)
                self.renderer.ask_user_card(card_input)
        elif ftype == "tool_result":
            if frame.get("tool") == "ask_user":
                self.renderer.status_line("已提交答案。")
            else:
                self.renderer.tool_result_line(
                    frame.get("tool", ""), frame.get("content", ""), bool(frame.get("is_error"))
                )
        elif ftype == "file":
            self.renderer.file_line(
                frame.get("name", ""), frame.get("path", ""), frame.get("note", "")
            )
        elif ftype == "error":
            self.flush()
            self.renderer.error_line(frame.get("content", ""))
        elif ftype == "session_start":
            summary = frame.get("task_summary", "")
            self.renderer.status_line(f"▶ {summary}" if summary else "▶ 会话开始")
        elif ftype == "session_complete":
            self.renderer.status_line("■ 会话完成")
        # done / 未知帧：done 由 finish() 兜底，未知类型静默忽略（todo 等未来类型）

    def finish(self) -> None:
        """回合结束（done 帧或流断开）后的兜底提交。"""
        self.flush()

    # ---------- 提交点 ----------

    def flush(self) -> None:
        """把累积缓冲渲染到 scrollback 并清空。"""
        if self.thinking_buf:
            self.renderer.submit_thinking("".join(self.thinking_buf))
        self.thinking_buf.clear()
        if self.text_buf:
            self.renderer.submit_markdown("".join(self.text_buf))
        self.text_buf.clear()


def _labels_of(tool_input: Any) -> list[str]:
    from interfaces.tui.render import build_option_labels

    if not isinstance(tool_input, dict):
        return []
    return build_option_labels(tool_input)
