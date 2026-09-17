"""Step 2 契约测试：AgentContext 三层抽层后的消息状态逻辑。

覆盖：add_message 结构/脏标记、get_valid_messages 过滤与剥 meta、
invalidate/pad 原地修改、save/load 持久化、usage 累计、
BaseAgent 属性转发（messages/_usage 与 context 同引用）、
同输入序列 messages 深度相等。
"""

import json

from core.agent_runtime.context import AgentContext
from core.base_agent import BaseAgent
from test.conftest import make_dgx_config


class _FakeSM:
    """最小 session_manager 替身：记录 mark_dirty/flush/save 调用。"""

    def __init__(self):
        self.messages = []
        self.metadata = {}
        self.mark_dirty_count = 0
        self.flush_count = 0
        self.save_count = 0

    def mark_dirty(self):
        self.mark_dirty_count += 1

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
        ctx = AgentContext()
        ctx.add_message("user", "hello")
        assert len(ctx.messages) == 1
        msg = ctx.messages[0]
        assert msg["role"] == "user"
        assert msg["content"] == "hello"
        assert "id" in msg["_meta"]

    def test_add_message_marks_dirty_no_flush(self):
        sm = _FakeSM()
        ctx = AgentContext(session_manager=sm)
        ctx.add_message("user", "hi")
        assert sm.mark_dirty_count == 1
        assert sm.flush_count == 0  # 批量落盘：逐条不 flush，由迭代/回合边界 flush()/save() 收尾
        # 同一引用不变式由 agent.py 的 `self.messages = session_manager.messages`
        # 重绑建立，见 TestAgentForwarding.test_messages_property_shares_reference

    def test_add_message_no_sm_skips_persist(self):
        ctx = AgentContext()
        ctx.add_message("user", "hi")
        assert len(ctx.messages) == 1

    def test_get_valid_messages_filters_and_strips(self):
        ctx = AgentContext()
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
        ctx = AgentContext()
        ctx.messages = [
            {"role": "user", "content": "a", "_meta": {"id": "x1", "output_path": "/tmp/o"}},
        ]
        valid = ctx.get_valid_messages(strip_meta=False)
        assert valid[0]["_meta"] == {"id": "x1", "output_path": "/tmp/o"}

    def test_invalidate_all_messages(self):
        ctx = AgentContext()
        ctx.messages = [
            {"role": "user", "content": "a", "_meta": {"id": "x1"}},
            {"role": "user", "content": "b", "_meta": {"id": "x2", "valid": False}},
        ]
        assert ctx.invalidate_all_messages() == 1
        assert ctx.messages[0]["_meta"]["valid"] is False

    def test_pad_dangling_tool_results(self):
        ctx = AgentContext()
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
        ctx = AgentContext(session_dir=tmp_path / "sess")
        ctx.messages = [{"role": "user", "content": "hi", "_meta": {"id": "m1"}}]
        ctx.save_messages(session_id="sess123")
        data = json.loads((tmp_path / "sess" / "index.json").read_text(encoding="utf-8"))
        assert data["session_id"] == "sess123"
        assert data["messages"] == ctx.messages

        ctx2 = AgentContext(session_dir=tmp_path / "sess")
        assert ctx2.load_messages()
        assert ctx2.messages == ctx.messages

    def test_save_routes_to_session_manager(self):
        sm = _FakeSM()
        ctx = AgentContext(session_manager=sm)
        ctx.save_messages()
        assert sm.save_count == 1

    def test_update_usage_accumulates(self):
        ctx = AgentContext()
        ctx.update_usage(input_tokens=10, output_tokens=5, api_calls=1)
        usage = ctx.get_usage()
        assert usage["input_tokens"] == 10
        assert usage["api_calls"] == 1
        ctx.update_usage(input_tokens=3)
        assert ctx.get_usage()["input_tokens"] == 13

    def test_sync_to_session_manager(self):
        sm = _FakeSM()
        ctx = AgentContext(session_manager=sm)
        ctx.update_usage(input_tokens=1)
        ctx.sync_to_session_manager()
        assert sm.metadata["usage"]["input_tokens"] == 1
        assert sm.mark_dirty_count == 1
        assert "updated_at" in sm.metadata

    def test_sync_no_sm_is_noop(self):
        AgentContext().sync_to_session_manager()  # 不应抛异常


class TestAgentForwarding:
    def test_messages_property_shares_reference(self, agent):
        assert agent.messages is agent.context.messages
        assert agent.messages is agent.session_manager.messages  # 同一引用不变式

    def test_messages_rebind_updates_context(self, agent):
        new_list = [{"role": "user", "content": "x"}]
        agent.messages = new_list
        assert agent.context.messages is new_list

    def test_add_message_via_agent_lands_in_context_and_sm(self, agent):
        agent.add_message("user", "hello")
        assert agent.context.messages[-1]["content"] == "hello"
        assert agent.session_manager.messages[-1]["content"] == "hello"

    def test_usage_property_forwards(self, agent):
        agent._update_usage(input_tokens=7)
        assert agent.context.get_usage()["input_tokens"] == 7

    def test_sync_forwards_to_context(self, agent):
        agent._update_usage(input_tokens=2)
        agent._sync_to_session_manager()
        assert agent.session_manager.metadata["usage"]["input_tokens"] == 2

    def test_same_input_sequence_deep_equal(self):
        """同输入序列：直连 BaseAgent 与 AgentContext 产物深度相等（剥 id 后）。"""
        config = make_dgx_config("anthropic")
        direct = BaseAgent(config=config)
        ctx = AgentContext()
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

    def test_pad_through_agent_matches_context(self, agent):
        """agent._pad_dangling_tool_results 与 context 原地结果一致。"""
        direct = AgentContext()
        agent.messages = [
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
        agent._pad_dangling_tool_results()
        direct.pad_dangling_tool_results()
        assert _strip_ids(agent.messages) == _strip_ids(direct.messages)
