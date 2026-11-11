"""Tests for 编辑器上下文 / 附件：格式化、_build_user_input、排队合并。"""

from core.editor_context import (
    MAX_SELECTION_CHARS,
    expand_file_mentions,
    format_attachment,
)
from core.turn_queue import QueuedTurn, merge_queued_turns


class TestFormatAttachment:
    def test_file_only(self):
        block = format_attachment({"path": "src/a.py"})
        assert block["type"] == "text"
        assert "src/a.py" in block["text"]
        assert "selected lines" not in block["text"]
        assert block["_meta"]["attachment"] is True

    def test_selection(self):
        block = format_attachment({
            "path": "src/a.py",
            "selection": {"startLine": 12, "endLine": 20, "text": "def f():\n    pass"},
        })
        assert "lines 12-20" in block["text"]
        assert "def f():" in block["text"]

    def test_selection_truncated(self):
        long_text = "x" * (MAX_SELECTION_CHARS + 100)
        block = format_attachment({
            "path": "a.txt",
            "selection": {"startLine": 1, "endLine": 2, "text": long_text},
        })
        assert "(selection truncated)" in block["text"]

    def test_image_hints_read_image(self):
        block = format_attachment({"path": "uploads/a.png", "media_type": "image/png"})
        assert 'read_image("uploads/a.png")' in block["text"]

    def test_invalid(self):
        assert format_attachment(None) is None
        assert format_attachment({}) is None
        assert format_attachment({"path": "  "}) is None


class TestBuildUserInput:
    def test_no_attachments_no_images(self):
        from web.routes_chat import _build_user_input
        assert _build_user_input("hi", None) == "hi"

    def test_attachment_only(self):
        from web.routes_chat import _build_user_input
        out = _build_user_input(
            "hi", None,
            attachments=[{"path": "a.py", "selection": {"startLine": 1, "endLine": 1, "text": "x"}}],
        )
        assert isinstance(out, list)
        assert out[0]["_meta"]["attachment"] is True
        assert out[-1] == {"type": "text", "text": "hi", "_valid": True}

    def test_multiple_attachments(self):
        from web.routes_chat import _build_user_input
        out = _build_user_input(
            "hi", None,
            attachments=[
                {"path": "src/a.py", "selection": {"startLine": 2, "endLine": 3, "text": "x"}},
                {"path": "uploads/a.png", "media_type": "image/png"},
            ],
        )
        assert isinstance(out, list)
        assert len([b for b in out if b.get("_meta", {}).get("attachment")]) == 2
        assert out[-1]["text"] == "hi"

    def test_attachments_with_images(self):
        from web.routes_chat import _build_user_input
        out = _build_user_input(
            "hi",
            [{"media_type": "image/png", "data": "abc"}],
            attachments=[{"path": "a.py"}],
        )
        assert any(b.get("_meta", {}).get("attachment") for b in out)
        assert any(b.get("type") == "image" for b in out)
        assert out[-1]["text"] == "hi"


class TestMergeQueuedTurns:
    @staticmethod
    def _item(content, **kw):
        return QueuedTurn(item_id="q", content=content, user_input=content, **kw)

    def test_text_only(self):
        ui, _ = merge_queued_turns([self._item("a"), self._item("b")])
        assert ui == "a\n\nb"

    def test_attachment_blocks_preserved(self):
        att = {"type": "text", "text": "x", "_meta": {"attachment": True}}
        item = QueuedTurn(
            item_id="q",
            content="hi",
            user_input=[att, {"type": "text", "text": "hi"}],
        )
        ui, _ = merge_queued_turns([item])
        assert isinstance(ui, list)
        assert any(b.get("_meta", {}).get("attachment") for b in ui)
        assert ui[-1].get("text") == "hi"

    def test_images_preserved(self):
        img = {"type": "image", "source": {"type": "base64", "data": "abc"}}
        item = QueuedTurn(item_id="q", content="a", user_input=[img], has_images=True)
        ui, _ = merge_queued_turns([item])
        assert isinstance(ui, list)
        assert any(b.get("type") == "image" for b in ui)


class TestFileMentions:
    @staticmethod
    def _workspace(tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.py").write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
        return ws

    def test_expand_ranged_mention(self, tmp_path):
        ws = self._workspace(tmp_path)
        blocks = expand_file_mentions("see @a.py#L2-3", ws)
        assert len(blocks) == 1
        assert "lines 2-3" in blocks[0]["text"]
        assert "line2" in blocks[0]["text"]
        assert blocks[0]["_meta"]["file_mention"] is True

    def test_mention_without_range_ignored(self, tmp_path):
        ws = self._workspace(tmp_path)
        assert expand_file_mentions("see @a.py", ws) == []

    def test_outside_workspace_ignored(self, tmp_path):
        ws = self._workspace(tmp_path)
        (tmp_path / "secret.txt").write_text("x\n", encoding="utf-8")
        assert expand_file_mentions("see @../secret.txt#L1-1", ws) == []

    def test_build_user_input_with_mention(self, tmp_path):
        from web.routes_chat import _build_user_input
        ws = self._workspace(tmp_path)
        out = _build_user_input("see @a.py#L2-3", None, uploads_dir=ws / "uploads")
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
