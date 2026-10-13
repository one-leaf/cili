"""Step 2 契约测试：ConversationStore 三层抽层后的消息状态逻辑。

覆盖：add_message 结构/脏标记、get_valid_messages 过滤与剥 meta、
invalidate/pad 原地修改、save/load 持久化、usage 累计、
BaseSessionRunner 属性转发（messages/_usage 与 context 同引用）、
同输入序列 messages 深度相等。
"""

import json

from core.session_runner_runtime.context import ConversationStore
from core.base_session_runner import BaseSessionRunner
from test.conftest import make_dgx_config


class _FakeSM:
    """最小 session 替身：记录 mark_dirty/flush/save 调用。"""

    def __init__(self):
        self.messages = []
        self.metadata = {}
        self.mark_dirty_count = 0
        self.flush_count = 0
        self.save_count = 0

    def mark_dirty(self):
        self.mark_dirty_count += 1

    def add_message(self, role, content, meta=None, flush=False):
        """对齐 SessionStore.add_message 契约（ConversationStore 委托调用）。"""
        meta = dict(meta) if meta else {}
        meta.setdefault("id", "fake-id")
        self.messages.append({"role": role, "content": content, "_meta": meta})
        self.mark_dirty()

    def flush(self):
        self.flush_count += 1

    def save(self):
        self.save_count += 1

    def get_usage(self):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "api_calls": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }


def _strip_ids(messages):
    """剥除自动生成的 _meta.id，用于跨实例深度比较。"""
    return [
        {**m, "_meta": {k: v for k, v in m.get("_meta", {}).items() if k != "id"}}
        for m in messages
    ]


class TestContextUnit:
    def test_add_message_builds_structure(self):
        ctx = ConversationStore()
        ctx.add_message("user", "hello")
        assert len(ctx.messages) == 1
        msg = ctx.messages[0]
        assert msg["role"] == "user"
        assert msg["content"] == "hello"
        assert "id" in msg["_meta"]

    def test_add_message_marks_dirty_no_flush(self):
        sm = _FakeSM()
        ctx = ConversationStore(session=sm)
        ctx.add_message("user", "hi")
        assert sm.mark_dirty_count == 1
        assert sm.flush_count == 0  # 批量落盘：逐条不 flush，由迭代/回合边界 flush()/save() 收尾
        # 同一引用不变式由 agent.py 的 `self.messages = session_manager.messages`
        # 重绑建立，见 TestAgentForwarding.test_messages_property_shares_reference

    def test_add_message_no_sm_skips_persist(self):
        ctx = ConversationStore()
        ctx.add_message("user", "hi")
        assert len(ctx.messages) == 1

    def test_get_valid_messages_filters_and_strips(self):
        ctx = ConversationStore()
        ctx.messages = [
            {"role": "user", "content": "a", "_meta": {"id": "x1"}},
            {"role": "user", "content": "b", "_meta": {"id": "x2", "valid": False}},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "t", "_meta": {"id": "b1"}}],
                "_meta": {"id": "x3", "pinned": True},
            },
        ]
        valid = ctx.get_valid_messages()
        assert len(valid) == 2
        assert valid[0] == {"role": "user", "content": "a"}
        # 消息级剥除内部字段，保留非内部 _meta
        assert valid[1]["_meta"] == {"pinned": True}
        # 块级剥除 _meta
        assert valid[1]["content"][0] == {"type": "text", "text": "t"}

    def test_get_valid_messages_keeps_meta_when_strip_false(self):
        ctx = ConversationStore()
        ctx.messages = [
            {"role": "user", "content": "a", "_meta": {"id": "x1", "output_path": "/tmp/o"}},
        ]
        valid = ctx.get_valid_messages(strip_meta=False)
        assert valid[0]["_meta"] == {"id": "x1", "output_path": "/tmp/o"}

    def test_invalidate_all_messages(self):
        ctx = ConversationStore()
        ctx.messages = [
            {"role": "user", "content": "a", "_meta": {"id": "x1"}},
            {"role": "user", "content": "b", "_meta": {"id": "x2", "valid": False}},
        ]
        assert ctx.invalidate_all_messages() == 1
        assert ctx.messages[0]["_meta"]["valid"] is False

    def test_pad_dangling_tool_results(self):
        ctx = ConversationStore()
        ctx.messages = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu1", "name": "bash", "input": {}}],
                "_meta": {"id": "x1"},
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tu2", "content": "ok"}],
                "_meta": {"id": "x2"},
            },
        ]
        ctx.pad_dangling_tool_results()
        # 悬挂的 tu1 得到占位；已回应的 tu2 不受影响
        assert len(ctx.messages) == 3
        placeholder = ctx.messages[1]
        assert placeholder["role"] == "user"
        assert placeholder["content"][0]["tool_use_id"] == "tu1"
        assert placeholder["content"][0]["is_error"] is True
        assert "id" in placeholder["_meta"]

    def test_save_load_legacy_roundtrip(self, tmp_path):
        ctx = ConversationStore(session_dir=tmp_path / "sess")
        ctx.messages = [{"role": "user", "content": "hi", "_meta": {"id": "m1"}}]
        ctx.save_messages(session_id="sess123")
        data = json.loads((tmp_path / "sess" / "index.json").read_text(encoding="utf-8"))
        assert data["session_id"] == "sess123"
        assert data["messages"] == ctx.messages

        ctx2 = ConversationStore(session_dir=tmp_path / "sess")
        assert ctx2.load_messages()
        assert ctx2.messages == ctx.messages

    def test_save_routes_to_session_manager(self):
        sm = _FakeSM()
        ctx = ConversationStore(session=sm)
        ctx.save_messages()
        assert sm.save_count == 1

    def test_update_usage_accumulates(self):
        ctx = ConversationStore()
        ctx.update_usage(input_tokens=10, output_tokens=5, api_calls=1)
        usage = ctx.get_usage()
        assert usage["input_tokens"] == 10
        assert usage["api_calls"] == 1
        ctx.update_usage(input_tokens=3)
        assert ctx.get_usage()["input_tokens"] == 13

    def test_speed_computed_per_call_with_duration(self):
        """带 duration 的调用按单次时段计速：ttft 拆分预填充与生成窗口。"""
        ctx = ConversationStore()
        ctx.update_usage(
            input_tokens=60000, output_tokens=300, api_calls=1,
            duration=3.0, ttft=1.0,
        )
        usage = ctx.get_usage()
        # 预填充 = 60000 / 1.0s，生成 = 300 / 2.0s
        assert usage["prefill_speed"] == 60000.0
        assert usage["generation_speed"] == 150.0

        # 第二次调用并入滚动平均（时长加权）
        ctx.update_usage(
            input_tokens=10000, output_tokens=100, api_calls=1,
            duration=2.0, ttft=1.0,
        )
        merged = ctx.get_usage()
        # 预填充：(60000*1 + 10000*1) / 2 = 35000；生成：(150*2 + 100*1) / 3 ≈ 133.33
        assert merged["prefill_speed"] == 35000.0
        assert merged["generation_speed"] == round((150 * 2 + 100 * 1) / 3, 2)

    def test_no_duration_keeps_speeds(self):
        """不带 duration 的调用只累计计数器，不影响已有速度。"""
        ctx = ConversationStore()
        ctx.update_usage(
            input_tokens=60000, output_tokens=300, api_calls=1,
            duration=3.0, ttft=1.0,
        )
        before = ctx.get_usage()
        ctx.update_usage(input_tokens=10, output_tokens=5, api_calls=1)
        after = ctx.get_usage()
        assert after["prefill_speed"] == before["prefill_speed"]
        assert after["generation_speed"] == before["generation_speed"]
        assert after["input_tokens"] == 60010

    def test_speeds_survive_restore_no_zero_overwrite(self):
        """回归：重启后恢复的 runner 读取 usage 不得把速度归零（183a9ad0 bug）。

        旧实现速度由进程内首尾时间戳推导，恢复后的 runner 时间戳为空，
        一次 sync/get_usage 就把持久化速度覆盖为 0。
        """
        ctx = ConversationStore()
        ctx.update_usage(
            input_tokens=546084, output_tokens=35156, api_calls=47,
            duration=10.0, ttft=1.0,
        )
        persisted = ctx.get_usage()
        assert persisted["prefill_speed"] > 0

        # 模拟重启：新 runner 从 session 恢复 _usage（含速度），无任何时间戳
        restored = ConversationStore()
        restored._usage = dict(persisted)
        # 只读路径（/status 等）多次读取，速度保持不变
        for _ in range(3):
            assert restored.get_usage()["prefill_speed"] == persisted["prefill_speed"]
            assert restored.get_usage()["generation_speed"] == persisted["generation_speed"]

        # sync 回写也不丢速度
        sm = _FakeSM()
        restored.session = sm
        restored.sync_to_session()
        assert sm.metadata["usage"]["prefill_speed"] == persisted["prefill_speed"]

    def test_sync_to_session(self):
        sm = _FakeSM()
        ctx = ConversationStore(session=sm)
        ctx.update_usage(input_tokens=1)
        ctx.sync_to_session()
        assert sm.metadata["usage"]["input_tokens"] == 1
        assert sm.mark_dirty_count == 1
        assert "updated_at" in sm.metadata

    def test_sync_no_sm_is_noop(self):
        ConversationStore().sync_to_session()  # 不应抛异常


class TestAgentForwarding:
    def test_messages_property_shares_reference(self, runner):
        assert runner.messages is runner.context.messages
        assert runner.messages is runner.session.messages  # 同一引用不变式

    def test_messages_rebind_updates_context(self, runner):
        new_list = [{"role": "user", "content": "x"}]
        runner.messages = new_list
        assert runner.context.messages is new_list

    def test_add_message_via_agent_lands_in_context_and_sm(self, runner):
        runner.add_message("user", "hello")
        assert runner.context.messages[-1]["content"] == "hello"
        assert runner.session.messages[-1]["content"] == "hello"

    def test_usage_property_forwards(self, runner):
        runner._update_usage(input_tokens=7)
        assert runner.context.get_usage()["input_tokens"] == 7

    def test_sync_forwards_to_context(self, runner):
        runner._update_usage(input_tokens=2)
        runner._sync_to_session()
        assert runner.session.metadata["usage"]["input_tokens"] == 2

    def test_same_input_sequence_deep_equal(self):
        """同输入序列：直连 BaseSessionRunner 与 ConversationStore 产物深度相等（剥 id 后）。"""
        config = make_dgx_config("anthropic")
        direct = BaseSessionRunner(config=config)
        ctx = ConversationStore()
        ops = [
            ("add", "user", "q1"),
            ("add", "assistant", [{"type": "tool_use", "id": "tu1", "name": "bash", "input": {}}]),
            ("add", "user", [{"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}]),
            ("add", "assistant", "done"),
            ("invalidate",),
        ]
        for op in ops:
            if op[0] == "add":
                direct.add_message(op[1], op[2])
                ctx.add_message(op[1], op[2])
            else:
                direct.invalidate_all_messages()
                ctx.invalidate_all_messages()
        assert _strip_ids(direct.messages) == _strip_ids(ctx.messages)

    def test_pad_through_agent_matches_context(self, runner):
        """runner._pad_dangling_tool_results 与 context 原地结果一致。"""
        direct = ConversationStore()
        runner.messages = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu9", "name": "bash", "input": {}}],
                "_meta": {"id": "x1"},
            }
        ]
        direct.messages = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu9", "name": "bash", "input": {}}],
                "_meta": {"id": "x1"},
            }
        ]
        runner._pad_dangling_tool_results()
        direct.pad_dangling_tool_results()
        assert _strip_ids(runner.messages) == _strip_ids(direct.messages)
