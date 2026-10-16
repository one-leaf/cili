"""QQ 出站管线的纯函数测试：消毒 / 切分 / 配额 / 流式状态机。

这些都不依赖网络与 QQ 凭据，可在任何环境跑——也是出站逻辑最容易出错的部分
（前缀不可变、SDK 二次截断、围栏闭合）。
"""

from __future__ import annotations

# ─── 文本消毒 ─────────────────────────────────────────────────────────


class TestSanitize:
    def test_strips_system_reminder(self):
        from interfaces.qq.sanitize import sanitize_qq_text
        s = "hello\n<system-reminder>internal stuff</system-reminder>\nworld"
        out = sanitize_qq_text(s)
        assert "internal stuff" not in out
        assert "hello" in out and "world" in out

    def test_strips_thinking_variants(self):
        from interfaces.qq.sanitize import sanitize_qq_text
        assert "secret" not in sanitize_qq_text("a<thinking>secret</thinking>b")
        assert "secret" not in sanitize_qq_text("a<think>secret</think>b")
        assert "secret" not in sanitize_qq_text("a`think`secret`/think`b")

    def test_strips_previous_response(self):
        from interfaces.qq.sanitize import sanitize_qq_text
        assert "old" not in sanitize_qq_text("<previous_response>old</previous_response>新")

    def test_preserves_markdown(self):
        from interfaces.qq.sanitize import sanitize_qq_text
        md = "# 标题\n\n- **粗体**\n\n```py\nx=1\n```"
        assert sanitize_qq_text(md) == md

    def test_empty_input(self):
        from interfaces.qq.sanitize import sanitize_qq_text
        assert sanitize_qq_text("") == ""


# ─── markdown 感知切分 ────────────────────────────────────────────────


class TestChunk:
    def test_short_text_single_chunk(self):
        from interfaces.qq.chunk import chunk_markdown
        assert chunk_markdown("短文本") == ["短文本"]

    def test_empty_text(self):
        from interfaces.qq.chunk import chunk_markdown
        assert chunk_markdown("") == []

    def test_default_limit_matches_sdk(self):
        """上限必须等于 SDK 的 MAX_MESSAGE_LENGTH(4000)，否则会被二次截断丢内容。"""
        from interfaces.qq.chunk import MAX_CHUNK_CHARS
        assert MAX_CHUNK_CHARS == 4000

    def test_respects_limit(self):
        from interfaces.qq.chunk import chunk_markdown
        text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
        chunks = chunk_markdown(text, limit=400)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 400, f"chunk 超限: {len(c)}"

    def test_fitting_table_stays_whole(self):
        """能放下的表格必须整块落在同一片里，不能被正文挤断。"""
        from interfaces.qq.chunk import chunk_markdown
        table = "\n".join(
            ["| a | b |", "|---|---|"] + [f"| {i} | {i} |" for i in range(3)]
        )
        text = ("前" * 100) + "\n" + table + "\n" + ("后" * 100)
        chunks = chunk_markdown(text, limit=200)
        assert any(table in c for c in chunks), chunks

    def test_oversized_table_splits_at_row_boundaries(self):
        """超限表格切不开整块，但必须按**行**切——不能把某行劈成两半。"""
        from interfaces.qq.chunk import chunk_markdown
        rows = [f"| row{i} | value{i} |" for i in range(40)]
        table = "\n".join(["| a | b |", "|---|---|"] + rows)
        chunks = chunk_markdown(table, limit=200)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 200
            for line in c.split("\n"):
                # 每行要么是完整的表格行，要么是空行——不能出现被劈断的半行
                assert line.strip() == "" or line.strip().startswith("|"), repr(line)
                assert line.strip() == "" or line.strip().endswith("|"), repr(line)

    def test_code_fence_closed_in_every_non_final_chunk(self):
        from interfaces.qq.chunk import chunk_markdown
        body = "\n".join(f"line{i} = {i}" for i in range(80))
        text = "```python\n" + body + "\n```\n尾注"
        chunks = chunk_markdown(text, limit=200)
        assert len(chunks) > 1
        for c in chunks[:-1]:
            fences = sum(1 for ln in c.split("\n") if ln.strip().startswith("```"))
            assert fences % 2 == 0, f"围栏未闭合: {c[:60]!r}"

    def test_reassembly_preserves_all_lines(self):
        from interfaces.qq.chunk import chunk_markdown
        text = "\n".join(f"行 {i} 内容" for i in range(150))
        joined = "\n".join(chunk_markdown(text, limit=300))
        for i in range(150):
            assert f"行 {i} 内容" in joined

    def test_single_overlong_line_hard_split(self):
        from interfaces.qq.chunk import chunk_markdown
        text = "x" * 1000
        chunks = chunk_markdown(text, limit=100)
        assert all(len(c) <= 100 for c in chunks)
        assert "".join(chunks) == text

    def test_no_content_lost(self):
        """回归：不能因为切分丢字符（SDK 二次截断曾会丢）。"""
        from interfaces.qq.chunk import chunk_markdown
        text = "\n".join(f"第{i}行" for i in range(300))
        chunks = chunk_markdown(text, limit=137)
        stripped = "".join(c.replace("\n", "") for c in chunks)
        assert stripped == text.replace("\n", "")


# ─── 被动回复配额 ─────────────────────────────────────────────────────


class TestReplyLimiter:
    def test_allows_four_then_falls_back(self):
        from interfaces.qq.limiter import ReplyLimiter
        lim = ReplyLimiter()
        for i in range(4):
            check = lim.check("m1")
            assert check.allowed, f"第 {i + 1} 次应放行"
            assert check.remaining == 4 - i
            lim.record("m1")
        blocked = lim.check("m1")
        assert not blocked.allowed
        assert blocked.fallback_reason == "limit_exceeded"
        assert blocked.should_fallback_to_proactive

    def test_unknown_msg_id_allowed(self):
        from interfaces.qq.limiter import ReplyLimiter
        assert ReplyLimiter().check("never-seen").allowed

    def test_expiry_frees_quota(self):
        from interfaces.qq.limiter import ReplyLimiter
        lim = ReplyLimiter(ttl_seconds=0.0)
        lim.record("m1")
        check = lim.check("m1")
        assert not check.allowed
        assert check.fallback_reason == "expired"

    def test_no_msg_id_is_not_passive(self):
        from interfaces.qq.limiter import ReplyLimiter
        check = ReplyLimiter().check("")
        assert not check.allowed
        assert check.fallback_reason == "no_msg_id"

    def test_lru_eviction(self):
        from interfaces.qq.limiter import ReplyLimiter
        lim = ReplyLimiter(max_tracked=2)
        lim.record("a")
        lim.record("b")
        lim.record("c")
        assert lim.stats()["tracked_messages"] == 2

    def test_isolated_by_msg_id(self):
        from interfaces.qq.limiter import ReplyLimiter
        lim = ReplyLimiter()
        for _ in range(4):
            lim.record("m1")
        assert not lim.check("m1").allowed
        assert lim.check("m2").allowed


# ─── 流式状态机 ───────────────────────────────────────────────────────


class _FakeTarget:
    """记录 push/restart 调用。"""

    def __init__(self):
        self.pushes: list[tuple[str, bool]] = []
        self.restarts = 0

    def push(self, full_text: str, *, final: bool) -> None:
        self.pushes.append((full_text, final))

    def restart(self) -> None:
        self.restarts += 1

    @property
    def last(self) -> str:
        return self.pushes[-1][0] if self.pushes else ""


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TestStreamingController:
    def _make(self, **kw):
        from interfaces.qq.stream import StreamingController
        target = _FakeTarget()
        clock = _FakeClock()
        return StreamingController(target, clock=clock, **kw), target, clock

    def test_first_push_immediate(self):
        ctl, target, _ = self._make(flush_interval=1.5)
        ctl.push("a")
        assert len(target.pushes) == 1

    def test_throttled_within_window(self):
        ctl, target, clock = self._make(flush_interval=1.5, max_wait=8.0)
        ctl.push("a")
        ctl.push("ab")
        assert len(target.pushes) == 1, "窗口内不应重复下发"
        clock.advance(1.5)
        ctl.push("abc")
        assert len(target.pushes) == 2
        assert target.last == "abc"

    def test_max_wait_forces_flush(self):
        """持续输出时不能一直不发：距首次待发超过 max_wait 必须强制下发。"""
        ctl, target, clock = self._make(flush_interval=100.0, max_wait=5.0)
        ctl.push("a")                 # 首片立即下发
        assert len(target.pushes) == 1

        clock.advance(1.0)
        ctl.push("ab")                # 进入待发；窗口 100s 远未到 → 不发
        assert len(target.pushes) == 1

        clock.advance(5.0)            # 距首次待发已 5s → max_wait 触发
        ctl.push("abc")
        assert len(target.pushes) == 2
        assert target.last == "abc"

    def test_finish_always_pushes_with_final_flag(self):
        ctl, target, _ = self._make()
        ctl.push("hello")
        ctl.finish()
        assert target.pushes[-1] == ("hello", True)

    def test_no_duplicate_push_when_unchanged(self):
        ctl, target, clock = self._make(flush_interval=0.0)
        ctl.push("same")
        n = len(target.pushes)
        clock.advance(1.0)
        ctl.push("same")
        assert len(target.pushes) == n

    def test_shorter_text_restarts_as_new_reply(self):
        """前缀发散且更短 → 收尾当前流、另开一条。"""
        ctl, target, clock = self._make(flush_interval=0.0)
        ctl.push("第一段回答")
        assert target.restarts == 0
        clock.advance(1.0)
        ctl.push("新的回答")
        assert target.restarts == 1
        assert target.last == "新的回答"

    def test_longer_divergent_text_appends_tail_only(self):
        """更长但前缀发散 → 只追加尾巴，不回退已下发内容。"""
        ctl, target, clock = self._make(flush_interval=0.0)
        ctl.push("abcdef")
        clock.advance(1.0)
        ctl.push("abcXYZdefg")
        assert target.restarts == 0
        assert target.last == "abcdef" + "XYZdefg"

    def test_whitespace_normalized_prefix_match(self):
        ctl, target, clock = self._make(flush_interval=0.0)
        ctl.push("hello world")
        clock.advance(1.0)
        ctl.push("hello   world extended")
        assert target.restarts == 0
        assert target.last == "hello   world extended"

    def test_reset_starts_over(self):
        """RETRY_CLEAR_SENTINEL 入口：QQ 侧无法清空，只能收尾另开。"""
        ctl, target, clock = self._make(flush_interval=0.0)
        ctl.push("旧内容")
        ctl.reset()
        assert target.restarts == 1
        assert ctl.accepted_text == ""
        clock.advance(1.0)
        ctl.push("新内容")
        assert target.last == "新内容"

    def test_chunks_sent_counter(self):
        ctl, target, clock = self._make(flush_interval=0.0)
        assert not ctl.has_sent_chunks
        ctl.push("a")
        assert ctl.chunks_sent == 1
        clock.advance(1.0)
        ctl.push("ab")
        assert ctl.chunks_sent == 2

    def test_finish_is_idempotent(self):
        ctl, target, _ = self._make()
        ctl.push("x")
        ctl.finish()
        n = len(target.pushes)
        ctl.finish()
        assert len(target.pushes) == n


# ─── 出站媒体：类型映射与路径校验 ─────────────────────────────────────


class TestMedia:
    def test_infer_file_type_conservative(self):
        """保守映射：只有 png/jpg 保证内嵌成图片（文档两处矛盾，见模块 docstring）。"""
        from interfaces.qq import media
        assert media.infer_file_type("a.png") == media.MEDIA_TYPE_IMAGE
        assert media.infer_file_type("a.JPG") == media.MEDIA_TYPE_IMAGE
        assert media.infer_file_type("a.mp4") == media.MEDIA_TYPE_VIDEO
        assert media.infer_file_type("a.silk") == media.MEDIA_TYPE_VOICE
        # gif/webp/bmp 暂归文件（实测后再放宽）
        assert media.infer_file_type("a.gif") == media.MEDIA_TYPE_FILE
        assert media.infer_file_type("a.webp") == media.MEDIA_TYPE_FILE
        assert media.infer_file_type("a.pdf") == media.MEDIA_TYPE_FILE
        assert media.infer_file_type("noext") == media.MEDIA_TYPE_FILE

    def test_validate_rejects_outside_workspace(self, tmp_path):
        from interfaces.qq.media import MediaError, validate_media_path
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        import pytest
        with pytest.raises(MediaError):
            validate_media_path(outside, ws)

    def test_validate_accepts_inside_workspace(self, tmp_path):
        from interfaces.qq.media import validate_media_path
        ws = tmp_path / "ws"
        ws.mkdir()
        f = ws / "ok.txt"
        f.write_text("x", encoding="utf-8")
        assert validate_media_path(f, ws) == f.resolve()

    def test_validate_rejects_missing_file(self, tmp_path):
        import pytest

        from interfaces.qq.media import MediaError, validate_media_path
        ws = tmp_path / "ws"
        ws.mkdir()
        with pytest.raises(MediaError):
            validate_media_path(ws / "nope.txt", ws)

    def test_friendly_upload_errors(self):
        from interfaces.qq.media import _friendly_upload_error
        assert "容量" in _friendly_upload_error(RuntimeError("biz 40093002"))
        assert "大小限制" in _friendly_upload_error(RuntimeError("code 850031"))
        assert "格式" in _friendly_upload_error(RuntimeError("code 850019"))

    def test_upload_file_oversize_raises(self, tmp_path, monkeypatch):
        import asyncio

        from interfaces.qq import media
        monkeypatch.setattr(media, "HARD_LIMIT_BYTES", 4)
        f = tmp_path / "big.bin"
        f.write_bytes(b"12345678")

        class _U:
            async def upload(self, **_kw):
                raise AssertionError("不应到达上传")

        import pytest
        with pytest.raises(media.MediaError):
            asyncio.run(media.upload_file(_U(), "o1", f))

    def test_upload_file_passes_mapped_type(self, tmp_path):
        import asyncio

        from interfaces.qq import media
        f = tmp_path / "pic.png"
        f.write_bytes(b"x" * 10)
        seen = {}

        class _U:
            async def upload(self, **kw):
                seen.update(kw)
                return "FILE_INFO"

        result = asyncio.run(media.upload_file(_U(), "o1", f))
        assert result == "FILE_INFO"
        assert seen["chat_type"] == "c2c"
        assert seen["chat_id"] == "o1"
        assert seen["file_type"] == media.MEDIA_TYPE_IMAGE
        assert seen["file_name"] == "pic.png"
