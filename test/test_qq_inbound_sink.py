"""QQ 入站侧：单流 transcript（QQSink）与编号回映射测试。"""

from __future__ import annotations

from interfaces.common.ask_user import resolve_answer


class _FakeApi:
    def next_msg_seq(self):
        return 7


class _SinkRuntime:
    def __init__(self):
        from interfaces.qq.config import QQConfig
        from interfaces.qq.limiter import ReplyLimiter
        from interfaces.qq.state import QQAccountState
        self.account_id = "acc1"
        self.cfg = QQConfig()
        self.limiter = ReplyLimiter()
        self.api = _FakeApi()
        self.loop = None
        self.state = QQAccountState()


class TestQQSinkTranscript:
    def _sink(self, **kw):
        from interfaces.qq.sink import QQSink
        rt = _SinkRuntime()
        sink = QQSink(rt, "openid", "msgid", **kw)
        # 用假 target 接管流式下发，避免依赖 SDK/loop
        pushed: list[tuple[str, bool]] = []

        class _T:
            sent_ok = 0
            failed = False

            def push(self, text, *, final):
                pushed.append((text, final))
                _T.sent_ok += 1

            def restart(self):
                pushed.append(("<<restart>>", False))

            def close(self):
                pass

        sink._target = _T()
        from interfaces.qq.stream_controller import StreamingController
        sink._controller = StreamingController(
            sink._target, flush_interval=0.0, max_wait=0.0
        )
        return sink, pushed

    def test_tool_line_format_and_dedup(self):
        sink, pushed = self._sink()
        sink.on_tool_call("read", {"file_path": "a.py"}, "t1")
        sink.on_tool_call("read", {"file_path": "b.py"}, "t2")   # 连续同名 → 去重
        sink.on_tool_call("grep", {"pattern": "x"}, "t3")
        text = pushed[-1][0]
        assert text.count("🔧 正在调用 read 工具…") == 1
        assert "🔧 正在调用 grep 工具…" in text
        # 不含入参
        assert "a.py" not in text and "x" not in text

    def test_tool_line_repeats_after_text(self):
        sink, pushed = self._sink()
        sink.on_tool_call("read", {}, "t1")
        sink.on_text("分析结果如下。")
        sink.on_tool_call("read", {}, "t2")   # 中间夹了正文 → 重新记一行
        assert pushed[-1][0].count("🔧 正在调用 read 工具…") == 2

    def test_ask_user_rendered_into_stream(self):
        sink, pushed = self._sink()
        sink.on_tool_call("ask_user", {"questions": [{
            "question": "选哪个?", "header": "选择",
            "options": [{"label": "A", "description": "方案甲"},
                        {"label": "B", "description": "方案乙"}],
        }]}, "t1")
        text = pushed[-1][0]
        assert "选哪个?" in text
        assert "1. A — 方案甲" in text
        assert "2. B — 方案乙" in text
        assert "回复序号" in text

    def test_retry_clear_sentinel_resets_stream(self):
        from interfaces.qq.sink import RETRY_CLEAR_SENTINEL
        sink, pushed = self._sink()
        sink.on_text("旧内容")
        sink.on_text(RETRY_CLEAR_SENTINEL)
        assert "<<restart>>" in [p[0] for p in pushed]
        sink.on_text("新内容")
        assert pushed[-1][0] == "新内容"

    def test_buffer_is_monotonic_prefix(self):
        """replace 模式要求新内容以已下发前缀开头（防 40007）。"""
        sink, pushed = self._sink()
        sink.on_text("第一段")
        sink.on_text("，第二段")
        sink.on_tool_call("read", {}, "t1")
        texts = [t for t, _ in pushed if t != "<<restart>>"]
        for prev, cur in zip(texts, texts[1:]):
            assert cur.startswith(prev), f"{cur!r} 不以 {prev!r} 开头"

    def test_thinking_not_pushed_to_qq(self):
        sink, pushed = self._sink()
        base_calls = []
        sink._base = type("B", (), {
            "on_thinking": staticmethod(lambda t: base_calls.append(t)),
        })()
        sink.on_thinking("内部推理")
        assert pushed == []                 # 不推给 QQ
        assert base_calls == ["内部推理"]     # 但走全局流

    def test_tool_result_not_pushed_to_qq(self):
        sink, pushed = self._sink()
        sink._base = type("B", (), {"on_tool_result": staticmethod(lambda *a: None)})()
        sink.on_tool_result("read", "文件内容", False, "t1")
        assert pushed == []

    def test_turn_complete_closes_stream(self):
        sink, pushed = self._sink()
        sink.on_text("答案")
        sink.on_turn_complete()
        assert pushed[-1][1] is True        # final 标记

    def test_pure_scaffolding_text_not_pushed(self):
        """整段都是 thinking 标签 → 不推给 QQ。"""
        sink, pushed = self._sink()
        sink.on_text("<thinking>只有推理</thinking>")
        assert pushed == []

    def test_build_returns_output_sink(self):
        from core.output_sink import OutputSink
        sink, _ = self._sink()
        assert isinstance(sink.build(), OutputSink)

    def test_fanout_to_base_on_text(self):
        """QQ 驱动的回合必须仍发布到全局事件流（Web 才看得见）。"""
        seen = []
        from core.output_sink import OutputSink
        sink, _ = self._sink(base=OutputSink(on_text=seen.append))
        sink.on_text("你好")
        assert seen == ["你好"]

    def test_multi_question_render_asks_for_qualified_answer(self):
        """多问题时编号在每个问题内重开，单个数字有歧义 → 提示「问题序号: 选项序号」。"""
        sink, pushed = self._sink()
        sink.on_tool_call("ask_user", {"questions": [
            {"question": "甲?", "header": "A", "options": [{"label": "A1", "description": ""}]},
            {"question": "乙?", "header": "B", "options": [{"label": "B1", "description": ""}]},
        ]}, "t1")
        text = pushed[-1][0]
        assert "1: 2" in text
        assert "回复序号即可" not in text


class TestResolveAnswer:
    """ask_user 的编号 → label 映射。"""

    class _Session:
        def __init__(self, questions):
            self.messages = [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu1", "name": "ask_user",
                     "input": {"questions": questions}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu1",
                     "content": "Waiting for user input...",
                     "_meta": {"completed": False, "tool_name": "ask_user"}},
                ]},
            ]

    class _Runner:
        def __init__(self, questions):
            self.session = TestResolveAnswer._Session(questions)

    def _resolve(self, questions, content):
        return resolve_answer(self._Runner(questions), content)

    def test_single_question_maps_number_to_label(self):
        qs = [{"question": "选?", "options": [{"label": "允许", "description": ""},
                                             {"label": "拒绝", "description": ""}]}]
        assert self._resolve(qs, "1") == "允许"
        assert self._resolve(qs, "2") == "拒绝"

    def test_out_of_range_number_passes_through(self):
        qs = [{"question": "选?", "options": [{"label": "A", "description": ""}]}]
        assert self._resolve(qs, "9") == "9"

    def test_multi_question_does_not_guess(self):
        """两个问题时 "1" 是歧义的 → 原样透传，不猜。"""
        qs = [
            {"question": "甲?", "options": [{"label": "甲1", "description": ""}]},
            {"question": "乙?", "options": [{"label": "乙1", "description": ""}]},
        ]
        assert self._resolve(qs, "1") == "1"

    def test_non_numeric_passes_through(self):
        qs = [{"question": "选?", "options": [{"label": "A", "description": ""}]}]
        assert self._resolve(qs, "我自己输入") == "我自己输入"

    def test_no_pending_ask_user_passes_through(self):
        class _Empty:
            class session:  # noqa: N801
                messages = []
        assert resolve_answer(_Empty(), "1") == "1"


class TestResolveAnswerAbortOption:
    """审批卡第 4 项「终止任务」：编号 4 经 IM 映射后进入 abort 决策。

    IM 侧不特判终止，靠「编号 → 选项 label」通用映射；这里锁住语义，
    防止以后调整选项顺序却没同步编号。
    """

    @staticmethod
    def _runner(options):
        from types import SimpleNamespace

        messages = [
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": "tu1", "name": "ask_user",
                "input": {"questions": [{
                    "question": "检测到需要确认的操作", "header": "权限确认",
                    "options": options,
                }]},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "tu1",
                "content": "等待用户确认", "is_error": True,
                "_meta": {"completed": False, "tool_name": "ask_user", "kind": "approval"},
            }]},
        ]
        return SimpleNamespace(session=SimpleNamespace(messages=messages))

    def test_fourth_option_maps_to_abort(self):
        from core.approval_cards import decision_from_text
        from core.tools.permission import (
            ABORT_LABEL,
            APPROVE_LABEL,
            REJECT_LABEL,
            REMEMBER_LABEL,
        )

        options = [{"label": label, "description": ""} for label in
                   (APPROVE_LABEL, REMEMBER_LABEL, REJECT_LABEL, ABORT_LABEL)]
        reply = resolve_answer(self._runner(options), "4")
        assert reply == ABORT_LABEL
        assert decision_from_text(reply) == "abort"
