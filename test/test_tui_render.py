"""TUI 渲染原语与 SSE 帧路由测试（不碰终端，FakeRenderer 收集调用序列）。"""

from __future__ import annotations

from interfaces.tui.render import (
    build_option_labels,
    format_ask_user_card,
    resolve_answer,
    summarize_tool_input,
)
from interfaces.tui.sink import SseFrameRouter


class FakeRenderer:
    """记录渲染调用序列（与 Renderer 同签名）。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def submit_markdown(self, text: str) -> None:
        self.calls.append(("md", text))

    def submit_thinking(self, text: str) -> None:
        self.calls.append(("think", text))

    def tool_line(self, tool: str, tool_input) -> None:
        self.calls.append(("tool", tool, tool_input))

    def tool_result_line(self, tool: str, content: str, is_error: bool) -> None:
        self.calls.append(("result", tool, content, is_error))

    def file_line(self, name: str, path: str, note: str) -> None:
        self.calls.append(("file", name, path, note))

    def error_line(self, msg: str) -> None:
        self.calls.append(("error", msg))

    def status_line(self, msg: str) -> None:
        self.calls.append(("status", msg))

    def info_line(self, msg: str) -> None:
        self.calls.append(("info", msg))

    def ask_user_card(self, tool_input) -> None:
        self.calls.append(("ask", tool_input))


# ---------- 纯函数 ----------


class TestSummarizeToolInput:
    def test_path_field(self):
        assert summarize_tool_input({"path": "src/a.py"}) == "src/a.py"

    def test_command_truncated(self):
        assert len(summarize_tool_input({"command": "x" * 200})) == 80

    def test_priority_order(self):
        # path 在 command 之前
        assert summarize_tool_input({"path": "p", "command": "c"}) == "p"

    def test_none_and_empty(self):
        assert summarize_tool_input(None) == ""
        assert summarize_tool_input({}) == ""

    def test_whitespace_collapsed(self):
        assert summarize_tool_input({"command": "a\n  b"}) == "a b"


class TestAskUserHelpers:
    INPUT = {
        "questions": [
            {
                "question": "用哪个库?",
                "header": "库",
                "options": [
                    {"label": "rich", "description": "渲染强"},
                    {"label": "textual", "description": "全屏"},
                ],
            }
        ]
    }

    def test_card_text(self):
        card = format_ask_user_card(self.INPUT)
        assert "[库] 用哪个库?" in card
        assert "1. rich" in card and "2. textual" in card

    def test_labels(self):
        assert build_option_labels(self.INPUT) == ["rich", "textual"]

    def test_resolve_single(self):
        assert resolve_answer("1", ["rich", "textual"]) == "rich"

    def test_resolve_multi(self):
        assert resolve_answer("2 1", ["rich", "textual"]) == "textual；rich"
        assert resolve_answer("1,2", ["rich", "textual"]) == "rich；textual"

    def test_free_text_passthrough(self):
        assert resolve_answer("都用", ["rich", "textual"]) == "都用"
        assert resolve_answer("9", ["rich"]) == "9"  # 越界视为自由文本
        assert resolve_answer("1", []) == "1"  # 无问题卡原样

    def test_empty_input(self):
        assert format_ask_user_card({}) == ""
        assert build_option_labels(None) == []


# ---------- SseFrameRouter ----------


class TestSseFrameRouter:
    def test_text_accumulates_and_submits_on_finish(self):
        r = FakeRenderer()
        router = SseFrameRouter(r)
        router.handle({"type": "text", "content": "你好，"})
        router.handle({"type": "text", "content": "世界"})
        assert r.calls == []  # 未到提交点
        router.finish()
        assert r.calls == [("md", "你好，世界")]

    def test_retry_clear_empties_buffers(self):
        r = FakeRenderer()
        router = SseFrameRouter(r)
        router.handle({"type": "text", "content": "旧内容"})
        router.handle({"type": "retry_clear"})
        router.handle({"type": "text", "content": "新内容"})
        router.finish()
        assert r.calls == [("md", "新内容")]

    def test_tool_use_flushes_pending_text(self):
        r = FakeRenderer()
        router = SseFrameRouter(r)
        router.handle({"type": "text", "content": "先看下文件"})
        router.handle({"type": "tool_use", "tool": "read", "input": {"path": "a.py"}, "tool_use_id": "t1"})
        assert ("md", "先看下文件") in r.calls
        assert ("tool", "read", {"path": "a.py"}) in r.calls

    def test_tool_result_success_and_error(self):
        r = FakeRenderer()
        router = SseFrameRouter(r)
        router.handle({"type": "tool_result", "tool": "read", "content": "l1\nl2", "is_error": False})
        router.handle({"type": "tool_result", "tool": "bash", "content": "boom", "is_error": True})
        assert ("result", "read", "l1\nl2", False) in r.calls
        assert ("result", "bash", "boom", True) in r.calls

    def test_ask_user_card_and_labels(self):
        r = FakeRenderer()
        router = SseFrameRouter(r)
        tool_input = {
            "questions": [{"question": "选哪个?", "header": "选择",
                           "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}]}]
        }
        router.handle({"type": "tool_use", "tool": "ask_user", "input": tool_input, "tool_use_id": "t2"})
        assert router.ask_labels == ["A", "B"]
        assert any(c[0] == "ask" for c in r.calls)

    def test_ask_user_result_prints_status(self):
        r = FakeRenderer()
        router = SseFrameRouter(r)
        router.handle({"type": "tool_result", "tool": "ask_user", "content": "A", "is_error": False})
        # ask_user 的 tool_result 不走通用摘要行（web 侧跳过的占位工具，TUI 收到的是应答确认）
        assert all(c[0] != "result" for c in r.calls)
        assert any(c[0] == "status" for c in r.calls)

    def test_error_flushes_pending_text(self):
        r = FakeRenderer()
        router = SseFrameRouter(r)
        router.handle({"type": "text", "content": "半截正文"})
        router.handle({"type": "error", "content": "API 超时"})
        assert ("md", "半截正文") in r.calls
        assert ("error", "API 超时") in r.calls

    def test_file_frame(self):
        r = FakeRenderer()
        router = SseFrameRouter(r)
        router.handle({"type": "file", "name": "x.md", "path": "out/x.md", "note": "报告"})
        assert r.calls == [("file", "x.md", "out/x.md", "报告")]

    def test_unknown_frame_ignored(self):
        r = FakeRenderer()
        router = SseFrameRouter(r)
        router.handle({"type": "todo_update_event"})
        router.handle({"type": "done"})
        assert r.calls == []

    def test_thinking_before_text(self):
        r = FakeRenderer()
        router = SseFrameRouter(r)
        router.handle({"type": "thinking", "content": "想一想"})
        router.handle({"type": "text", "content": "答案"})
        router.finish()
        assert r.calls == [("think", "想一想"), ("md", "答案")]
