"""邮件游标与拦截统计（按账号分文件）。"""

import time

import pytest

from interfaces.email import state as state_mod
from interfaces.email.cursor import (
    CursorData,
    add_ignored_event,
    cursor_path,
    ignored_summary,
    load_cursor,
    save_cursor,
)


@pytest.fixture
def email_data_dir(tmp_path, monkeypatch):
    """隔离 DATA_DIR：游标不落真实 data/cili。"""
    monkeypatch.setattr(state_mod, "DATA_DIR", tmp_path)
    import interfaces.email.cursor as cursor_mod
    monkeypatch.setattr(cursor_mod, "DATA_DIR", tmp_path)
    return tmp_path


class TestCursor:
    def test_missing_file_means_first_run(self, email_data_dir):
        assert load_cursor("me@example.com") is None

    def test_save_and_load(self, email_data_dir):
        cursor = CursorData(uidvalidity=42, last_uid=105)
        assert save_cursor("me@example.com", cursor)
        loaded = load_cursor("me@example.com")
        assert loaded is not None
        assert loaded.uidvalidity == 42
        assert loaded.last_uid == 105

    def test_accounts_have_separate_files(self, email_data_dir):
        save_cursor("a@example.com", CursorData(uidvalidity=1, last_uid=10))
        save_cursor("b@example.com", CursorData(uidvalidity=2, last_uid=20))
        assert cursor_path("a@example.com") != cursor_path("b@example.com")
        assert load_cursor("a@example.com").last_uid == 10
        assert load_cursor("b@example.com").last_uid == 20


class TestIgnoredStats:
    def test_summary_counts_recent_only(self, email_data_dir):
        cursor = CursorData(uidvalidity=1, last_uid=1)
        add_ignored_event(cursor, "spam", "a@evil.com")
        # 手工塞一条 30 小时前的记录
        cursor.ignored.append({
            "reason": "auto", "from": "old@x.com", "at": time.time() - 30 * 3600,
        })
        save_cursor("agent@example.com", cursor)
        summary = ignored_summary("agent@example.com")
        assert summary["total"] == 1
        assert summary["by_reason"] == {"spam": 1}
        assert summary["recent"][0]["from"] == "a@evil.com"

    def test_events_are_capped(self, email_data_dir):
        cursor = CursorData()
        for i in range(300):
            add_ignored_event(cursor, "spam", f"x{i}@evil.com")
        assert len(cursor.ignored) == 200
        # 保留的是最近 200 条
        assert cursor.ignored[-1]["from"] == "x299@evil.com"

    def test_summary_without_file(self, email_data_dir):
        assert ignored_summary("nobody@example.com")["total"] == 0
