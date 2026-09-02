"""Tests for core/compression.py."""

import json
from unittest.mock import MagicMock

import pytest

from core.compression import (
    compact_messages_with_summary,
    count_messages_tokens,
    count_tokens_approx,
    microcompact_tool_results,
    summarize_messages_for_compact,
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

        使用新格式：_meta.file_size, _meta.compacted（消息级别）
        """
        return {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "x",
            }],
            "_meta": {
                "file_size": file_size,
                "compacted": False,
            }
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
        # 内容未被标记为压缩（新格式：_meta.compacted）
        assert not messages[1].get("_meta", {}).get("compacted")

    def test_compresses_old_results(self):
        """超过 keep_recent 的旧工具结果被标记为已压缩。"""
        messages = [
            self._make_text_msg("user", "question"),
            self._make_tool_result_msg("old output", file_size=200),  # 要压缩
            self._make_text_msg("assistant", "reply"),
            self._make_tool_result_msg("recent output"),  # 保留
        ]
        saved = microcompact_tool_results(messages, keep_recent=1)
        assert saved == 200  # 基于 _meta.file_size
        # 旧的被标记为已压缩（新格式：_meta.compacted）
        assert messages[1]["_meta"]["compacted"] is True
        # 最近的不被压缩
        assert not messages[3].get("_meta", {}).get("compacted")

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


class TestSummarizeMessagesForCompact:
    """summarize_messages_for_compact() 用 LLM 生成摘要。"""

    def test_success(self):
        """summarize_messages_for_compact() 成功调用 LLM。"""
        from core.llm import LLMResponse, TextBlock
        mock_client = MagicMock()
        mock_response = LLMResponse(content=[TextBlock(text="对话摘要内容")])
        mock_client.chat.return_value = mock_response

        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮你的？"},
        ]
        result = summarize_messages_for_compact(messages, mock_client)
        assert result == "对话摘要内容"
        mock_client.chat.assert_called_once()

    def test_llm_failure_returns_truncated(self):
        """LLM 调用失败时返回截断文本。"""
        mock_client = MagicMock()
        mock_client.chat.side_effect = Exception("API error")

        messages = [{"role": "user", "content": "hello world"}]
        result = summarize_messages_for_compact(messages, mock_client)
        assert "hello world" in result

    def test_long_conversation_truncated(self):
        """超长对话截断到 max_chars。"""
        from core.llm import LLMResponse, TextBlock
        mock_client = MagicMock()
        mock_response = LLMResponse(content=[TextBlock(text="摘要")])
        mock_client.chat.return_value = mock_response

        messages = [{"role": "user", "content": "x" * 100000}]
        summarize_messages_for_compact(messages, mock_client, max_chars=50000)
        # 验证传给 LLM 的 prompt 不超过 max_chars 太多
        call_args = mock_client.chat.call_args
        # messages 现在是 Message 对象，用属性访问
        msg = call_args[1]["messages"][0]
        prompt_text = msg.content if isinstance(msg.content, str) else str(msg.content)
        # prompt 包含截断标记
        assert "..." in prompt_text or len(prompt_text) < 100000


class TestCompactMessagesWithSummary:
    """compact_messages_with_summary() 保留最近 N 条，其余用摘要替换。"""

    def test_too_few_messages_no_compress(self):
        """消息数 <= keep_recent_count + 1 时不压缩。"""
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "bye"},
        ]
        mock_client = MagicMock()
        saved = compact_messages_with_summary(messages, mock_client, keep_recent_count=6)
        assert saved == 0
        assert len(messages) == 3  # 原消息不变

    def test_compresses_and_preserves_recent(self):
        """压缩后保留最近消息，旧消息被摘要替换。"""
        messages = [
            {"role": "user", "content": "old question 1"},
            {"role": "assistant", "content": "old answer 1"},
            {"role": "user", "content": "old question 2"},
            {"role": "assistant", "content": "old answer 2"},
            {"role": "user", "content": "recent question"},
            {"role": "assistant", "content": "recent answer"},
        ]
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [{"type": "text", "text": "之前的对话摘要"}]
        mock_client.chat.return_value = mock_response

        saved = compact_messages_with_summary(messages, mock_client, keep_recent_count=2)
        # saved 可能为负（摘要+样板比原始短消息更长），只验证结构
        assert isinstance(saved, int)
        # 压缩后: 摘要 user + 摘要 assistant + 2 条保留消息
        assert len(messages) == 4
        assert "摘要" in messages[0]["content"]
        assert messages[-2]["content"] == "recent question"
        assert messages[-1]["content"] == "recent answer"
