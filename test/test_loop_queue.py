"""Loop 排队投递边界测试（假 LLM 序列，不触网）。

覆盖设计 §5.3：迭代顶部投递、自然结束出口投递并 continue、暂停不投递、
autonomous 不消费、sink.on_user_message 载荷。
"""

from types import SimpleNamespace

from core.output_sink import OutputSink
from core.turn_queue import QueuedTurn
from core.turn_queue import queue as turn_queue


class _Resp:
    def __init__(self, text="", tool_calls=None):
        self.model = "fake-model"
        self._text = text
        self._tool_calls = tool_calls or []

    def get_text(self):
        return self._text

    def get_tool_calls(self):
        return self._tool_calls

    def content_as_dicts(self):
        return [{"type": "text", "text": self._text}] if self._text else []


def _enqueue(key, content):
    turn_queue.enqueue(key, QueuedTurn(item_id="", content=content, user_input=content))


def _key(runner):
    return f"{runner.workspace_uuid}:{runner.current_session_id}"


class TestQueueDeliveryBoundaries:
    def test_iteration_top_merges_before_first_llm_call(self, runner):
        key = _key(runner)
        _enqueue(key, "排队一")
        _enqueue(key, "排队二")
        calls = []

        def fake_llm(**kwargs):
            calls.append(1)
            return _Resp("收到")

        runner._call_llm = fake_llm
        try:
            runner.run("开始")
        finally:
            turn_queue.remove_session(key)

        user_msgs = [m for m in runner.messages if m["role"] == "user"]
        assert [m["content"] for m in user_msgs] == ["开始", "排队一\n\n排队二"]
        merged = user_msgs[-1]
        assert merged["_meta"]["merged_count"] == 2
        assert len(merged["_meta"]["queued_ids"]) == 2
        assert len(calls) == 1

    def test_natural_stop_delivers_and_continues(self, runner):
        key = _key(runner)
        calls = []

        def fake_llm(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                # 第一轮进行中用户发来消息（模拟运行中入队）
                _enqueue(key, "运行中补充")
                return _Resp("第一轮")
            return _Resp("第二轮")

        runner._call_llm = fake_llm
        try:
            runner.run("开始")
        finally:
            turn_queue.remove_session(key)

        roles_contents = [(m["role"], m["content"]) for m in runner.messages]
        assert roles_contents == [
            ("user", "开始"),
            ("assistant", [{"type": "text", "text": "第一轮"}]),
            ("user", "运行中补充"),
            ("assistant", [{"type": "text", "text": "第二轮"}]),
        ]
        assert len(calls) == 2

    def test_paused_queue_not_delivered(self, runner):
        key = _key(runner)
        _enqueue(key, "排队")
        turn_queue.pause(key, "stopped")
        runner._call_llm = lambda **kw: _Resp("完成")
        try:
            runner.run("开始")
        finally:
            turn_queue.remove_session(key)

        assert [m["role"] for m in runner.messages] == ["user", "assistant"]

    def test_on_user_message_sink_gets_batch_payload(self, runner):
        key = _key(runner)
        _enqueue(key, "排队")
        delivered = []
        runner._call_llm = lambda **kw: _Resp("完成")
        try:
            runner.run("开始", sink=OutputSink(on_user_message=delivered.append))
        finally:
            turn_queue.remove_session(key)

        assert len(delivered) == 1
        payload = delivered[0]
        assert payload["merged_count"] == 1
        assert payload["content"] == "排队"
        assert payload["item_ids"]


class TestAutonomousDoesNotDrain:
    def test_autonomous_runner_returns_zero(self):
        from core.base_session_runner import BaseSessionRunner

        fake = SimpleNamespace(
            _mode="autonomous", workspace_uuid="ws",
            current_session_id="sess", _session_id="sess",
        )
        assert BaseSessionRunner._drain_queued_user_messages(fake) == 0
