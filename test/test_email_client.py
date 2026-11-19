"""IMAP 响应解析（客户端里最容易出错的纯函数部分）。"""

import imaplib

from interfaces.email.client import (
    EmailAuthError,
    EmailClient,
    EmailClientError,
)


class FakeIMAP:
    """只实现被调用到的 IMAP 方法。"""

    def __init__(self, *, status=None, uid_results=None):
        self._status = status
        self._uid_results = list(uid_results or [])

    def status(self, mailbox, what):
        return self._status

    def uid(self, command, *args):
        return self._uid_results.pop(0)


class TestUidNext:
    def test_parses_uidnext(self):
        imap = FakeIMAP(status=("OK", [b'"INBOX" (UIDNEXT 12345)']))
        assert EmailClient._uidnext(imap) == 12345

    def test_bad_response(self):
        imap = FakeIMAP(status=("NO", [b"garbage"]))
        assert EmailClient._uidnext(imap) == 0


class TestSearchAfter:
    def test_filters_by_last_uid(self):
        imap = FakeIMAP(uid_results=[("OK", [b"5 8 12"])])
        assert EmailClient._search_after(imap, 8) == [12]

    def test_range_quirk_does_not_replay_last_message(self):
        # "UID 1000:*" 在最大 UID 小于 1000 时仍会返回最后一封（RFC 3501），必须过滤
        imap = FakeIMAP(uid_results=[("OK", [b"95"])])
        assert EmailClient._search_after(imap, 100) == []

    def test_empty_search(self):
        imap = FakeIMAP(uid_results=[("OK", [b""])])
        assert EmailClient._search_after(imap, 1) == []


class TestFetchHeader:
    def test_parses_size_and_headers(self):
        header = b"From: me@example.com\r\nSubject: hi\r\nMessage-ID: <a@x>\r\n\r\n"
        imap = FakeIMAP(uid_results=[(
            "OK",
            [(b"1 (RFC822.SIZE 555 BODY[HEADER] {10}", header), b")"],
        )])
        size, msg = EmailClient._fetch_header(imap, 1)
        assert size == 555
        assert msg["Subject"] == "hi"

    def test_missing_tuple_returns_none(self):
        imap = FakeIMAP(uid_results=[("OK", [b")"])])
        assert EmailClient._fetch_header(imap, 1) is None


class TestErrorMapping:
    def test_auth_error(self):
        err = EmailClient._map_imap_error(imaplib.IMAP4.error(b"LOGIN failed"))
        assert isinstance(err, EmailAuthError)

    def test_generic_error(self):
        err = EmailClient._map_imap_error(imaplib.IMAP4.error(b"NO mailbox"))
        assert isinstance(err, EmailClientError)
        assert not isinstance(err, EmailAuthError)
