"""web/sse.py 适配层测试：SSE 回调与持久 sink 的扇出关系、回合结束信号。

回归背景：make_sse_callbacks 此前直接**覆盖** default_sink 的正文类回调，
于是请求级流是这些事件的唯一去处 —— 页面一刷新（请求流断开）本回合的
正文/思考/工具卡片就再也收不到，且跑完没有信号让界面重拉。
"""

import json
import queue
from types import SimpleNamespace

from core.output_sink import OutputSink
from web.sse import make_sse_callbacks


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


class _FakeRunner:
    """最小 runner 替身：on_tool_result 会读 session / workspace_uuid 取 todo 事件。"""

    def __init__(self, base, session_id: str = "sess-A"):
        self.default_sink = base
        self.session = None
        self.workspace_uuid = ""
        self.current_session_id = session_id


class TestFanOutToDefaultSink:
    def test_text_fans_out_and_also_streams(self):
        received = []
        q = queue.Queue()
        sink = make_sse_callbacks(q, _FakeRunner(OutputSink(on_text=received.append)))

        sink.on_text("hello")

        assert received == ["hello"], "正文必须同时广播到全局流"
        frames = _drain(q)
        assert len(frames) == 1 and '"content": "hello"' in frames[0]

    def test_thinking_and_tool_events_fan_out(self):
        calls = []
        base = OutputSink(
            on_thinking=lambda t: calls.append(("thinking", t)),
            on_tool_call=lambda n, i, u: calls.append(("call", n, u)),
            on_tool_result=lambda n, o, e, u: calls.append(("result", n, e)),
        )
        q = queue.Queue()
        sink = make_sse_callbacks(q, _FakeRunner(base))

        sink.on_thinking("hmm")
        sink.on_tool_call("bash", {"command": "ls"}, "t1")
        sink.on_tool_result("bash", "out", False, "t1")

        assert calls == [
            ("thinking", "hmm"),
            ("call", "bash", "t1"),
            ("result", "bash", False),
        ]
        assert len(_drain(q)) == 3

    def test_placeholder_tool_result_not_fanned_out(self):
        """占位工具（ask_user/session）有专用事件，不走 tool_result 通道。"""
        calls = []
        q = queue.Queue()
        sink = make_sse_callbacks(
            q, _FakeRunner(OutputSink(on_tool_result=lambda *a: calls.append(a)))
        )

        sink.on_tool_result("ask_user", "x", False, "t1")

        assert calls == []
        assert _drain(q) == []

    def test_retry_sentinel_is_control_frame_only(self):
        """413 重试的清理信号是控制帧，不得作为正文广播到全局流。"""
        from core.base_session_runner import RETRY_CLEAR_SENTINEL

        received = []
        q = queue.Queue()
        sink = make_sse_callbacks(q, _FakeRunner(OutputSink(on_text=received.append)))

        sink.on_text(RETRY_CLEAR_SENTINEL)

        assert received == []
        frames = _drain(q)
        assert len(frames) == 1 and "retry_clear" in frames[0]

    def test_runner_without_default_sink_is_safe(self):
        """runner 未绑定 default_sink 时退化为 no-op，不抛异常。"""
        q = queue.Queue()
        sink = make_sse_callbacks(q, SimpleNamespace())

        sink.on_text("x")
        sink.on_thinking("y")

        assert len(_drain(q)) == 2

    def test_every_frame_carries_session_id(self):
        """回归：帧必须带 session_id —— 前台流不因切会话而中断，前端据此
        丢弃属于其他会话的帧，否则本回合输出会渲染到另一个会话界面上。
        """
        q = queue.Queue()
        sink = make_sse_callbacks(q, _FakeRunner(OutputSink(), session_id="sess-A"))

        sink.on_text("x")
        sink.on_thinking("y")
        sink.on_tool_call("bash", {}, "t1")
        sink.on_tool_result("bash", "out", False, "t1")
        sink.on_session_start("e1", "task")
        sink.on_session_complete("e1")

        frames = _drain(q)
        assert len(frames) == 6
        for frame in frames:
            payload = json.loads(frame[len("data: "):].strip())
            assert payload["session_id"] == "sess-A", payload

    def test_retry_clear_frame_carries_session_id(self):
        from core.base_session_runner import RETRY_CLEAR_SENTINEL

        q = queue.Queue()
        sink = make_sse_callbacks(q, _FakeRunner(OutputSink(), session_id="sess-B"))
        sink.on_text(RETRY_CLEAR_SENTINEL)

        payload = json.loads(_drain(q)[0][len("data: "):].strip())
        assert payload["type"] == "retry_clear"
        assert payload["session_id"] == "sess-B"


class TestTurnCompleteEvent:
    """回合结束信号：落盘后广播到全局流，供刷新后的界面自愈。"""

    def test_bind_default_sink_publishes_turn_complete(self):
        from core.event_bus import get_event_bus
        from web.deps import _bind_default_sink

        bus = get_event_bus()
        q: queue.Queue = queue.Queue()
        bus.subscribe(q, "ws-turn", "sess-turn")
        try:
            runner = SimpleNamespace(default_sink=None, sink=None)
            _bind_default_sink(runner, "ws-turn", "sess-turn")

            runner.default_sink.on_turn_complete()

            ev = q.get_nowait()
            assert ev["type"] == "turn_complete"
            assert ev["workspace_uuid"] == "ws-turn"
            assert ev["session_id"] == "sess-turn"
        finally:
            bus.unsubscribe(q)

    def test_turn_complete_filtered_by_session(self):
        """事件按 session 过滤：其他会话的订阅者不应收到。"""
        from core.event_bus import get_event_bus
        from web.deps import _bind_default_sink

        bus = get_event_bus()
        q: queue.Queue = queue.Queue()
        bus.subscribe(q, "ws-turn", "other-session")
        try:
            runner = SimpleNamespace(default_sink=None, sink=None)
            _bind_default_sink(runner, "ws-turn", "sess-turn")
            runner.default_sink.on_turn_complete()

            assert q.empty()
        finally:
            bus.unsubscribe(q)
