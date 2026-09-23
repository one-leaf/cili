"""记忆管线单元测试：提取（恰好一次 + 降级）+ 整合（原子游标推进）+ 开关门控。

用假 extractor/consolidator 回调替换 LLM，验证 journal 去重、指针接续、
RAW 降级、游标只在成功时推进、memory_enabled 工作区过滤。
"""

import json
from pathlib import Path

import pytest

from core import memory_pipeline
from core.config import upsert_workspace_entry
from core.memory_pipeline import (
    redact_secrets,
    memory_enabled,
    run_extraction,
    run_consolidation,
    consolidate_all,
)
from core.memory_store import Journal, MemoryStore


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    """把 workspaces.json 索引重定向到临时文件，隔离真实工作区。

    测试工作区在 {projects_dir}/{uuid} 下，.cili/memory/ 是其记忆目录。
    """
    import core.config as config_mod
    monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
    ad = tmp_path / "projects"
    ad.mkdir()
    return ad


def _ws(projects_dir, uuid, *, enabled=False, has_memory=True):
    """创建测试工作区目录并注册到 workspaces.json，返回工作区目录。"""
    d = Path(projects_dir) / uuid
    d.mkdir(parents=True, exist_ok=True)
    upsert_workspace_entry({
        "uuid": uuid,
        "workspace_name": f"Test {uuid}",
        "directory": str(d),
        "memory_enabled": enabled,
    })
    if has_memory:
        (d / ".cili" / "memory").mkdir(parents=True, exist_ok=True)
    return d


def _mem(projects_dir, uuid):
    """返回该工作区的 .cili/memory 目录路径。"""
    return Path(projects_dir) / uuid / ".cili" / "memory"


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
    def test_happy_path_appends_structured(self, projects_dir):
        _ws(projects_dir, "ws1")

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

        journal = Journal(str(_mem(projects_dir, "ws1")))
        assert journal.pending_count() == 2
        recs = journal.read_pending(limit=10)
        assert recs[0]["type_guess"] == "preference"
        # 密钥已在入库前掩蔽
        assert "sk-secret1234567890" not in recs[1]["content"]
        assert "***REDACTED***" in recs[1]["content"]

    def test_idempotent_rerun(self, projects_dir):
        _ws(projects_dir, "ws1")

        def fake_extractor(prompt, system, schema):
            return {"memories": [{"type": "fact", "title": "T", "content": "x"}]}

        run_extraction("ws1", "s1", _messages(), extractor=fake_extractor)
        # 指针已推进到 m2，重跑同一批消息 → 不新增
        r = run_extraction("ws1", "s1", _messages(), extractor=fake_extractor)
        assert r["skipped"] is True
        assert r["appended"] == 0
        journal = Journal(str(_mem(projects_dir, "ws1")))
        assert journal.pending_count() == 1

    def test_pointer_continues_from_last_id(self, projects_dir):
        """只提取指针之后的新消息（last_msg_id）。"""
        _ws(projects_dir, "ws1")
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

    def test_degraded_appends_raw(self, projects_dir):
        _ws(projects_dir, "ws1")

        def broken(prompt, system, schema):
            raise RuntimeError("llm down")

        r = run_extraction("ws1", "s1", _messages(), extractor=broken)
        assert r["raw"] == 1
        assert r["appended"] == 1
        journal = Journal(str(_mem(projects_dir, "ws1")))
        recs = journal.read_pending(limit=10)
        assert recs[0]["raw"] is True
        # 降级原文同样掩蔽密钥
        assert "sk-abcdef1234567890xyz" not in recs[0]["content"]
        assert "***REDACTED***" in recs[0]["content"]

    def test_no_new_messages_skips(self, projects_dir):
        _ws(projects_dir, "ws1")
        r = run_extraction("ws1", "s1", [], extractor=lambda *a: {"memories": []})
        assert r["skipped"] is True


# ── 整合 ──────────────────────────────────────────────

class TestConsolidation:
    def _seed(self, projects_dir, uuid="ws1", content="some durable fact"):
        _ws(projects_dir, uuid, enabled=True)
        journal = Journal(str(_mem(projects_dir, uuid)))
        journal.append(key="extract:s1:m1:0", type_guess="fact", title="Test Memory",
                       content=content, source="session")
        return journal

    def test_applies_ops_and_advances_cursor(self, projects_dir):
        self._seed(projects_dir)

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

        store = MemoryStore(str(_mem(projects_dir, "ws1")))
        fm, body = store.peek("test-memory")
        assert body.strip() == "consolidated body"
        assert fm["tags"] == ["t"]
        assert fm["source"] == "derived"

        # summary 已写入
        summary = (_mem(projects_dir, "ws1") / "summary.md").read_text(encoding="utf-8")
        assert "用户偏好简洁回复" in summary
        assert r["summary_len"] > 0

        # 游标推进到已处理记录
        assert Journal(str(_mem(projects_dir, "ws1"))).cursor() == 1

    def test_failure_leaves_cursor_untouched(self, projects_dir):
        self._seed(projects_dir)
        journal = Journal(str(_mem(projects_dir, "ws1")))
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

    def test_conflicting_store_self_heals(self, projects_dir):
        """store name 与不同类型条目冲突 → 自动追加 -2 后缀新建保留内容，游标照常推进。

        （旧行为是记 failed 不推游标，重跑同批记录会产生同样的冲突 → 确定性死锁。）
        """
        store = MemoryStore(str(_mem(projects_dir, "ws1")))
        store.store(type_="fact", name="foo", title="Foo fact", content="existing")
        self._seed(projects_dir)
        journal = Journal(str(_mem(projects_dir, "ws1")))
        assert journal.cursor() == 0

        def conflicting(prompt, system, schema):
            return {"ops": [{"op": "store", "name": "foo", "type": "preference",
                             "title": "X", "content": "y", "reason": "conflict"}],
                    "summary": ""}

        r = run_consolidation("ws1", consolidator=conflicting)
        assert r["failed"] == []
        names = [a["name"] for a in r["applied"]]
        assert "foo-2" in names
        fm, _ = MemoryStore(str(_mem(projects_dir, "ws1"))).peek("foo-2")
        assert fm["type"] == "preference"
        assert journal.cursor() == 1
        assert journal.pending_count() == 0

    def test_empty_store_op_consumed(self, projects_dir):
        """空 title+content 的 store op → 无可保留内容，视为完成并推进游标，避免死锁。"""
        self._seed(projects_dir)
        journal = Journal(str(_mem(projects_dir, "ws1")))

        def empty_store(prompt, system, schema):
            return {"ops": [{"op": "store", "name": "", "type": "fact", "title": "",
                             "content": "", "reason": "nothing"}],
                    "summary": ""}

        r = run_consolidation("ws1", consolidator=empty_store)
        assert r["failed"] == []
        assert len(r["applied"]) == 1
        assert r.get("error") is None
        assert journal.cursor() == 1
        assert journal.pending_count() == 0

    def test_no_pending_returns_empty(self, projects_dir):
        _ws(projects_dir, "ws1", enabled=True)
        r = run_consolidation("ws1")
        assert r["processed"] == 0

    def test_incomplete_ops_keeps_cursor(self, projects_dir):
        """op 数 < 待整合记录数（截断丢尾部/模型少输出）→ 不推游标，记录保留供重跑。"""
        self._seed(projects_dir)
        journal = Journal(str(_mem(projects_dir, "ws1")))
        assert journal.pending_count() == 1

        def few(prompt, system, schema):
            return {"ops": [], "summary": "partially truncated"}

        r = run_consolidation("ws1", consolidator=few)
        assert "not advanced" in r["error"]
        assert r["applied"] == []
        assert journal.cursor() == 0
        assert journal.pending_count() == 1

    def test_missing_target_ops_do_not_deadlock(self, projects_dir):
        """delete/archive 目标条目不存在、update 目标缺失 → 视为完成/新建保留，队列照常推进。

        复现线上死锁：模型对不存在的条目发 delete/archive/update，旧行为记 failed
        不推游标，重跑同批记录产生同样的 op → 待整合永远不降。
        """
        self._seed(projects_dir)
        journal = Journal(str(_mem(projects_dir, "ws1")))
        assert journal.cursor() == 0

        def hallucinating(prompt, system, schema):
            return {"ops": [
                {"op": "delete", "name": "ghost-entry", "reason": "wrong"},
                {"op": "archive", "name": "ghost-archive", "reason": "obsolete"},
                {"op": "update", "name": "ghost-update", "type": "fact",
                 "title": "New Ghost", "content": "kept content", "reason": "refine"},
            ], "summary": ""}

        r = run_consolidation("ws1", consolidator=hallucinating)
        assert r["failed"] == []
        assert len(r["applied"]) == 3
        # update 退化 store 新建，内容不丢
        fm, body = MemoryStore(str(_mem(projects_dir, "ws1"))).peek("ghost-update")
        assert body.strip() == "kept content"
        assert journal.cursor() == 1
        assert journal.pending_count() == 0

    def test_max_batches_clears_queue(self, projects_dir):
        """max_batches>1 循环整合至清零，返回跨批聚合计数。"""
        _ws(projects_dir, "ws1", enabled=True)
        journal = Journal(str(_mem(projects_dir, "ws1")))
        for i in range(5):
            journal.append(key=f"extract:s1:m{i}:0", type_guess="fact",
                           title=f"Memory {i}", content=f"durable fact {i}", source="session")

        def per_record(prompt, system, schema):
            n = prompt[0].content.count("[cursor")
            return {"ops": [{"op": "store", "type": "fact", "title": "T", "content": "b"}
                            for _ in range(n)], "summary": ""}

        r = run_consolidation("ws1", consolidator=per_record, limit=2, max_batches=4)
        assert "error" not in r
        assert r["processed"] == 5
        assert len(r["applied"]) == 5
        assert r["pending_after"] == 0
        assert journal.pending_count() == 0

    def test_default_max_batches_one(self, projects_dir):
        """缺省 max_batches=1 只处理一批（limit 条），行为与旧版一致。"""
        _ws(projects_dir, "ws1", enabled=True)
        journal = Journal(str(_mem(projects_dir, "ws1")))
        for i in range(5):
            journal.append(key=f"extract:s1:m{i}:0", type_guess="fact",
                           title=f"Memory {i}", content=f"durable fact {i}", source="session")

        def per_record(prompt, system, schema):
            n = prompt[0].content.count("[cursor")
            return {"ops": [{"op": "store", "type": "fact", "title": "T", "content": "b"}
                            for _ in range(n)], "summary": ""}

        r = run_consolidation("ws1", consolidator=per_record, limit=2)
        assert r["processed"] == 2
        assert r["pending_after"] == 3

    def test_truncated_batch_splits_and_advances(self, projects_dir):
        """整批整合抛错（模拟 max_tokens 截断）时拆半重试，两半各自成功 → 全部应用并推进游标。"""
        _ws(projects_dir, "ws1", enabled=True)
        journal = Journal(str(_mem(projects_dir, "ws1")))
        for i in range(4):
            journal.append(key=f"extract:s1:m{i}:0", type_guess="fact",
                           title=f"Memory {i}", content=f"durable fact {i}", source="session")
        assert journal.pending_count() == 4

        calls: list[int] = []

        def splitty(prompt, system, schema):
            calls.append(len(prompt[0].content))
            # 4 条记录一起整合必失败（截断）；拆半后各自成功
            if len(calls) == 1:
                raise RuntimeError("truncated")
            return {"ops": [{"op": "store", "type": "fact", "title": "T",
                             "content": f"body {len(calls)}"}],
                    "summary": "ok"}

        r = run_consolidation("ws1", consolidator=splitty)
        assert "error" not in r
        assert len(calls) > 1  # 确实发生了拆半重试
        assert len(r["applied"]) == 4
        assert journal.cursor() == 4
        assert journal.pending_count() == 0


# ── 门控 / 全量整合 ───────────────────────────────────

class TestGating:
    def test_memory_enabled_reads_setting(self, projects_dir):
        _ws(projects_dir, "on", enabled=True)
        _ws(projects_dir, "off", enabled=False)
        _ws(projects_dir, "none")
        assert memory_enabled("on") is True
        assert memory_enabled("off") is False
        assert memory_enabled("none") is False

    def test_consolidate_all_skips_disabled(self, projects_dir):
        _ws(projects_dir, "w1", enabled=True)
        _ws(projects_dir, "w2", enabled=False)
        _ws(projects_dir, "w3", enabled=True, has_memory=False)  # 无 memory 目录，不被扫描

        def fake(prompt, system, schema):
            return {"ops": [], "summary": ""}

        results = consolidate_all(consolidator=fake)
        uuids = [r["workspace_uuid"] for r in results]
        assert uuids == ["w1"]
        assert "w2" not in uuids
        assert "w3" not in uuids

    def test_consolidate_all_errors_captured(self, projects_dir):
        _ws(projects_dir, "w1", enabled=True)
        _ws(projects_dir, "w2", enabled=True)
        # 两个工作区都有待整合记录，才能让回调真正被调用
        for uuid in ("w1", "w2"):
            Journal(str(_mem(projects_dir, uuid))).append(
                key=f"extract:{uuid}:m1:0", type_guess="fact", title="T", content="x"
            )

        # 用调用次数让 w1 失败、w2 成功
        calls = {"n": 0}

        def flaky(prompt, system, schema):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return {"ops": [{"op": "store", "type": "fact", "title": "T", "content": "x"}],
                    "summary": ""}

        results = consolidate_all(consolidator=flaky)
        by_uuid = {r["workspace_uuid"]: r for r in results}
        assert "error" in by_uuid["w1"]
        assert "error" not in by_uuid["w2"]
