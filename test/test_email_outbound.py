"""邮件回信组装：线程头、头注入防御、附件限额。"""

import email
import email.policy

import pytest

from interfaces.email.config import EmailConfig
from interfaces.email.outbound import ReplyContext, build_reply


def _cfg(**overrides) -> EmailConfig:
    base = dict(
        id="agent@example.com",
        label="助手",
        email_address="agent@example.com",
        max_outbound_files=2,
        max_outbound_bytes=1024,
    )
    base.update(overrides)
    return EmailConfig(**base)


def _ctx(**overrides) -> ReplyContext:
    base = dict(
        sender="me@example.com",
        subject="季度报告",
        message_id="<orig@example.com>",
        references="<older@example.com>",
    )
    base.update(overrides)
    return ReplyContext(**base)


class TestReplyHeaders:
    def test_subject_gets_re_prefix(self):
        msg = build_reply(_cfg(), _ctx(), "正文")
        assert msg["Subject"] == "Re: 季度报告"

    def test_existing_re_not_doubled(self):
        msg = build_reply(_cfg(), _ctx(subject="Re: 季度报告"), "正文")
        assert msg["Subject"] == "Re: 季度报告"

    def test_empty_subject_default(self):
        msg = build_reply(_cfg(), _ctx(subject=""), "正文")
        assert msg["Subject"] == "Cili 回复"

    def test_subject_crlf_stripped(self):
        # 头注入防御：主题里的换行不能变成新头部
        msg = build_reply(_cfg(), _ctx(subject="你好\r\nBcc: evil@x.com"), "正文")
        assert "\n" not in msg["Subject"]
        assert msg["Bcc"] is None

    def test_thread_headers(self):
        msg = build_reply(_cfg(), _ctx(), "正文")
        assert msg["In-Reply-To"] == "<orig@example.com>"
        assert msg["References"] == "<older@example.com> <orig@example.com>"

    def test_auto_submitted_marks_loop_guard(self):
        msg = build_reply(_cfg(), _ctx(), "正文")
        assert msg["Auto-Submitted"] == "auto-replied"

    def test_from_and_to(self):
        msg = build_reply(_cfg(), _ctx(), "正文")
        assert "agent@example.com" in msg["From"]
        assert msg["To"] == "me@example.com"

    def test_body_content(self):
        msg = build_reply(_cfg(), _ctx(), "第一行\n第二行")
        body = msg.get_body(preferencelist=("plain",)).get_content()
        assert "第一行" in body and "第二行" in body


class TestAttachments:
    def _file(self, tmp_path, name, size):
        path = tmp_path / name
        path.write_bytes(b"x" * size)
        return str(path)

    def test_attachment_added(self, tmp_path):
        path = self._file(tmp_path, "报告.txt", 10)
        msg = build_reply(_cfg(), _ctx(), "见附件", [path])
        parts = [p for p in msg.iter_attachments()]
        assert len(parts) == 1
        assert parts[0].get_filename() == "报告.txt"

    def test_too_many_files_rejected(self, tmp_path):
        paths = [self._file(tmp_path, f"f{i}.txt", 1) for i in range(3)]
        with pytest.raises(ValueError):
            build_reply(_cfg(), _ctx(), "见附件", paths)

    def test_total_size_rejected(self, tmp_path):
        path = self._file(tmp_path, "big.bin", 2048)   # 上限 1024
        with pytest.raises(ValueError):
            build_reply(_cfg(), _ctx(), "见附件", [path])

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            build_reply(_cfg(), _ctx(), "见附件", [str(tmp_path / "nope.txt")])

    def test_reply_parses_back(self, tmp_path):
        path = self._file(tmp_path, "a.txt", 5)
        msg = build_reply(_cfg(), _ctx(), "正文", [path])
        parsed = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
        assert parsed["Subject"] == "Re: 季度报告"
        assert parsed["Auto-Submitted"] == "auto-replied"
