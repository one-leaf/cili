"""Tests for core/compression.py."""

import json
from unittest.mock import MagicMock

import pytest

from core.compression import (
    count_messages_tokens,
    count_tokens_approx,
    microcompact_mark_orphans_and_errors,
)


class TestCountTokensApprox:
    """count_tokens_approx() 对不同内容的估算。"""

    def test_empty_string(self):
        assert count_tokens_approx("") == 0

    def test_pure_english(self):
        # 英文约 4 字符/token
        text = "hello world"  # 11 chars
        tokens = count_tokens_approx(text)
        assert tokens == int(11 / 4)

    def test_pure_chinese(self):
        # 中文约 2.5 字符/token
        text = "你好世界测试"  # 6 中文字
        tokens = count_tokens_approx(text)
        assert tokens == int(6 / 2.5)

    def test_mixed(self):
        text = "你好hello世界world"
        # 4 中文字 + 10 英文字符
        tokens = count_tokens_approx(text)
        expected = int(4 / 2.5 + 10 / 4)
        assert tokens == expected

    def test_consistency_with_root_agent(self):
        """确保和 root_agent._count_tokens 使用相同比率 (2.5)。"""
        # 验证中文比率是 2.5 而不是 2
        text = "中文测试内容"  # 6 中文字
        tokens = count_tokens_approx(text)
        assert tokens == int(6 / 2.5)  # = 2，如果是 /2 则 = 3


class TestCountMessagesTokens:
    """count_messages_tokens() 估算消息列表的总 token 数。"""

    def test_empty_messages(self):
        assert count_messages_tokens([]) == 0

    def test_string_content(self):
        messages = [{"role": "user", "content": "hello world"}]
        tokens = count_messages_tokens(messages)
        assert tokens == count_tokens_approx("hello world")

    def test_list_content_text_blocks(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ]}]
        tokens = count_messages_tokens(messages)
        assert tokens > 0

    def test_tool_result_string(self):
        messages = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "content": "some output"},
        ]}]
        tokens = count_messages_tokens(messages)
        assert tokens == count_tokens_approx("some output")

    def test_tool_result_multimodal(self):
        messages = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "content": [
                {"type": "text", "text": "result text"},
                {"type": "image", "source": {"data": "base64data" * 100}},
            ]},
        ]}]
        tokens = count_messages_tokens(messages)
        assert tokens > 0

    def test_tool_use(self):
        messages = [{"role": "assistant", "content": [
            {"type": "tool_use", "name": "bash", "input": {"command": "ls -la"}},
        ]}]
        tokens = count_messages_tokens(messages)
        assert tokens > 0


class TestMicrocompactOrphans:
    """microcompact_mark_orphans_and_errors() 任务1：标记孤立 tool_result。"""

    def _make_tool_use_msg(self, tool_use_id: str) -> dict:
        return {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": "bash", "input": {}}],
        }

    def _make_tool_result_msg(self, tool_use_id: str, is_error: bool = False) -> dict:
        return {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "output", "is_error": is_error}],
        }

    def test_orphan_marked_invalid(self):
        """tool_result 无对应 tool_use 时，整条 user 消息标记 valid=False。"""
        messages = [
            self._make_tool_result_msg("orphan_1"),  # 无对应 tool_use
        ]
        count = microcompact_mark_orphans_and_errors(messages)
        assert count == 1
        assert messages[0]["_meta"]["valid"] is False

    def test_paired_not_invalidated(self):
        """有对应 tool_use 的 tool_result 不被标记。"""
        messages = [
            self._make_tool_use_msg("call_1"),
            self._make_tool_result_msg("call_1"),
        ]
        count = microcompact_mark_orphans_and_errors(messages)
        assert count == 0
        assert not messages[1].get("_meta", {}).get("valid")

    def test_orphan_after_tool_use_invalidated(self):
        """当 tool_use 所在 assistant 消息已被标记失效，tool_result 成为孤立。"""
        messages = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "bash", "input": {}}],
             "_meta": {"valid": False}},
            self._make_tool_result_msg("call_1"),  # 对应 tool_use 已失效 → 孤立
        ]
        count = microcompact_mark_orphans_and_errors(messages)
        assert count == 1
        assert messages[1]["_meta"]["valid"] is False

    def test_already_invalidated_skipped(self):
        """已标记失效的消息不重复计数。"""
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": ""}],
             "_meta": {"valid": False}},
        ]
        count = microcompact_mark_orphans_and_errors(messages)
        assert count == 0


class TestMicrocompactErrors:
    """microcompact_mark_orphans_and_errors() 任务2：标记旧错误结果对。"""

    def _make_pair(self, tool_use_id: str, is_error: bool = False) -> tuple[dict, dict]:
        use_msg = {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": "bash", "input": {}}],
        }
        result_msg = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "err" if is_error else "ok", "is_error": is_error}],
        }
        return use_msg, result_msg

    def test_error_pairs_within_limit_not_invalidated(self):
        """错误结果对数量 <= 10 时不标记。"""
        messages = []
        for i in range(5):
            use, result = self._make_pair(f"call_{i}", is_error=True)
            messages.extend([use, result])
        count = microcompact_mark_orphans_and_errors(messages)
        # 这5对错误结果都在保留范围内，不标记（但注意：它们都有对应的有效 tool_use，不会成为孤立）
        assert count == 0

    def test_error_pairs_over_limit_invalidated(self):
        """超过10对的错误结果对，最早的被整对标记失效。"""
        messages = []
        for i in range(12):
            use, result = self._make_pair(f"call_{i}", is_error=True)
            messages.extend([use, result])
        count = microcompact_mark_orphans_and_errors(messages)
        # 12对 > 10，最早的2对被标记（每对2条消息 = 4条）
        assert count == 4
        assert messages[0]["_meta"]["valid"] is False   # call_0 tool_use
        assert messages[1]["_meta"]["valid"] is False   # call_0 tool_result
        assert messages[2]["_meta"]["valid"] is False   # call_1 tool_use
        assert messages[3]["_meta"]["valid"] is False   # call_1 tool_result
        # 第3对及之后保留
        assert not messages[4].get("_meta", {}).get("valid")

    def test_non_error_results_not_affected(self):
        """非错误结果不参与错误对标记。"""
        messages = []
        for i in range(12):
            use, result = self._make_pair(f"call_{i}", is_error=False)
            messages.extend([use, result])
        count = microcompact_mark_orphans_and_errors(messages)
        # 无错误结果，不标记（也无孤立）
        assert count == 0

    def test_empty_messages(self):
        assert microcompact_mark_orphans_and_errors([]) == 0


