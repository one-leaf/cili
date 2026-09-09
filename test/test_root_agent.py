"""Tests for core/root_agent.py — pure logic methods.

注意：大部分方法通过 agent fixture 测试（需要 API key）。
无 API key 时自动 skip。
"""

import pytest

from core.root_agent import RootAgent


class TestIterContentBlocks:
    """iter_content_blocks() 静态方法，直接测试无需 agent 实例。"""

    def test_string_content(self):
        messages = [{"role": "user", "content": "hello"}]
        blocks = list(RootAgent.iter_content_blocks(messages))
        assert blocks == [("text", "hello")]

    def test_list_content_text(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "part1"},
            {"type": "text", "text": "part2"},
        ]}]
        blocks = list(RootAgent.iter_content_blocks(messages))
        assert ("text", "part1") in blocks
        assert ("text", "part2") in blocks

    def test_tool_use_block(self):
        tool_block = {"type": "tool_use", "name": "bash", "input": {"command": "ls"}}
        messages = [{"role": "assistant", "content": [tool_block]}]
        blocks = list(RootAgent.iter_content_blocks(messages))
        assert ("tool_use", tool_block) in blocks

    def test_tool_result_string(self):
        messages = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "content": "output text"},
        ]}]
        blocks = list(RootAgent.iter_content_blocks(messages))
        assert ("tool_result_str", "output text") in blocks

    def test_tool_result_multimodal(self):
        messages = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "content": [
                {"type": "text", "text": "result text"},
                {"type": "image", "source": {"data": "base64data"}},
            ]},
        ]}]
        blocks = list(RootAgent.iter_content_blocks(messages))
        assert ("tool_result_text", "result text") in blocks
        assert ("tool_result_image", "base64data") in blocks

    def test_empty_messages(self):
        assert list(RootAgent.iter_content_blocks([])) == []

    def test_multiple_messages(self):
        messages = [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]
        blocks = list(RootAgent.iter_content_blocks(messages))
        assert len(blocks) == 2


class TestCountTokens:
    """_count_messages_tokens() 委托 compression.count_messages_tokens。"""

    def test_empty(self, agent):
        assert agent._count_messages_tokens([]) == 0

    def test_english(self, agent):
        from core.compression import count_messages_tokens
        messages = [{"role": "user", "content": "hello world"}]
        assert agent._count_messages_tokens(messages) == count_messages_tokens(messages)

    def test_chinese(self, agent):
        messages = [{"role": "user", "content": "中文测试内容"}]
        # 6 中文字 / 2.5 = 2
        assert agent._count_messages_tokens(messages) == 2

    def test_reasoning_blocks_counted(self, agent):
        """reasoning/thinking 块计入估算（统一前 base_agent 版本会漏算）。"""
        messages = [{"role": "assistant", "content": [{"type": "reasoning", "text": "思考内容"}]}]
        assert agent._count_messages_tokens(messages) > 0

    def test_consistency_with_compression(self, agent):
        """root_agent._count_messages_tokens 和 compression.count_messages_tokens 一致。"""
        from core.compression import count_messages_tokens
        text = "混合mixed测试text内容data"
        messages = [{"role": "user", "content": text}]
        assert agent._count_messages_tokens(messages) == count_messages_tokens(messages)


class TestFindSplitByUserMessages:
    """_find_split_by_user_messages() — backward walk 保证不拆散 tool pair。"""

    def _msg(self, role, content):
        return {"role": role, "content": content}

    def _tool_use_msg(self):
        return {"role": "assistant", "content": [
            {"type": "tool_use", "name": "bash", "input": {"command": "ls"}, "id": "t1"},
        ]}

    def _tool_result_msg(self):
        return {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "output"},
        ]}

    def test_no_split_when_few_user_messages(self, agent):
        """用户消息 <= keep_user_count 时返回 0。"""
        messages = [
            self._msg("user", "q1"),
            self._msg("assistant", "a1"),
        ]
        assert agent._find_split_by_user_messages(messages, keep_user_count=3) == 0

    def test_basic_split(self, agent):
        """基本分割：保留最后 N 条用户消息。"""
        messages = [
            self._msg("user", "old q1"),       # 0 - 压缩区
            self._msg("assistant", "old a1"),   # 1 - 压缩区
            self._msg("user", "recent q1"),     # 2 - 保留区
            self._msg("assistant", "recent a1"),# 3 - 保留区
        ]
        split = agent._find_split_by_user_messages(messages, keep_user_count=1)
        assert split == 2  # messages[0:2] 压缩，messages[2:] 保留

    def test_does_not_split_tool_chain_from_above(self, agent):
        """backward walk 不会把 tool_result 留在保留区而 tool_use 在压缩区。"""
        messages = [
            self._msg("user", "old q1"),          # 0 - 压缩区
            self._msg("assistant", "old a1"),      # 1 - 压缩区
            self._msg("user", "recent q1"),        # 2 - 这是第2个 user text
            self._msg("assistant", "a2"),           # 3
            self._tool_use_msg(),                   # 4
            self._tool_result_msg(),                # 5
            self._msg("user", "final q"),           # 6 - 这是第3个 user text (keep=2 时起点)
        ]
        # keep_user_count=2，起点是 messages[6]（第3个 user text）
        # backward walk 应该跳过 tool chain，不会在 tool_result 和 tool_use 之间分裂
        split = agent._find_split_by_user_messages(messages, keep_user_count=2)
        # 验证：split 之前的 tool_use 和 tool_result 都在同一侧
        # 即 split 不会落在 4 和 5 之间，也不会落在 5 和 6 之间
        assert split <= 4 or split >= 6  # 不拆散 chain

    def test_does_not_split_tool_chain_backward(self, agent):
        """backward walk 把整个 tool chain 都推到同一侧。"""
        messages = [
            self._msg("user", "old q1"),          # 0
            self._msg("assistant", "old a1"),      # 1
            self._tool_use_msg(),                   # 2
            self._tool_result_msg(),                # 3
            self._msg("user", "recent q"),          # 4 - keep_user_count=1 时起点
        ]
        split = agent._find_split_by_user_messages(messages, keep_user_count=1)
        # split 应该在 4，即 tool chain 全部在压缩区
        assert split == 4

    def test_multiple_tool_chains(self, agent):
        """多个 tool chain 场景。"""
        messages = [
            self._msg("user", "q1"),              # 0
            self._msg("assistant", "a1"),          # 1
            self._tool_use_msg(),                   # 2
            self._tool_result_msg(),                # 3
            self._msg("assistant", "a2"),           # 4
            self._tool_use_msg(),                   # 5
            self._tool_result_msg(),                # 6
            self._msg("user", "q2"),                # 7
        ]
        # keep_user_count=1，起点是 messages[7]
        split = agent._find_split_by_user_messages(messages, keep_user_count=1)
        assert split == 7  # tool chain 全部在压缩区


class TestEstimateRequestBodySize:
    """_estimate_request_body_size() 估算请求体大小。"""

    def test_simple_messages(self, agent):
        messages = [{"role": "user", "content": "hello"}]
        size = agent._estimate_request_body_size(messages)
        assert size > 0
        # 至少包含 "hello" 的字节数
        assert size >= len("hello".encode("utf-8"))

    def test_empty_messages(self, agent):
        size = agent._estimate_request_body_size([])
        # 即使空消息列表也有基础 JSON 开销
        assert size >= 0

    def test_large_content_increases_size(self, agent):
        small = [{"role": "user", "content": "hi"}]
        large = [{"role": "user", "content": "x" * 10000}]
        assert agent._estimate_request_body_size(large) > agent._estimate_request_body_size(small)


class TestPadDanglingToolResults:
    """_pad_dangling_tool_results()：中断后为悬挂的 tool_use 补占位 tool_result"""

    def _assistant_tool_use(self, tool_use_id, meta_id):
        return {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": "bash", "input": {}}],
            "_meta": {"id": meta_id},
        }

    def _user_tool_result(self, tool_use_id, meta_id):
        return {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}],
            "_meta": {"id": meta_id},
        }

    def test_pads_dangling_tool_use(self, agent):
        """悬挂的 tool_use 在其后插入 is_error 占位结果"""
        agent.messages.clear()
        agent.messages.extend([
            self._assistant_tool_use("tu1", "a1"),
            self._user_tool_result("tu1", "u1"),
            self._assistant_tool_use("tu2", "a2"),
            {"role": "user", "content": "继续", "_meta": {"id": "u2"}},
        ])

        agent._pad_dangling_tool_results()

        # 占位结果应插入在 a2（索引 2）之后、原 user 消息之前
        padded = agent.messages[3]
        assert padded["role"] == "user"
        assert padded["content"][0]["type"] == "tool_result"
        assert padded["content"][0]["tool_use_id"] == "tu2"
        assert padded["content"][0]["is_error"] is True
        assert len(agent.messages) == 5

    def test_no_pad_when_all_paired(self, agent):
        """全部配对时不插入任何消息"""
        agent.messages.clear()
        agent.messages.extend([
            self._assistant_tool_use("tu1", "a1"),
            self._user_tool_result("tu1", "u1"),
        ])

        before = len(agent.messages)
        agent._pad_dangling_tool_results()
        assert len(agent.messages) == before

    def test_idempotent(self, agent):
        """重复调用不会重复插入"""
        agent.messages.clear()
        agent.messages.extend([
            self._assistant_tool_use("tu1", "a1"),
        ])

        agent._pad_dangling_tool_results()
        count_after_first = len(agent.messages)
        agent._pad_dangling_tool_results()
        assert len(agent.messages) == count_after_first

    def test_invalid_messages_skipped(self, agent):
        """标记 valid=False 的消息不参与配对检查"""
        dangling = self._assistant_tool_use("tu1", "a1")
        dangling["_meta"]["valid"] = False
        agent.messages.clear()
        agent.messages.extend([dangling])

        agent._pad_dangling_tool_results()
        assert len(agent.messages) == 1
