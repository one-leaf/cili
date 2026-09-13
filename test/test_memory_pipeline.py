"""记忆管线单元测试：提取（恰好一次 + 降级）+ 整合（原子游标推进）+ 开关门控。

用假 extractor/consolidator 回调替换 LLM，验证 journal 去重、指针接续、
RAW 降级、游标只在成功时推进、memory_enabled 工作区过滤。git 提交尽力而为。
"""

import json

import pytest

from core import memory_pipeline
from core.memory_pipeline import (
    redact_secrets,
    memory_enabled,
    run_extraction,
    run_consolidation,
    consolidate_all,
)
from core.memory_store import Journal, MemoryStore, _find_git


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """把 data/agents 重定向到临时目录，隔离真实工作区。"""
    ad = tmp_path / "agents"
    ad.mkdir()
    monkeypatch.setattr("core.config.AGENTS_DIR", ad)
    monkeypatch.setattr("core.memory_pipeline.AGENTS_DIR", ad)
    return ad


def _ws(agents_dir, uuid, *, enabled=False, has_memory=True):
    d = agents_dir / uuid
    d.mkdir(parents=True, exist_ok=True)
    (d / "setting.json").write_text(json.dumps({"memory_enabled": enabled}), encoding="utf-8")
    if has_memory:
        (d / "memory").mkdir(exist_ok=True)
    return d


def _messages():
    return [
        {"role": "user",
         "content": "我的 API key 是 sk-abcdef1234567890xyz，请记住我偏好简洁回复",
         "_meta": {"id": "m1"}},
        {"role": "assistant", "content": "好的，已记住。", "_meta": {"id": "m2"}},
    ]


# ── 密钥掩蔽 ──────────────────────────────────────────

class TestRedactSecrets:
    def test_key_equals_value(self):
        out = redact_secrets("db password: hunter23 and api_key=sk-secret1234567890")
        assert "api_key=***REDACTED***" in out
        assert "sk-secret1234567890" not in out
        assert "hunter23" not in out

    def test_bearer_token(self):
        out = redact_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
        assert "Bearer ***REDACTED***" in out
        assert "abcdefghijklmnopqrstuvwxyz012345" not in out

    def test_bare_token_prefixes(self):
        # 假 token 由片段拼接：保留掩蔽校验的同时，避免与真实密钥格式的静态扫描误匹配
        for token in ("sk-ant-" + "abcdef1234567890abcdef",
                      "ghp_" + "abcdef1234567890abcdef",
                      "AKIAI" + "OSFODNN7EXAMPLE",
                      "xoxb-" + "123456789012-" + "abcdefghijklmnop"):
            out = redact_secrets(f"token={token}")
            assert "***REDACTED***" in out
            assert token not in out

    def test_plain_text_untouched(self):
        text = "the quick brown fox jumps over the lazy dog"
        assert redact_secrets(text) == text

    def test_empty(self):
        assert redact_secrets("") == ""
        assert redact_secrets(None) is None


# ── 提取 ──────────────────────────────────────────────

class TestExtraction:
    def test_happy_path_appends_structured(self, agents_dir):
        _ws(agents_dir, "ws1")

        def fake_extractor(prompt, system, schema):
            return {"memories": [
                {"type": "preference", "title": "简洁回复", "description": "用户偏好",
                 "content": "reply concisely", "tags": ["style"]},
                {"type": "fact", "title": "Server IP", "description": "",
                 "content": "server is 192.168.3.3 with api_key=sk-secret1234567890", "tags": []},
            ]}

        r = run_extraction("ws1", "s1", _messages(), extractor=fake_extractor)
        assert r["appended"] == 2
        assert r["raw"] == 0
        assert r["extracted"] == 2

        journal = Journal(str(agents_dir / "ws1" / "memory"))
        assert journal.pending_count() == 2
        recs = journal.read_pending(limit=10)
        assert recs[0]["type_guess"] == "preference"
        # 密钥已在入库前掩蔽
        assert "sk-secret1234567890" not in recs[1]["content"]
        assert "***REDACTED***" in recs[1]["content"]

    def test_idempotent_rerun(self, agents_dir):
        _ws(agents_dir, "ws1")

        def fake_extractor(prompt, system, schema):
            return {"memories": [{"type": "fact", "title": "T", "content": "x"}]}

        run_extraction("ws1", "s1", _messages(), extractor=fake_extractor)
        # 指针已推进到 m2，重跑同一批消息 → 不新增
        r = run_extraction("ws1", "s1", _messages(), extractor=fake_extractor)
        assert r["skipped"] is True
        assert r["appended"] == 0
        journal = Journal(str(agents_dir / "ws1" / "memory"))
        assert journal.pending_count() == 1

    def test_pointer_continues_from_last_id(self, agents_dir):
        """只提取指针之后的新消息（last_msg_id）。"""
        _ws(agents_dir, "ws1")
        seen: list[str] = []

        def fake_extractor(prompt, system, schema):
            seen.append(prompt[0].content)
            return {"memories": [{"type": "fact", "title": "T", "content": "x"}]}

        msgs = _messages()
        run_extraction("ws1", "s1", msgs, extractor=fake_extractor)
        run_extraction("ws1", "s1", msgs + [{"role": "user", "content": "新消息", "_meta": {"id": "m3"}}],
                       extractor=fake_extractor)
        assert len(seen) == 2
        assert "新消息" in seen[1]
        assert "我的 API key" not in seen[1]

    def test_degraded_appends_raw(self, agents_dir):
        _ws(agents_dir, "ws1")

        def broken(prompt, system, schema):
            raise RuntimeError("llm down")

        r = run_extraction("ws1", "s1", _messages(), extractor=broken)
        assert r["raw"] == 1
        assert r["appended"] == 1
        journal = Journal(str(agents_dir / "ws1" / "memory"))
        recs = journal.read_pending(limit=10)
        assert recs[0]["raw"] is True
        # 降级原文同样掩蔽密钥
        assert "sk-abcdef1234567890xyz" not in recs[0]["content"]
        assert "***REDACTED***" in recs[0]["content"]

    def test_no_new_messages_skips(self, agents_dir):
        _ws(agents_dir, "ws1")
        r = run_extraction("ws1", "s1", [], extractor=lambda *a: {"memories": []})
        assert r["skipped"] is True


# ── 整合 ──────────────────────────────────────────────

class TestConsolidation:
    def _seed(self, agents_dir, uuid="ws1", content="some durable fact"):
        _ws(agents_dir, uuid, enabled=True)
        journal = Journal(str(agents_dir / uuid / "memory"))
        journal.append(key="extract:s1:m1:0", type_guess="fact", title="Test Memory",
                       content=content, source="session")
        return journal

    def test_applies_ops_and_advances_cursor(self, agents_dir):
        self._seed(agents_dir)

        def fake(prompt, system, schema):
            return {
                "ops": [
                    {"op": "store", "type": "fact", "title": "Test Memory",
                     "content": "consolidated body", "tags": ["t"]},
                    {"op": "skip", "name": "unrelated", "reason": "not useful"},
                ],
                "summary": "工作区概况：用户偏好简洁回复。",
            }

        r = run_consolidation("ws1", consolidator=fake)
        assert r["processed"] == 1
        ops = [a["op"] for a in r["applied"]]
        assert ops == ["store", "skip"]
        assert r["pending_after"] == 0

        store = MemoryStore(str(agents_dir / "ws1" / "memory"))
        fm, body = store.peek("test-memory")
        assert body.strip() == "consolidated body"
        assert fm["tags"] == ["t"]
        assert fm["source"] == "derived"

        # summary 已写入
        summary = (agents_dir / "ws1" / "memory" / "summary.md").read_text(encoding="utf-8")
        assert "用户偏好简洁回复" in summary
        assert r["summary_len"] > 0

        # 游标推进到已处理记录
        assert Journal(str(agents_dir / "ws1" / "memory")).cursor() == 1
        assert "committed" in r
        if _find_git() is not None:
            assert r["committed"] is True

    def test_failure_leaves_cursor_untouched(self, agents_dir):
        self._seed(agents_dir)
        journal = Journal(str(agents_dir / "ws1" / "memory"))
        assert journal.cursor() == 0

        def broken(prompt, system, schema):
            raise RuntimeError("consolidator exploded")

        r = run_consolidation("ws1", consolidator=broken)
        assert "error" in r
        assert r["processed"] == 0
        # 失败不推游标 → 可安全重跑
        assert journal.cursor() == 0
        assert journal.pending_count() == 1

        def working(prompt, system, schema):
            return {"ops": [{"op": "store", "type": "fact", "title": "Test Memory", "content": "ok"}],
                    "summary": ""}

        r2 = run_consolidation("ws1", consolidator=working)
        assert r2["processed"] == 1
        assert r2["pending_after"] == 0

    def test_no_pending_returns_empty(self, agents_dir):
        _ws(agents_dir, "ws1", enabled=True)
        r = run_consolidation("ws1")
        assert r["processed"] == 0
        assert r["committed"] is False


# ── 门控 / 全量整合 ───────────────────────────────────

class TestGating:
    def test_memory_enabled_reads_setting(self, agents_dir):
        _ws(agents_dir, "on", enabled=True)
        _ws(agents_dir, "off", enabled=False)
        _ws(agents_dir, "none")
        assert memory_enabled("on") is True
        assert memory_enabled("off") is False
        assert memory_enabled("none") is False

    def test_consolidate_all_skips_disabled(self, agents_dir):
        _ws(agents_dir, "w1", enabled=True)
        _ws(agents_dir, "w2", enabled=False)
        _ws(agents_dir, "w3", enabled=True, has_memory=False)  # 无 memory 目录，不被扫描

        def fake(prompt, system, schema):
            return {"ops": [], "summary": ""}

        results = consolidate_all(consolidator=fake)
        uuids = [r["workspace_uuid"] for r in results]
        assert uuids == ["w1"]
        assert "w2" not in uuids
        assert "w3" not in uuids

    def test_consolidate_all_errors_captured(self, agents_dir):
        _ws(agents_dir, "w1", enabled=True)
        _ws(agents_dir, "w2", enabled=True)
        # 两个工作区都有待整合记录，才能让回调真正被调用
        for uuid in ("w1", "w2"):
            Journal(str(agents_dir / uuid / "memory")).append(
                key=f"extract:{uuid}:m1:0", type_guess="fact", title="T", content="x"
            )

        # 用调用次数让 w1 失败、w2 成功
        calls = {"n": 0}

        def flaky(prompt, system, schema):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return {"ops": [], "summary": ""}

        results = consolidate_all(consolidator=flaky)
        by_uuid = {r["workspace_uuid"]: r for r in results}
        assert "error" in by_uuid["w1"]
        assert "error" not in by_uuid["w2"]
