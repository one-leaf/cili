"""邮件正文解析与附件落盘（回合链路的纯函数据部分）。"""

import email
from email.message import EmailMessage

from interfaces.email.media import extract_text, save_attachments, truncate_body


def _msg(*, plain=None, html=None):
    msg = EmailMessage()
    msg["Subject"] = "test"
    if plain is not None:
        msg.set_content(plain)
    if html is not None:
        if plain is None:
            msg.set_content(html, subtype="html")
        else:
            msg.add_alternative(html, subtype="html")
    return email.message_from_bytes(msg.as_bytes())


class TestExtractText:
    def test_plain_body(self):
        assert extract_text(_msg(plain="你好\n世界")) == "你好\n世界"

    def test_html_only_stripped(self):
        text = extract_text(_msg(html="<p>你好</p><p>世界</p><script>x()</script>"))
        assert "你好" in text
        assert "世界" in text
        assert "x()" not in text

    def test_prefers_plain_over_html(self):
        text = extract_text(_msg(plain="纯文本正文", html="<p>HTML 正文</p>"))
        assert text == "纯文本正文"

    def test_quoted_lines_stripped(self):
        text = extract_text(_msg(plain="新内容\n> 旧内容\n> 更多旧内容"))
        assert "新内容" in text
        assert "旧内容" not in text


class TestTruncate:
    def test_short_body_unchanged(self):
        assert truncate_body("abc", 10) == "abc"

    def test_long_body_truncated_with_note(self):
        out = truncate_body("x" * 100, 10)
        assert out.startswith("x" * 10)
        assert "已截断" in out


class TestAttachments:
    def _msg_with_attachment(self, name, data: bytes):
        msg = EmailMessage()
        msg.set_content("正文")
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
        return email.message_from_bytes(msg.as_bytes())

    def test_attachment_saved(self, tmp_path):
        msg = self._msg_with_attachment("a.bin", b"hello")
        items = save_attachments(msg, uploads_dir=tmp_path / "uploads", workspace_dir=tmp_path)
        assert len(items) == 1
        assert items[0].filename == "a.bin"
        assert (tmp_path / "uploads" / "a.bin").read_bytes() == b"hello"

    def test_duplicate_names_unique(self, tmp_path):
        msg = EmailMessage()
        msg.set_content("正文")
        msg.add_attachment(b"1", maintype="application", subtype="octet-stream", filename="a.txt")
        msg.add_attachment(b"2", maintype="application", subtype="octet-stream", filename="a.txt")
        parsed = email.message_from_bytes(msg.as_bytes())
        items = save_attachments(parsed, uploads_dir=tmp_path / "uploads", workspace_dir=tmp_path)
        names = sorted(i.filename for i in items)
        assert names == ["a-1.txt", "a.txt"]

    def test_path_traversal_neutralized(self, tmp_path):
        msg = self._msg_with_attachment("../../evil.txt", b"x")
        items = save_attachments(msg, uploads_dir=tmp_path / "uploads", workspace_dir=tmp_path)
        assert len(items) == 1
        assert items[0].filename == "evil.txt"
        assert (tmp_path / "uploads" / "evil.txt").exists()

    def test_image_kind(self, tmp_path):
        msg = EmailMessage()
        msg.set_content("正文")
        msg.add_attachment(b"\x89PNG", maintype="image", subtype="png", filename="pic.png")
        parsed = email.message_from_bytes(msg.as_bytes())
        items = save_attachments(parsed, uploads_dir=tmp_path / "uploads", workspace_dir=tmp_path)
        assert items[0].kind == "image"
        assert items[0].rel_path == "uploads/pic.png"
