"""BaseSessionRunner 纯逻辑单测（不依赖 DGX）：full compact、消息管理。"""

import json
from unittest.mock import patch

from core.base_session_runner import BaseSessionRunner
from test.support import make_dgx_config


def _agent():
    """绕过 __init__ 创建实例，调用不依赖实例状态的纯方法。"""
    return object.__new__(BaseSessionRunner)


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


class TestBaseSessionRunnerUnitTests:
    """BaseSessionRunner 单元测试：纯逻辑，不调用 LLM。"""

    def test_add_message_with_valid_flag(self):
        """add_message 不再添加 block 级别的 _valid（新格式使用 message 级别的 _meta.valid）。"""
        from core.base_session_runner import BaseSessionRunner

        config = make_dgx_config("anthropic")
        runner = BaseSessionRunner(config=config)
        runner.add_message("user", [{"type": "text", "text": "hello"}])

        assert len(runner.messages) == 1
        assert runner.messages[0]["role"] == "user"
        # 新格式：不再自动添加 block 级别的 _valid
        # 消息级别的 _meta.valid 由其他逻辑设置

    def test_get_valid_messages_filters_invalid(self):
        """get_valid_messages 过滤无效消息（使用新格式 _meta.valid）。"""
        from core.base_session_runner import BaseSessionRunner

        config = make_dgx_config("anthropic")
        runner = BaseSessionRunner(config=config)
        runner.messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            # 使用新格式：消息级别的 _meta.valid = False
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "test"}], "_meta": {"valid": False}},
        ]

        valid = runner.get_valid_messages()
        assert len(valid) == 2  # user message + valid assistant message

    def test_count_tokens(self):
        """_count_messages_tokens 委托 compression.count_messages_tokens（含 reasoning 块）。"""
        from core.base_session_runner import BaseSessionRunner

        config = make_dgx_config("anthropic")
        runner = BaseSessionRunner(config=config)

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "hi"},
                {"type": "reasoning", "text": "思考内容"},  # reasoning 块也应计入
            ]},
        ]

        assert runner._count_messages_tokens(messages) > 0
        assert runner._count_messages_tokens([]) == 0

    def test_iter_content_blocks(self):
        """iter_content_blocks 产出正确类型。"""
        from core.base_session_runner import BaseSessionRunner

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "id": "1", "name": "bash", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "1", "content": "output"},
            ]},
        ]

        blocks = list(BaseSessionRunner.iter_content_blocks(messages))
        types = [b[0] for b in blocks]

        assert "text" in types
        assert "tool_use" in types
        assert "tool_result_str" in types

    def test_full_compact_failure_does_not_invalidate(self, tmp_path):
        """摘要生成失败时旧消息不应被标记无效（防止历史永久丢失）。"""
        from core.base_session_runner import BaseSessionRunner

        config = make_dgx_config("anthropic")
        runner = BaseSessionRunner(config=config, session_dir=tmp_path)
        runner.messages = [
            {"role": "user", "content": "question 1"},
            {"role": "assistant", "content": "answer 1"},
            {"role": "user", "content": "question 2"},
            {"role": "assistant", "content": "answer 2"},
            {"role": "user", "content": "recent question"},
        ]

        # Patch Runner._summarize_messages（inner runner），不是 BaseSessionRunner._summarize_messages
        with patch.object(runner.runner, "_summarize_messages", return_value="（摘要生成失败，请查看完整历史）"):
            before, after = runner._perform_full_compact(keep_user_messages=1, keep_tokens=0)

        # 摘要失败后所有消息仍有效
        assert all(m.get("_meta", {}).get("valid") is not False for m in runner.messages)
        assert len(runner.get_valid_messages()) == len(runner.messages)

    def test_full_compact_success_invalidates_old(self, tmp_path):
        """摘要成功后旧消息才被标记无效。"""
        from core.base_session_runner import BaseSessionRunner

        config = make_dgx_config("anthropic")
        runner = BaseSessionRunner(config=config, session_dir=tmp_path)
        runner.messages = [
            {"role": "user", "content": "question 1"},
            {"role": "assistant", "content": "answer 1"},
            {"role": "user", "content": "question 2"},
            {"role": "assistant", "content": "answer 2"},
            {"role": "user", "content": "recent question"},
        ]

        # Patch Runner._summarize_messages（inner runner）
        with patch.object(runner.runner, "_summarize_messages", return_value="之前的对话摘要内容"):
            runner._perform_full_compact(keep_user_messages=1, keep_tokens=0)

        # 摘要成功：旧消息被标记无效，摘要消息已插入
        valid = runner.get_valid_messages()
        assert len(valid) < len(runner.messages)
        assert any("摘要" in (m.get("content", "") if isinstance(m.get("content"), str) else "")
                   for m in runner.messages)

    def test_full_compact_preserves_pinned_messages(self, tmp_path):
        """full compact 不标记 pinned 消息失效，也不影响其后续消息的对齐。"""
        from core.base_session_runner import BaseSessionRunner

        config = make_dgx_config("anthropic")
        runner = BaseSessionRunner(config=config, session_dir=tmp_path)
        runner.messages = [
            {"role": "user", "content": "pinned task", "_meta": {"pinned": True}},
            {"role": "user", "content": "question 1"},
            {"role": "assistant", "content": "answer 1"},
            {"role": "user", "content": "question 2"},
            {"role": "assistant", "content": "answer 2"},
            {"role": "user", "content": "pinned check", "_meta": {"pinned": True}},
        ]

        # Patch Runner._summarize_messages（inner runner）
        with patch.object(runner.runner, "_summarize_messages", return_value="对话摘要"):
            runner._perform_full_compact(keep_user_messages=1, keep_tokens=0)

        # pinned 消息永不失效
        for m in runner.messages:
            if m.get("_meta", {}).get("pinned"):
                assert m["_meta"].get("valid") is not False
        # pinned 之后的普通消息仍可被正常压缩
        assert any(
            m.get("content") == "answer 1" and m.get("_meta", {}).get("valid") is False
            for m in runner.messages
        )

    def test_full_compact_aligns_across_error_notice(self, tmp_path):
        """error_notice 消息（仅 UI 展示、不进上下文）不得让压缩游标错位。

        回归：游标此前只跳过 _meta.valid is False，而 get_valid_messages 还跳过
        error_notice，导致其后消息整体漂移一位 —— pinned 锚点被误标失效、
        尾部应压缩的消息漏标。
        """
        from core.base_session_runner import BaseSessionRunner

        config = make_dgx_config("anthropic")
        runner = BaseSessionRunner(config=config, session_dir=tmp_path)
        runner.messages = [
            {"role": "assistant", "content": "错误: 网络超时", "_meta": {"error_notice": True}},
            {"role": "user", "content": "pinned task", "_meta": {"pinned": True}},
            {"role": "user", "content": "question 1"},
            {"role": "assistant", "content": "answer 1"},
            {"role": "user", "content": "question 2"},
            {"role": "assistant", "content": "answer 2"},
            {"role": "user", "content": "pinned check", "_meta": {"pinned": True}},
        ]

        # Patch Runner._summarize_messages（inner runner）
        with patch.object(runner.runner, "_summarize_messages", return_value="对话摘要"):
            runner._perform_full_compact(keep_user_messages=1, keep_tokens=0)

        # pinned 消息永不失效（漂移会让第一条 pinned 被误标）
        for m in runner.messages:
            if m.get("_meta", {}).get("pinned"):
                assert m["_meta"].get("valid") is not False
        # split 之前的普通消息仍被正常压缩（漂移会让它漏标）
        assert any(
            m.get("content") == "answer 1" and m.get("_meta", {}).get("valid") is False
            for m in runner.messages
        )

    def test_save_messages_preserves_metadata(self, tmp_path):
        """save_messages 不应覆盖 SessionStore 写入的 name/metadata。"""
        from core.base_session_runner import BaseSessionRunner

        config = make_dgx_config("anthropic")
        session_dir = tmp_path / "sess"
        runner = BaseSessionRunner(config=config, session_dir=session_dir)
        runner._session_id = "sess123"

        # 预写一个带 name/metadata 的会话文件（模拟 SessionStore.save()）
        session_dir.mkdir(parents=True, exist_ok=True)
        existing = {
            "session_id": "sess123",
            "name": "我的会话",
            "messages": [],
            "metadata": {
                "usage": {"input_tokens": 100, "output_tokens": 50, "api_calls": 2},
                "agent_count": 3,
            },
        }
        (session_dir / "index.json").write_text(
            json.dumps(existing, ensure_ascii=False), encoding="utf-8"
        )

        runner.messages = [{"role": "user", "content": "hi"}]
        runner.save_messages()

        data = json.loads((session_dir / "index.json").read_text(encoding="utf-8"))
        assert data["name"] == "我的会话"
        assert data["metadata"]["usage"]["input_tokens"] == 100
        assert data["metadata"]["agent_count"] == 3
        assert data["messages"] == runner.messages

    def test_mark_all_images_invalid_preserves_pairing(self):
        """图片失效改为占位符替换，tool_use/tool_result 配对不被破坏。"""
        from core.base_session_runner import BaseSessionRunner

        config = make_dgx_config("anthropic")
        runner = BaseSessionRunner(config=config)
        runner.messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "a"}},
            ]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "t1", "content": [
                    {"type": "text", "text": "screenshot"},
                    {"type": "image", "source": {"media_type": "image/png", "data": "A" * 100}},
                ],
            }]},
        ]

        runner._mark_all_images_invalid()

        # 消息仍有效（不被整条过滤），图片被替换为文本占位符
        assert len(runner.get_valid_messages()) == 2
        subs = runner.messages[1]["content"][0]["content"]
        assert [s["type"] for s in subs] == ["text", "text"]

    def test_mark_old_images_invalid_replaces_not_invalidates(self):
        """_mark_old_images_invalid 替换旧图片，保留消息有效性。"""
        from core.base_session_runner import BaseSessionRunner

        config = make_dgx_config("anthropic")
        runner = BaseSessionRunner(config=config)
        runner.messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
            ]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "t1", "content": [
                    {"type": "image", "source": {"data": "A" * 200}},
                ],
            }]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t2", "name": "read", "input": {}},
            ]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "t2", "content": [
                    {"type": "image", "source": {"data": "B" * 100}},
                ],
            }]},
        ]

        saved = runner._mark_old_images_invalid(max_body_size=10250)  # 替换最早一条后 body < 10250

        assert saved > 0  # 有节省
        assert len(runner.get_valid_messages()) == 4  # 全部消息仍有效
        first_subs = runner.messages[1]["content"][0]["content"]
        assert first_subs[0]["type"] == "text"  # 最早一条已替换为占位符
        last_subs = runner.messages[3]["content"][0]["content"]
        assert last_subs[0]["type"] == "image"  # 最近一条保留
