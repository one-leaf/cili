"""邮件入站过滤闸（垃圾/自动消息/退信/认证/白名单/额度/去重）。"""

import email
from email.message import EmailMessage

from interfaces.email.filter import (
    RateLimiter,
    SeenIds,
    check_message,
    is_allowed,
    sender_address,
)

ALLOWED = ["me@example.com"]
MAX_SIZE = 100 * 1024 * 1024


def _msg(*, headers: dict | None = None, body: str = "hello", from_addr="me@example.com",
         content_type: str = "text/plain"):
    """构造一封邮件（只关心头部时体内容不重要）。"""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "agent@example.com"
    msg["Subject"] = "test"
    msg["Message-ID"] = "<abc@example.com>"
    for key, value in (headers or {}).items():
        msg[key] = value
    msg.set_content(body)
    if content_type != "text/plain":
        del msg["Content-Type"]
        msg["Content-Type"] = content_type
    return email.message_from_bytes(msg.as_bytes())


def _check(msg, *, size=1000, allowed=ALLOWED):
    return check_message(
        msg, size=size, allowed_senders=allowed, max_inbound_bytes=MAX_SIZE,
    )


class TestSenderHelpers:
    def test_sender_address_lowercase(self):
        msg = _msg(from_addr="Alice <Me@Example.COM>")
        assert sender_address(msg) == "me@example.com"

    def test_whitelist_case_insensitive(self):
        assert is_allowed("ME@example.com", ALLOWED)
        assert not is_allowed("me@example.com", [])
        assert not is_allowed("", ALLOWED)


class TestGates:
    def test_whitelisted_passes(self):
        assert _check(_msg()) is None

    def test_not_whitelisted(self):
        assert _check(_msg(from_addr="stranger@evil.com")) == "not_whitelisted"

    def test_empty_whitelist_rejects_all(self):
        # 空白名单 = 账号整体拒收（不是「全放行」）
        assert _check(_msg(), allowed=[]) == "not_whitelisted"

    def test_too_large(self):
        assert _check(_msg(), size=MAX_SIZE + 1) == "too_large"

    def test_dsn_multipart_report(self):
        msg = _msg(content_type="multipart/report; report-type=delivery-status")
        assert _check(msg) == "dsn"

    def test_bounce_null_return_path(self):
        msg = _msg(headers={"Return-Path": "<>"}, from_addr="me@example.com")
        assert _check(msg) == "bounce"

    def test_bounce_mailer_daemon(self):
        msg = _msg(from_addr="Mail Delivery System <MAILER-DAEMON@example.com>")
        # MAILER-DAEMON 不在白名单，但退信闸先于白名单闸命中
        assert _check(msg) == "bounce"

    def test_auto_submitted(self):
        assert _check(_msg(headers={"Auto-Submitted": "auto-generated"})) == "auto"
        assert _check(_msg(headers={"Auto-Submitted": "auto-replied"})) == "auto"

    def test_auto_submitted_no_passes(self):
        assert _check(_msg(headers={"Auto-Submitted": "no"})) is None

    def test_bulk_precedence(self):
        assert _check(_msg(headers={"Precedence": "bulk"})) == "bulk"
        assert _check(_msg(headers={"Precedence": "list"})) == "bulk"
        assert _check(_msg(headers={"Precedence": "junk"})) == "bulk"

    def test_list_id(self):
        assert _check(_msg(headers={"List-Id": "<dev.example.com>"})) == "bulk"

    def test_spam_flag(self):
        assert _check(_msg(headers={"X-Spam-Flag": "YES"})) == "spam"

    def test_spam_status(self):
        assert _check(_msg(headers={"X-Spam-Status": "Yes, score=9.9"})) == "spam"
        # "No, score=..." 不应误杀
        assert _check(_msg(headers={"X-Spam-Status": "No, score=0.1"})) is None

    def test_dmarc_fail(self):
        msg = _msg(headers={
            "Authentication-Results":
                "mx.example.com; spf=pass smtp.mailfrom=evil.com; "
                "dkim=pass header.d=evil.com; dmarc=fail header.from=example.com",
        })
        assert _check(msg) == "auth"

    def test_spf_and_dkim_fail_without_dmarc(self):
        msg = _msg(headers={
            "Authentication-Results":
                "mx.example.com; spf=fail; dkim=fail header.d=example.com",
        })
        assert _check(msg) == "auth"

    def test_spf_fail_alone_passes(self):
        # 只有 spf 失败、dkim 通过 → 不判认证失败（转发场景常见）
        msg = _msg(headers={
            "Authentication-Results": "mx.example.com; spf=softfail; dkim=pass",
        })
        assert _check(msg) is None

    def test_missing_auth_header_passes(self):
        # 小型自建服务器不写 Authentication-Results，不能因此全拒
        assert _check(_msg()) is None

    def test_gate_order_whitelist_after_cheap_checks(self):
        # 非白名单 + 垃圾标记 → 报垃圾标记（先命中的闸）
        msg = _msg(headers={"X-Spam-Flag": "YES"}, from_addr="stranger@evil.com")
        assert _check(msg) == "spam"


class TestRateLimiter:
    def test_per_sender_limit(self):
        now = [0.0]

        def clock():
            return now[0]

        limiter = RateLimiter(per_sender_per_hour=2, per_account_per_day=100, clock=clock)
        assert limiter.allow("a@x.com")
        assert limiter.allow("a@x.com")
        assert not limiter.allow("a@x.com")      # 第三条被挡
        now[0] = 3601.0                          # 窗口滑过后恢复
        assert limiter.allow("a@x.com")

    def test_per_account_limit(self):
        now = [0.0]

        def clock():
            return now[0]

        limiter = RateLimiter(per_sender_per_hour=100, per_account_per_day=2, clock=clock)
        assert limiter.allow("a@x.com")
        assert limiter.allow("b@x.com")
        assert not limiter.allow("c@x.com")      # 账号日额度用尽

    def test_senders_are_independent(self):
        limiter = RateLimiter(per_sender_per_hour=1, per_account_per_day=100)
        assert limiter.allow("a@x.com")
        assert limiter.allow("b@x.com")
        assert not limiter.allow("a@x.com")


class TestSeenIds:
    def test_dedup(self):
        seen = SeenIds(capacity=3)
        assert not seen.seen_or_add("<1@x>")
        assert seen.seen_or_add("<1@x>")
        assert not seen.seen_or_add("<2@x>")

    def test_empty_id_not_deduped(self):
        seen = SeenIds()
        assert not seen.seen_or_add("")
        assert not seen.seen_or_add("")

    def test_capacity_evicts_oldest(self):
        seen = SeenIds(capacity=2)
        seen.seen_or_add("<1@x>")
        seen.seen_or_add("<2@x>")
        seen.seen_or_add("<3@x>")   # 挤掉 <1@x>
        assert not seen.seen_or_add("<1@x>")
