"""mail 工具：账号选择、发信审批、读侧行为（全部假客户端，不发真实网络）。"""

import pytest
from email.message import EmailMessage

from core.mail.accounts import EmailConfig
from core.mail.client import MailSummary, ReadResult
from core.security.permission_state import PermissionStore
from core.tools import mail as mail_mod
from core.tools.mail import MailTool, accounts_for_workspace, usable_accounts
from core.tools.permission import META_KEY
from core.untrusted import UNTRUSTED_DATA_BEGIN


def _account(**overrides) -> EmailConfig:
    base = dict(
        id="me@example.com",
        label="",
        enabled=True,
        email_address="me@example.com",
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
        username="me@example.com",
        password="secret",
        allowed_senders=[],
        allowed_recipients=[],
    )
    base.update(overrides)
    return EmailConfig(**base)


@pytest.fixture
def accounts(monkeypatch):
    """把 load_email_accounts 换成可控列表。"""
    holder = {"items": []}
    monkeypatch.setattr(mail_mod, "load_email_accounts", lambda: list(holder["items"]))
    return holder


@pytest.fixture
def fake_client(monkeypatch):
    """替换 MailClient：记录发出的邮件，返回预设的读侧结果。"""

    class Fake:
        sent: list = []
        list_result = (0, [])
        search_result = ([], False)
        read_result = ReadResult()

        def __init__(self, cfg):
            self.cfg = cfg

        def send(self, msg):
            Fake.sent.append((self.cfg, msg))

        def list_recent(self, limit=20, *, unread_only=False):
            return Fake.list_result

        def search_recent(self, criteria, limit=20):
            return Fake.search_result

        def read_message(self, uid, *, max_bytes=0):
            return Fake.read_result

    Fake.sent = []
    monkeypatch.setattr(mail_mod, "MailClient", Fake)
    return Fake


def _tool(tmp_path, store=None, workspace_uuid="ws-test") -> MailTool:
    return MailTool(
        cwd=str(tmp_path), workspace_uuid=workspace_uuid, permission_store=store,
    )


def _message(
    subject="报告", sender="boss@example.com", body="正文", **headers,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(body)
    return msg


def _with_attachment(filename="a.txt", data=b"hello", body="正文") -> EmailMessage:
    msg = _message(body=body)
    msg.add_attachment(
        data, maintype="application", subtype="octet-stream", filename=filename,
    )
    return msg


def _plain_body(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain",))
    return part.get_content() if part is not None else ""


# ── 账号筛选 ────────────────────────────────────────────────────────


class TestAccounts:
    def test_usable_requires_enabled_and_capability(self, accounts):
        accounts["items"] = [
            _account(id="a@x.com", email_address="a@x.com"),
            _account(id="b@x.com", email_address="b@x.com", enabled=False),
            _account(id="c@x.com", email_address="c@x.com", smtp_host="", imap_host=""),
        ]
        assert [a.email_address for a in usable_accounts()] == ["a@x.com"]

    def test_workspace_priority(self, accounts):
        bound = _account(id="b@x.com", email_address="b@x.com", default_workspace_uuid="ws")
        unbound = _account(id="a@x.com", email_address="a@x.com")
        other = _account(id="c@x.com", email_address="c@x.com", default_workspace_uuid="other")
        accounts["items"] = [unbound, other, bound]
        assert [a.email_address for a in accounts_for_workspace("ws")] == [
            "b@x.com", "a@x.com",
        ]
        # 不限定工作区的账号在任何工作区都可用，只是排在绑定账号之后
        assert [a.email_address for a in accounts_for_workspace("other")] == [
            "c@x.com", "a@x.com",
        ]

    def test_tool_hidden_without_accounts(self, accounts, tmp_path):
        from core.tools.registry import create_tools

        accounts["items"] = []
        names = [t.name for t in create_tools(cwd=str(tmp_path), workspace_uuid="ws")]
        assert "mail" not in names

    def test_tool_visible_with_account(self, accounts, tmp_path):
        from core.tools.registry import create_tools

        accounts["items"] = [_account()]
        tools = create_tools(cwd=str(tmp_path), workspace_uuid="ws")
        tool = next(t for t in tools if t.name == "mail")
        assert "me@example.com" in tool.description

    def test_other_workspace_only_returns_error(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account(default_workspace_uuid="other")]
        res = _tool(tmp_path).execute(action="list")
        assert res.is_error
        assert "当前工作区没有可用的邮箱账号" in res.output

    def test_multi_account_uid_action_requires_account(self, accounts, tmp_path, fake_client):
        accounts["items"] = [
            _account(id="a@x.com", email_address="a@x.com"),
            _account(id="b@x.com", email_address="b@x.com"),
        ]
        res = _tool(tmp_path).execute(action="read", uid=1)
        assert res.is_error
        assert "请用 account 显式指定" in res.output

    def test_unknown_account(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        res = _tool(tmp_path).execute(action="list", account="nobody@x.com")
        assert res.is_error
        assert "未知账号" in res.output

    def test_missing_imap(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account(imap_host="")]
        res = _tool(tmp_path).execute(action="list")
        assert res.is_error
        assert "IMAP" in res.output

    def test_missing_smtp(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account(smtp_host="")]
        res = _tool(tmp_path).execute(action="send", to="a@example.com",
                                      subject="s", body="b")
        assert res.is_error
        assert "SMTP" in res.output

    def test_reply_and_forward_require_imap(self, accounts, tmp_path, fake_client):
        # 只有 SMTP 的账号能发新邮件，但回复/转发还要读原邮件
        accounts["items"] = [_account(imap_host="")]
        for action in ("reply", "forward"):
            res = _tool(tmp_path).execute(
                action=action, uid=1, to="a@example.com", body="b",
            )
            assert res.is_error
            assert "SMTP 与 IMAP" in res.output


# ── 发信 ────────────────────────────────────────────────────────────


class TestSend:
    def test_send_same_domain_no_approval(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        res = _tool(tmp_path).execute(
            action="send", to="colleague@example.com", subject="周报", body="内容",
        )
        assert not res.is_error and "已发送" in res.output
        cfg, msg = fake_client.sent[-1]
        assert msg["To"] == "colleague@example.com"
        assert msg["Subject"] == "周报"
        assert "内容" in _plain_body(msg)
        assert "me@example.com" in msg["From"]

    def test_multiple_recipients_parsed(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account(allowed_recipients=["*@*"])]
        res = _tool(tmp_path).execute(
            action="send", to="a@example.com; b@example.org，c@example.net",
            subject="s", body="b",
        )
        assert not res.is_error
        _, msg = fake_client.sent[-1]
        assert msg["To"] == "a@example.com, b@example.org, c@example.net"

    def test_many_recipients_not_truncated(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account(allowed_recipients=["*@*"])]
        recipients = [f"user{i}@example.com" for i in range(25)]
        res = _tool(tmp_path).execute(
            action="send", to=", ".join(recipients), subject="s", body="b",
        )
        assert not res.is_error
        _, msg = fake_client.sent[-1]
        assert all(addr in msg["To"] for addr in recipients)

    def test_subject_crlf_stripped(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        _tool(tmp_path).execute(
            action="send", to="a@example.com", subject="你好\r\nBcc: evil@x.com",
            body="b",
        )
        _, msg = fake_client.sent[-1]
        assert "\n" not in msg["Subject"]
        assert msg["Bcc"] is None

    def test_attachment_inside_workspace(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        (tmp_path / "report.txt").write_text("hello", encoding="utf-8")
        res = _tool(tmp_path).execute(
            action="send", to="a@example.com", subject="s", body="b",
            attachments=["report.txt"],
        )
        assert not res.is_error
        _, msg = fake_client.sent[-1]
        assert [p.get_filename() for p in msg.iter_attachments()] == ["report.txt"]

    def test_attachment_outside_workspace_rejected(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        res = _tool(tmp_path).execute(
            action="send", to="a@example.com", subject="s", body="b",
            attachments=[str(outside)],
        )
        assert res.is_error
        assert "不在工作区内" in res.output
        assert not fake_client.sent

    def test_smtp_auth_error_hint(self, accounts, tmp_path, monkeypatch):
        from core.mail.client import EmailAuthError

        class Failing:
            def __init__(self, cfg): pass
            def send(self, msg): raise EmailAuthError("SMTP 认证失败：bad")

        monkeypatch.setattr(mail_mod, "MailClient", Failing)
        accounts["items"] = [_account()]
        res = _tool(tmp_path).execute(
            action="send", to="a@example.com", subject="s", body="b",
        )
        assert res.is_error
        assert "授权码" in res.output


# ── 审批 ────────────────────────────────────────────────────────────


class TestApproval:
    def test_cross_domain_needs_approval_then_sends(
        self, accounts, tmp_path, fake_client,
    ):
        accounts["items"] = [_account()]
        store = PermissionStore()
        tool = _tool(tmp_path, store=store)

        first = tool.execute(action="send", to="a@other.com", subject="s", body="b")
        assert first.completed is False
        assert META_KEY in first.meta
        assert not fake_client.sent

        store.approve(first.meta[META_KEY]["decision_id"], "x")
        second = tool.execute(action="send", to="a@other.com", subject="s", body="b")
        assert not second.is_error
        assert fake_client.sent

    def test_changed_body_needs_new_approval(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        store = PermissionStore()
        tool = _tool(tmp_path, store=store)

        first = tool.execute(action="send", to="a@other.com", subject="s", body="one")
        store.approve(first.meta[META_KEY]["decision_id"], "x")
        second = tool.execute(action="send", to="a@other.com", subject="s", body="two")
        assert second.completed is False
        assert not fake_client.sent

    def test_no_store_fails_closed(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        res = _tool(tmp_path).execute(
            action="send", to="a@other.com", subject="s", body="b",
        )
        assert res.is_error
        assert "审批通道" in res.output
        assert not fake_client.sent

    def test_allow_all_wildcard(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account(allowed_recipients=["*@*"])]
        res = _tool(tmp_path).execute(
            action="send", to="a@other.com", subject="s", body="b",
        )
        assert not res.is_error
        assert fake_client.sent

    def test_domain_wildcard(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account(allowed_recipients=["*@example.org"])]
        tool = _tool(tmp_path, store=PermissionStore())
        ok = tool.execute(action="send", to="a@example.org", subject="s", body="b")
        assert not ok.is_error
        pending = tool.execute(action="send", to="a@other.org", subject="s", body="b")
        assert pending.completed is False

    def test_inbound_whitelist_exempts(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account(allowed_senders=["a@other.com"])]
        res = _tool(tmp_path).execute(
            action="send", to="a@other.com", subject="s", body="b",
        )
        assert not res.is_error
        assert fake_client.sent

    def test_public_domain_not_exempt(self, accounts, tmp_path, fake_client):
        # gmail 这类公共域名不算「同域名」，必须命中放行名单/白名单
        accounts["items"] = [_account(id="me@gmail.com", email_address="me@gmail.com")]
        res = _tool(tmp_path, store=PermissionStore()).execute(
            action="send", to="attacker@gmail.com", subject="s", body="b",
        )
        assert res.completed is False
        assert not fake_client.sent

    def test_company_domain_still_exempt(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        res = _tool(tmp_path, store=PermissionStore()).execute(
            action="send", to="colleague@example.com", subject="s", body="b",
        )
        assert not res.is_error
        assert fake_client.sent

    def test_self_send_exempt_even_on_public_domain(
        self, accounts, tmp_path, fake_client,
    ):
        accounts["items"] = [_account(id="me@gmail.com", email_address="me@gmail.com")]
        res = _tool(tmp_path, store=PermissionStore()).execute(
            action="send", to="me@gmail.com", subject="s", body="b",
        )
        assert not res.is_error
        assert fake_client.sent

    def test_mixed_whitelist_and_external_needs_approval(
        self, accounts, tmp_path, fake_client,
    ):
        accounts["items"] = [_account(allowed_senders=["a@other.com"])]
        res = _tool(tmp_path, store=PermissionStore()).execute(
            action="send", to="a@other.com, b@other.com", subject="s", body="b",
        )
        assert res.completed is False
        assert not fake_client.sent


# ── 回复 / 转发 ─────────────────────────────────────────────────────


class TestReplyForward:
    def test_reply_thread_headers_and_no_auto_submitted(
        self, accounts, tmp_path, fake_client,
    ):
        accounts["items"] = [_account(allowed_recipients=["*@*"])]
        original = _message(
            subject="季度报告",
            sender="boss@example.com",
            Reply_To="assistant@example.com",
            Message_ID="<orig@example.com>",
            References="<older@example.com>",
        )
        fake_client.read_result = ReadResult(message=original, headers=original, size=100)
        res = _tool(tmp_path).execute(action="reply", uid=7, body="收到")
        assert not res.is_error
        _, msg = fake_client.sent[-1]
        assert msg["To"] == "assistant@example.com"
        assert msg["Subject"] == "Re: 季度报告"
        assert msg["In-Reply-To"] == "<orig@example.com>"
        assert msg["References"] == "<older@example.com> <orig@example.com>"
        assert msg["Auto-Submitted"] is None

    def test_reply_missing_uid(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        fake_client.read_result = ReadResult()
        res = _tool(tmp_path).execute(action="reply", uid=9, body="b")
        assert res.is_error
        assert "找不到 uid=9" in res.output

    def test_forward_prefix_and_original_attachment(
        self, accounts, tmp_path, fake_client,
    ):
        accounts["items"] = [_account(allowed_recipients=["*@*"])]
        original = _with_attachment(filename="报告.txt", body="原正文")
        fake_client.read_result = ReadResult(message=original, headers=original, size=100)
        res = _tool(tmp_path).execute(
            action="forward", uid=3, to="a@other.com", body="请看",
        )
        assert not res.is_error
        _, msg = fake_client.sent[-1]
        assert msg["Subject"] == "Fwd: 报告"
        assert msg["In-Reply-To"] is None
        assert [p.get_filename() for p in msg.iter_attachments()] == ["报告.txt"]
        assert "转发的邮件" in _plain_body(msg)
        assert "原正文" in _plain_body(msg)

    def test_forward_external_needs_approval(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        original = _message(subject="报告")
        fake_client.read_result = ReadResult(message=original, headers=original, size=100)
        res = _tool(tmp_path, store=PermissionStore()).execute(
            action="forward", uid=3, to="a@other.com",
        )
        assert res.completed is False
        assert not fake_client.sent


# ── 查收件箱 ────────────────────────────────────────────────────────


class TestReadSide:
    def test_list_output_and_truncation(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        fake_client.list_result = (
            5,
            [MailSummary(uid=3, sender="a@b.com", subject="hi",
                         date="2026-11-20 10:00", size=2048)],
        )
        res = _tool(tmp_path).execute(action="list")
        assert not res.is_error
        assert res.output.startswith(UNTRUSTED_DATA_BEGIN)
        assert "共 5 封，显示最近 1 封" in res.output
        assert "仅显示最近 1 封" in res.output
        assert "[uid 3]" in res.output

    def test_list_empty(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        res = _tool(tmp_path).execute(action="list")
        assert "共 0 封" in res.output

    def test_read_body_and_attachments(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        msg = _with_attachment(filename="a.txt", body="正文内容")
        fake_client.read_result = ReadResult(message=msg, headers=msg, size=100)
        res = _tool(tmp_path).execute(action="read", uid=1)
        assert not res.is_error
        assert res.output.startswith(UNTRUSTED_DATA_BEGIN)
        assert "正文内容" in res.output
        assert "a.txt" in res.output

    def test_read_too_large_headers_only(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        headers = _message(subject="超大邮件")
        fake_client.read_result = ReadResult(
            headers=headers, size=200 * 1024 * 1024, too_large=True,
        )
        res = _tool(tmp_path).execute(action="read", uid=1)
        assert not res.is_error
        assert "Subject: 超大邮件" in res.output
        assert "未下载正文" in res.output

    def test_search_requires_condition(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        res = _tool(tmp_path).execute(action="search")
        assert res.is_error
        assert "至少需要" in res.output

    def test_search_invalid_date(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        res = _tool(tmp_path).execute(action="search", since="2026/11/01")
        assert res.is_error
        assert "YYYY-MM-DD" in res.output

    def test_search_truncation_notice(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        fake_client.search_result = (
            [MailSummary(uid=1, sender="a@b.com", subject="hi", size=1)], True,
        )
        res = _tool(tmp_path).execute(action="search", subject="hi")
        assert "命中 1 封" in res.output
        assert "只扫描了最近 50 封候选" in res.output

    def test_save_attachment(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        msg = _with_attachment(filename="a.txt", data=b"hello")
        fake_client.read_result = ReadResult(message=msg, headers=msg, size=100)
        res = _tool(tmp_path).execute(action="save_attachment", uid=1)
        assert not res.is_error
        assert (tmp_path / "uploads" / "a.txt").read_bytes() == b"hello"
        assert "uploads/a.txt" in res.output

    def test_save_attachment_name_miss_lists_names(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        msg = _with_attachment(filename="a.txt")
        fake_client.read_result = ReadResult(message=msg, headers=msg, size=100)
        res = _tool(tmp_path).execute(
            action="save_attachment", uid=1, name="nope.pdf",
        )
        assert res.is_error
        assert "a.txt" in res.output

    def test_save_attachment_none(self, accounts, tmp_path, fake_client):
        accounts["items"] = [_account()]
        msg = _message(body="纯文本")
        fake_client.read_result = ReadResult(message=msg, headers=msg, size=100)
        res = _tool(tmp_path).execute(action="save_attachment", uid=1)
        assert res.output == "该邮件没有附件"


def test_select_inbox_is_readonly():
    from core.mail.client import MailClient

    class FakeImap:
        def __init__(self):
            self.calls = []

        def select(self, mailbox, readonly=False):
            self.calls.append((mailbox, readonly))
            return "OK", [b"1"]

        def response(self, name):
            return "UIDVALIDITY", [b"1"]

    imap = FakeImap()
    MailClient._select_inbox(imap)
    assert imap.calls == [("INBOX", True)]