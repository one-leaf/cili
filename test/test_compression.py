"""Tests for core/compression.py."""

import json
from unittest.mock import MagicMock

import pytest

from core.compression import (
    count_messages_tokens,
    count_tokens_approx,
    microcompact_tool_results,
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


class TestMicrocompactToolResults:
    """microcompact_tool_results() 保留最近 N 条，标记更早的为已压缩。"""

    def _make_tool_result_msg(self, content: str, file_size: int = 0) -> dict:
        """创建工具结果消息。file_size 模拟外部文件大小。

        使用新格式：block 级别的 _meta.file_size, _meta.compacted
        """
        block = {
            "type": "tool_result",
            "tool_use_id": "x",
        }
        if file_size > 0:
            block["_meta"] = {"file_size": file_size}
        return {
            "role": "user",
            "content": [block],
        }

    def _make_text_msg(self, role: str, text: str) -> dict:
        return {"role": role, "content": text}

    def test_no_compression_when_few_results(self):
        """工具结果数 <= keep_recent 时不压缩。"""
        messages = [
            self._make_text_msg("user", "question"),
            self._make_tool_result_msg("output 1"),
            self._make_tool_result_msg("output 2"),
        ]
        saved = microcompact_tool_results(messages, keep_recent=6)
        assert saved == 0
        # 内容未被标记为压缩（新格式：block 级别 _meta.compacted）
        assert not messages[1]["content"][0].get("_meta", {}).get("compacted")

    def test_compresses_old_results(self):
        """超过 keep_recent 的旧工具结果被标记为已压缩。"""
        messages = [
            self._make_text_msg("user", "question"),
            self._make_tool_result_msg("old output", file_size=200),  # 要压缩
            self._make_text_msg("assistant", "reply"),
            self._make_tool_result_msg("recent output"),  # 保留
        ]
        saved = microcompact_tool_results(messages, keep_recent=1)
        assert saved == 200  # 基于 block 级别 _meta.file_size
        # 旧的被标记为已压缩（新格式：block 级别 _meta.compacted）
        assert messages[1]["content"][0]["_meta"]["compacted"] is True
        # 最近的不被压缩
        assert not messages[3]["content"][0].get("_meta", {}).get("compacted")

    def test_already_compacted_skipped(self):
        """已压缩的消息不重复压缩。"""
        messages = [
            self._make_tool_result_msg("old output", file_size=100),
            self._make_tool_result_msg("recent output"),
        ]
        # 先压缩一次
        microcompact_tool_results(messages, keep_recent=1)
        # 再压缩一次，saved 应该为 0（已压缩过的跳过）
        saved2 = microcompact_tool_results(messages, keep_recent=1)
        assert saved2 == 0

    def test_no_tool_results(self):
        """没有工具结果消息时返回 0。"""
        messages = [
            self._make_text_msg("user", "hello"),
            self._make_text_msg("assistant", "hi"),
        ]
        saved = microcompact_tool_results(messages)
        assert saved == 0

    def test_empty_messages(self):
        saved = microcompact_tool_results([])
        assert saved == 0


class TestMicrocompactInline:
    """内联结果（file_size=0）压缩只标记 compacted，不 spill 外置文件（jsonl 保留原文）。"""

    def _make_inline_msg(self, tool_use_id: str, content: str, seq: int | None = 0) -> dict:
        msg = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                }
            ],
        }
        if seq is not None:
            msg["_meta"] = {"seq": seq}
        return msg

    def test_inline_compacted_no_file_created(self, tmp_path):
        """内联结果（已持久化）压缩：标记 compacted + 清 content，不写外置文件。"""
        messages = [
            self._make_inline_msg("toolu_abc", "long inline output " * 50, seq=0),
            self._make_inline_msg("toolu_recent", "recent", seq=1),
        ]
        saved = microcompact_tool_results(messages, keep_recent=1)

        old = messages[0]["content"][0]
        assert old["_meta"]["compacted"] is True
        assert "output_path" not in old["_meta"]
        assert old.get("content") is None
        assert saved == len("long inline output " * 50)

        # 不生成外置文件
        assert list(tmp_path.iterdir()) == []

        # 最近一条不压缩
        recent = messages[1]["content"][0]
        assert not recent.get("_meta", {}).get("compacted")
        assert recent["content"] == "recent"

    def test_inline_unpersisted_skipped(self):
        """未持久化（无 _meta.seq）的内联结果跳过压缩，避免清空后原文丢失。"""
        messages = [
            self._make_inline_msg("toolu_abc", "inline output " * 20, seq=None),
            self._make_inline_msg("toolu_recent", "recent", seq=1),
        ]
        saved = microcompact_tool_results(messages, keep_recent=1)

        assert saved == 0
        old = messages[0]["content"][0]
        assert not old.get("_meta", {}).get("compacted")
        assert old["content"] == "inline output " * 20

    def test_inline_small_result_skipped(self):
        """小于 200 字符的内联结果保留原文，不压缩。"""
        messages = [
            self._make_inline_msg("toolu_abc", "small", seq=0),
            self._make_inline_msg("toolu_recent", "recent", seq=1),
        ]
        saved = microcompact_tool_results(messages, keep_recent=1)

        assert saved == 0
        old = messages[0]["content"][0]
        assert not old.get("_meta", {}).get("compacted")
        assert old["content"] == "small"

    def test_file_backed_marks_only(self):
        """外部文件结果（file_size>0）：只标记 compacted，content 保持原样。"""
        messages = [
            self._make_inline_msg("toolu_abc", "ignored", seq=0),
            self._make_inline_msg("toolu_recent", "recent", seq=1),
        ]
        messages[0]["content"][0]["_meta"] = {"file_size": 300}
        saved = microcompact_tool_results(messages, keep_recent=1)

        assert saved == 300
        old = messages[0]["content"][0]
        assert old["_meta"]["compacted"] is True
        assert old["content"] == "ignored"

