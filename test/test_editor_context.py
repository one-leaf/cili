"""Tests for 编辑器上下文：格式化、_build_user_input、排队合并。"""

from core.editor_context import MAX_SELECTION_CHARS, format_editor_context
from core.turn_queue import QueuedTurn, merge_queued_turns


class TestFormatEditorContext:
    def test_open_file_only(self):
        block = format_editor_context({"path": "src/a.py"})
        assert block["type"] == "text"
        assert "src/a.py" in block["text"]
        assert "selected lines" not in block["text"]
        assert block["_meta"]["editor_context"] is True

    def test_selection(self):
        block = format_editor_context({
            "path": "src/a.py",
            "selection": {"startLine": 12, "endLine": 20, "text": "def f():\n    pass"},
        })
        assert "lines 12-20" in block["text"]
        assert "def f():" in block["text"]

    def test_selection_truncated(self):
        long_text = "x" * (MAX_SELECTION_CHARS + 100)
        block = format_editor_context({
            "path": "a.txt",
            "selection": {"startLine": 1, "endLine": 2, "text": long_text},
        })
        assert "(selection truncated)" in block["text"]

    def test_invalid(self):
        assert format_editor_context(None) is None
        assert format_editor_context({}) is None
        assert format_editor_context({"path": "  "}) is None


class TestBuildUserInput:
    def test_no_context_no_images_returns_str(self):
        from web.routes_chat import _build_user_input
        assert _build_user_input("hi", None) == "hi"

    def test_context_only(self):
        from web.routes_chat import _build_user_input
        out = _build_user_input("hi", None, editor_context={"path": "a.py"})
        assert isinstance(out, list)
        assert out[0]["_meta"]["editor_context"] is True
        assert out[1]["type"] == "text" and out[1]["text"] == "hi"

    def test_context_with_images(self):
        from web.routes_chat import _build_user_input
        out = _build_user_input(
            "hi",
            [{"media_type": "image/png", "data": "abc"}],
            editor_context={"path": "a.py"},
        )
        assert out[0]["_meta"]["editor_context"] is True
        assert any(b.get("type") == "image" for b in out)


class TestMergeQueuedTurns:
    @staticmethod
    def _item(content, **kw):
        return QueuedTurn(item_id="q", content=content, user_input=content, **kw)

    def test_text_only_without_context(self):
        ui, _ = merge_queued_turns([self._item("a"), self._item("b")])
        assert ui == "a\n\nb"

    def test_text_with_context(self):
        ui, _ = merge_queued_turns([
            self._item("a", editor_context={"path": "a.py"}),
            self._item("b"),
        ])
        assert isinstance(ui, list)
        ctx = [b for b in ui if b.get("_meta", {}).get("editor_context")]
        assert len(ctx) == 1
        assert "a.py" in ctx[0]["text"]
        assert any(b.get("type") == "text" and b.get("text") == "a" for b in ui)
        assert any(b.get("type") == "text" and b.get("text") == "b" for b in ui)

    def test_images_with_context(self):
        img = {"type": "image", "source": {"type": "base64", "data": "abc"}}
        ui, _ = merge_queued_turns([
            QueuedTurn(item_id="q", content="a", user_input=[img], has_images=True,
                       editor_context={"path": "a.py"}),
        ])
        assert isinstance(ui, list)
        assert any(b.get("type") == "image" for b in ui)
        assert sum(1 for b in ui if b.get("_meta", {}).get("editor_context")) == 1


class TestFileMentions:
    @staticmethod
    def _workspace(tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.py").write_text(
            "line1\nline2\nline3\nline4\nline5\n",
            encoding="utf-8",
        )
        return ws

    def test_expand_ranged_mention(self, tmp_path):
        from core.editor_context import expand_file_mentions
        ws = self._workspace(tmp_path)
        blocks = expand_file_mentions("see @a.py#L2-3", ws)
        assert len(blocks) == 1
        assert "lines 2-3" in blocks[0]["text"]
        assert "line2" in blocks[0]["text"] and "line3" in blocks[0]["text"]
        assert blocks[0]["_meta"]["file_mention"] is True

    def test_mention_without_range_ignored(self, tmp_path):
        from core.editor_context import expand_file_mentions
        ws = self._workspace(tmp_path)
        assert expand_file_mentions("see @a.py", ws) == []

    def test_outside_workspace_ignored(self, tmp_path):
        from core.editor_context import expand_file_mentions
        ws = self._workspace(tmp_path)
        (tmp_path / "secret.txt").write_text("x\n", encoding="utf-8")
        assert expand_file_mentions("see @../secret.txt#L1-1", ws) == []

    def test_build_user_input_with_mention(self, tmp_path):
        from web.routes_chat import _build_user_input
        ws = self._workspace(tmp_path)
        out = _build_user_input(
            "see @a.py#L2-3", None,
            uploads_dir=ws / "uploads",
        )
        assert isinstance(out, list)
        assert any(b.get("_meta", {}).get("file_mention") for b in out)


class TestMergeQueuedTurnsMentions:
    def test_mention_blocks_preserved(self):
        mention = {"type": "text", "text": "x", "_meta": {"file_mention": True}}
        item = QueuedTurn(
            item_id="q",
            content="see @a.py#L2-3",
            user_input=[mention, {"type": "text", "text": "see @a.py#L2-3"}],
        )
        ui, _ = merge_queued_turns([item])
        assert isinstance(ui, list)
        assert any(b.get("_meta", {}).get("file_mention") for b in ui)


class TestAttachments:
    @staticmethod
    def _workspace(tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "uploads").mkdir()
        return ws

    def test_image_attachment_hint(self, tmp_path):
        from web.routes_chat import _build_user_input
        ws = self._workspace(tmp_path)
        (ws / "uploads" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
        out = _build_user_input(
            "hi", None,
            uploads_dir=ws / "uploads",
            attachments=[{"path": "uploads/a.png", "media_type": "image/png"}],
        )
        assert isinstance(out, list)
        assert any(
            b.get("type") == "text" and 'read_image("uploads/a.png")' in b.get("text", "")
            for b in out
        )
        assert not any(b.get("type") == "image" for b in out)

    def test_text_attachment_hint(self, tmp_path):
        from web.routes_chat import _build_user_input
        ws = self._workspace(tmp_path)
        (ws / "uploads" / "a.txt").write_text("hello\n", encoding="utf-8")
        out = _build_user_input(
            "hi", None,
            uploads_dir=ws / "uploads",
            attachments=[{"path": "uploads/a.txt", "media_type": "text/plain"}],
        )
        assert any(
            b.get("type") == "text" and (b.get("_meta") or {}).get("attachment")
            for b in out
        )

    def test_attachment_outside_workspace_skipped(self, tmp_path):
        from web.routes_chat import _build_user_input
        ws = self._workspace(tmp_path)
        (tmp_path / "secret.txt").write_text("x\n", encoding="utf-8")
        out = _build_user_input(
            "hi", None,
            uploads_dir=ws / "uploads",
            attachments=[{"path": "../secret.txt", "media_type": "text/plain"}],
        )
        assert out == "hi"

    def test_merge_preserves_attachment_blocks(self):
        hint = {"type": "text", "text": "x", "_meta": {"attachment": True}}
        img = {"type": "image", "source": {"type": "base64", "data": "abc"},
               "_meta": {"attachment": True}}
        item = QueuedTurn(
            item_id="q",
            content="hi",
            user_input=[hint, img, {"type": "text", "text": "hi"}],
        )
        ui, _ = merge_queued_turns([item])
        assert isinstance(ui, list)
        assert any(
            b.get("type") == "text" and (b.get("_meta") or {}).get("attachment")
            for b in ui
        )
        assert any(b.get("type") == "image" for b in ui)
