"""BaseAgent 纯逻辑单测（不依赖 DGX）：full compact 切分点等。"""

import pytest

from core.base_agent import BaseAgent


def _agent():
    """绕过 __init__ 创建实例，调用不依赖实例状态的纯方法。"""
    return object.__new__(BaseAgent)


def _user_text(text, pinned=False):
    return {"role": "user", "content": text, "_meta": {"pinned": pinned} if pinned else {}}


def _tool_result_msg(tool_use_id="t1"):
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "result"}],
        "_meta": {},
    }


def _assistant_tool_use(tool_use_id="t1"):
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tool_use_id, "name": "bash", "input": {"command": "ls"}}],
        "_meta": {},
    }


def _assistant_text(text):
    return {"role": "assistant", "content": text, "_meta": {}}


class TestFindSplitByUserMessages:
    """切分点查找：交互模式按 string user 消息，autonomous 模式回退到任意 user 消息。"""

    def test_interactive_keeps_last_n_text(self):
        """纯文本 user 消息多于 keep 时，按最近 N 条切分。"""
        messages = [
            _user_text("q1"), _assistant_text("a1"),
            _user_text("q2"), _assistant_text("a2"),
            _user_text("q3"), _assistant_text("a3"),
        ]
        split = _agent()._find_split_by_user_messages(messages, keep_user_count=2)
        assert split == 2  # 保留 q3/a3 + q2/a2

    def test_autonomous_falls_back_to_any_user(self):
        """无纯文本 user 消息时（worker/lite），退回按 tool_result 轮切分。"""
        messages = [
            _user_text("task", pinned=True),  # pinned 任务消息不参与计数
            _assistant_tool_use("t1"),
            _tool_result_msg("t1"),
            _assistant_tool_use("t2"),
            _tool_result_msg("t2"),
            _assistant_tool_use("t3"),
            _tool_result_msg("t3"),
            _assistant_tool_use("t4"),
            _tool_result_msg("t4"),
        ]
        split = _agent()._find_split_by_user_messages(messages, keep_user_count=2)
        # 保留最后 2 轮；切分点回退到第 3 轮的 assistant tool_use 起点
        assert split == 5
        kept = messages[split:]
        tool_results_kept = sum(
            1 for m in kept if isinstance(m.get("content"), list)
            and any(b.get("type") == "tool_result" for b in m["content"])
        )
        assert tool_results_kept == 2

    def test_not_enough_messages_returns_zero(self):
        messages = [_user_text("q1"), _assistant_text("a1")]
        assert _agent()._find_split_by_user_messages(messages, keep_user_count=3) == 0

    def test_does_not_split_mid_tool_chain(self):
        """切分点落在工具链中间时回退到轮起点，不撕开 tool_use/tool_result。"""
        messages = [
            _user_text("task", pinned=True),
            _assistant_tool_use("t1"),
            _tool_result_msg("t1"),
            _assistant_tool_use("t2"),
            _tool_result_msg("t2"),
            _assistant_tool_use("t3"),
            _tool_result_msg("t3"),
            _assistant_text("final answer"),
        ]
        split = _agent()._find_split_by_user_messages(messages, keep_user_count=1)
        assert split == 5  # 回退到 t3 轮起点，保留 t3 轮 + final
        # 轮完整性：split-1 不是被丢弃的 assistant tool_use，split 不是被丢弃的 user tool_result
        prev = messages[split - 1]
        msg = messages[split]
        prev_content = prev.get("content", [])
        content = msg.get("content", [])
        assert not (prev.get("role") == "assistant" and isinstance(prev_content, list)
                    and any(b.get("type") == "tool_use" for b in prev_content))
        assert not (msg.get("role") == "user" and isinstance(content, list)
                    and any(b.get("type") == "tool_result" for b in content))


class TestSafeOutputFilename:
    """tool_use_id 净化：非法 ID 回退随机文件名，防路径穿越。"""

    @staticmethod
    def _safe(tool_use_id):
        return _agent()._safe_output_filename(tool_use_id)

    def test_valid_id_preserved(self):
        assert self._safe("toolu_01ABC-def") == "toolu_01ABC-def.txt"

    def test_empty_returns_empty(self):
        assert self._safe("") == ""

    def test_path_traversal_falls_back(self):
        """含路径分隔符/`..` 的 ID 不能直接用作文件名。"""
        for bad in ["../../etc/passwd", "a/b.txt", "..", "x\\y", "a b"]:
            name = self._safe(bad)
            assert name != bad and name.endswith(".txt")
            assert "/" not in name and "\\" not in name and ".." not in name

    def test_fallback_has_consistent_format(self):
        import re as _re
        name = self._safe("../evil")
        assert _re.match(r"^[0-9a-f]{8}\.txt$", name)
