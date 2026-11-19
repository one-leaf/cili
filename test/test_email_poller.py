"""轮询游标：首次启动跳过历史、逐封推进、UIDVALIDITY 变化重置。"""

import asyncio
import email

from interfaces.email import state as state_mod
from interfaces.email.client import MailItem, PollResult
from interfaces.email.config import EmailConfig
from interfaces.email.poller import Poller
from interfaces.email.runtime import EmailRuntime
from interfaces.email.state import CursorData, load_cursor, save_cursor

ACCOUNT = "agent@example.com"


class FakeClient:
    def __init__(self, result: PollResult):
        self.result = result
        self.calls: list[dict] = []

    def poll(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeHandler:
    def __init__(self):
        self.items: list[MailItem] = []

    async def handle(self, item):
        self.items.append(item)


def _runtime(tmp_path, monkeypatch, client) -> EmailRuntime:
    monkeypatch.setattr(state_mod, "DATA_DIR", tmp_path)
    cfg = EmailConfig(
        id=ACCOUNT, email_address=ACCOUNT, allowed_senders=["me@example.com"],
    )
    return EmailRuntime(
        account_id=ACCOUNT, cfg=cfg, state=None, loop=None, client=client,
    )


def _run(poller: Poller) -> None:
    asyncio.run(poller._poll_once())


class TestFirstRun:
    def test_skips_history_and_saves_cursor(self, tmp_path, monkeypatch):
        client = FakeClient(PollResult(uidvalidity=7, last_uid=99, reset=True, uidnext=100))
        runtime = _runtime(tmp_path, monkeypatch, client)
        handler = FakeHandler()
        _run(Poller(runtime, handler))

        call = client.calls[0]
        assert call["last_uid"] is None
        assert call["uidvalidity"] == 0
        cursor = load_cursor(ACCOUNT)
        assert cursor.uidvalidity == 7
        assert cursor.last_uid == 99
        assert handler.items == []

    def test_uidvalidity_change_resets_without_replay(self, tmp_path, monkeypatch):
        client = FakeClient(PollResult(uidvalidity=8, last_uid=200, reset=True, uidnext=201))
        runtime = _runtime(tmp_path, monkeypatch, client)
        save_cursor(ACCOUNT, CursorData(uidvalidity=7, last_uid=150))
        _run(Poller(runtime, FakeHandler()))

        call = client.calls[0]
        assert call["last_uid"] == 150
        assert call["uidvalidity"] == 7
        cursor = load_cursor(ACCOUNT)
        assert cursor.uidvalidity == 8
        assert cursor.last_uid == 200


class TestNormalPoll:
    def test_advances_cursor_and_dispatches(self, tmp_path, monkeypatch):
        items = [
            MailItem(uid=11, sender="spam@evil.com", reason="spam"),
            MailItem(uid=12, sender="me@example.com", message_id="<m@x>", reason=None),
        ]
        client = FakeClient(PollResult(uidvalidity=7, last_uid=12, items=items))
        runtime = _runtime(tmp_path, monkeypatch, client)
        handler = FakeHandler()
        save_cursor(ACCOUNT, CursorData(uidvalidity=7, last_uid=10))
        _run(Poller(runtime, handler))

        # 放行的进回合，被拦的不进
        assert [i.uid for i in handler.items] == [12]
        cursor = load_cursor(ACCOUNT)
        assert cursor.last_uid == 12
        assert cursor.ignored[-1]["reason"] == "spam"
        assert cursor.ignored[-1]["from"] == "spam@evil.com"

        status = runtime.status
        assert status["last_error"] == ""
        assert status["last_poll_at"]
        assert status["last_uid"] == 12


class TestDecide:
    def _msg(self, sender="me@example.com", message_id="<abc@x>"):
        raw = (
            f"From: {sender}\r\nTo: agent@example.com\r\n"
            f"Subject: hi\r\nMessage-ID: {message_id}\r\n\r\nbody"
        )
        return email.message_from_string(raw)

    def test_whitelist_gate(self, tmp_path, monkeypatch):
        runtime = _runtime(tmp_path, monkeypatch, FakeClient(PollResult()))
        poller = Poller(runtime, FakeHandler())
        assert poller._decide(self._msg(sender="me@example.com"), 10) is None
        assert poller._decide(self._msg(sender="x@evil.com"), 10) == "not_whitelisted"

    def test_duplicate_gate(self, tmp_path, monkeypatch):
        runtime = _runtime(tmp_path, monkeypatch, FakeClient(PollResult()))
        poller = Poller(runtime, FakeHandler())
        assert poller._decide(self._msg(message_id="<dup@x>"), 10) is None
        assert poller._decide(self._msg(message_id="<dup@x>"), 10) == "duplicate"

    def test_rate_limit_gate(self, tmp_path, monkeypatch):
        runtime = _runtime(tmp_path, monkeypatch, FakeClient(PollResult()))
        runtime.cfg.turns_per_sender_per_hour = 1
        poller = Poller(runtime, FakeHandler())
        assert poller._decide(self._msg(message_id="<a@x>"), 10) is None
        assert poller._decide(self._msg(message_id="<b@x>"), 10) == "rate_limited"
