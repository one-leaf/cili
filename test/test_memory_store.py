"""MemoryStore + Journal 单元测试（v3 记忆存储引擎）。

不依赖 LLM：直接构造 MemoryStore / Journal，验证条目读写、索引、
检索、老化归档、恰好一次游标。
"""

import datetime as dt
import os

import pytest

from core.memory_store import (
    MemoryStore,
    Journal,
    cleanup_memory_git,
    slugify,
)


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memory"))


# ── store / 索引 ─────────────────────────────────────

class TestStore:
    def test_store_creates_entry_and_index(self, store, tmp_path):
        r = store.store(
            type_="fact", name="rest-api", title="REST API Design",
            description="Use plural nouns for resource names",
            content="## Rules\n- use plural nouns",
            tags=["api", "rest"], source="user",
        )
        assert r["name"] == "rest-api"
        assert r["type"] == "fact"
        assert r["replaced"] is False
        path = tmp_path / "memory" / "entries" / "fact" / "rest-api.md"
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert 'name: "rest-api"' in text
        assert "Use plural nouns" in text
        # 索引已重建
        index = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
        assert "rest-api" in index

    def test_store_derived_name_ascii(self, store):
        r = store.store(type_="skill", title="Python Async", content="use asyncio")
        assert r["name"] == "python-async"
        assert r["replaced"] is False

    def test_store_derived_name_chinese_hash(self, store):
        """中文标题 → memory-{md5[:8]}，确定性且全局唯一。"""
        r1 = store.store(type_="fact", title="用户偏好简洁回复", content="x")
        r2 = store.store(type_="fact", title="用户偏好简洁回复", content="y")
        assert r1["name"].startswith("memory-")
        assert r1["name"] == r2["name"]
        assert r1["replaced"] is False
        assert r2["replaced"] is True

    def test_store_same_slug_different_title_suffix(self, store):
        """派生 name 与既有不同标题冲突时加 -2，避免误覆盖。"""
        store.store(type_="fact", title="Deploy Guide", content="v1")
        r = store.store(type_="fact", title="Deploy Guide!", content="v2")
        assert r["name"] == "deploy-guide-2"
        assert store.find("deploy-guide")

    def test_store_global_name_uniqueness(self, store):
        """同一 name 不能跨类型复用。"""
        store.store(type_="fact", name="kibana", title="A", content="x")
        with pytest.raises(ValueError):
            store.store(type_="skill", name="kibana", title="B", content="y")

    def test_store_validates_type(self, store):
        with pytest.raises(ValueError):
            store.store(type_="bogus", title="X", content="Y")

    def test_store_validates_description_length(self, store):
        with pytest.raises(ValueError):
            store.store(type_="fact", title="X", content="Y", description="a" * 201)

    def test_store_name_validation(self, store):
        """拒绝路径穿越 / 非 ASCII / UUID 样式的 name。"""
        for evil in ("..", "a/../b", "a\\b", "C:\\evil"):
            with pytest.raises(ValueError):
                store.store(type_="fact", name=evil, title="X", content="Y")
        with pytest.raises(ValueError):
            store.store(type_="fact", name="中文名字", title="X", content="Y")
        with pytest.raises(ValueError):
            store.store(type_="fact", name="550e8400-e29b-41d4-a716-446655440000", title="X", content="Y")

    def test_store_preserves_usage_after_replace(self, store):
        """原地替换保留 created 与 usage_count。"""
        store.store(type_="fact", name="keep-me", title="A", content="v1")
        store.read("keep-me")
        r = store.store(type_="fact", name="keep-me", title="A", content="v2")
        assert r["replaced"] is True
        fm, _ = store.peek("keep-me")
        assert fm["usage_count"] == 1

    def test_slugify(self):
        assert slugify("REST API Design") == "rest-api-design"
        assert slugify("  multiple   spaces  ") == "multiple-spaces"
        assert slugify("") == "untitled"
        assert slugify("用户偏好").startswith("memory-")


# ── find / read / peek / list ────────────────────────

class TestFindRead:
    def _seed(self, store):
        store.store(type_="fact", name="k8s-guide", title="Kubernetes Deploy Guide",
                    description="Deploy apps to kubernetes clusters", content="steps", tags=["k8s"])
        store.store(type_="skill", name="flask-fastapi", title="Flask Migration",
                    description="Migrate legacy flask apps", content="steps", tags=["python"])

    def test_find_by_title_keyword(self, store):
        self._seed(store)
        hits = store.find("kubernetes")
        assert [h["name"] for h in hits] == ["k8s-guide"]

    def test_find_case_insensitive_and_tag(self, store):
        self._seed(store)
        assert store.find("K8S")
        assert store.find("python")

    def test_find_type_filter(self, store):
        self._seed(store)
        hits = store.find("migration", type_="skill")
        assert [h["name"] for h in hits] == ["flask-fastapi"]
        assert store.find("migration", type_="fact") == []

    def test_find_no_result(self, store):
        assert store.find("nonexistent-xyz") == []

    def test_find_requires_query(self, store):
        with pytest.raises(ValueError):
            store.find("")

    def test_read_increments_usage_peek_does_not(self, store):
        store.store(type_="fact", name="counter", title="C", content="body")
        fm0, _ = store.peek("counter")
        assert fm0["usage_count"] == 0
        fm1, body = store.read("counter")
        assert fm1["usage_count"] == 1
        assert body.strip() == "body"
        assert fm1["last_used"]
        # read 后再 peek：不递增
        fm2, _ = store.peek("counter")
        assert fm2["usage_count"] == 1

    def test_find_excludes_archived(self, store):
        store.store(type_="fact", name="gone", title="Gone Entry", content="x")
        store.archive("gone")
        # find 只召回 active/stale，归档条目不参与召回（含显式 status=archived）
        assert store.find("gone") == []
        assert store.find("gone", status="archived") == []
        # 归档条目走 list(status="archived")
        assert store.list(status="archived")[0]["name"] == "gone"

    def test_read_missing_raises(self, store):
        with pytest.raises(ValueError):
            store.read("nope")

    def test_list_defaults_exclude_archived(self, store):
        store.store(type_="fact", name="a1", title="A", content="x")
        store.store(type_="fact", name="a2", title="B", content="y")
        store.archive("a2")
        names = [e["name"] for e in store.list()]
        assert names == ["a1"]
        archived = [e["name"] for e in store.list(status="archived")]
        assert archived == ["a2"]


# ── update / delete / archive / restore ──────────────

class TestMutation:
    def test_update_preserves_created(self, store):
        store.store(type_="fact", name="up", title="Old", description="d", content="v1")
        store.update("up", title="New", content="v2", tags=["t1"])
        fm, body = store.peek("up")
        assert fm["title"] == "New"
        assert fm["tags"] == ["t1"]
        assert body.strip() == "v2"
        assert fm["created"]
        assert fm["usage_count"] == 0

    def test_update_description_too_long(self, store):
        store.store(type_="fact", name="up", title="Old", content="v1")
        with pytest.raises(ValueError):
            store.update("up", description="b" * 201)

    def test_update_missing_raises(self, store):
        with pytest.raises(ValueError):
            store.update("nope", content="x")

    def test_delete_removes_file_and_index(self, store, tmp_path):
        store.store(type_="fact", name="del", title="X", content="y")
        path = tmp_path / "memory" / "entries" / "fact" / "del.md"
        assert path.is_file()
        store.delete("del")
        assert not path.exists()
        index = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
        assert "del" not in index

    def test_archive_restore_roundtrip(self, store, tmp_path):
        store.store(type_="fact", name="arch", title="A", content="body")
        store.archive("arch")
        archived_path = tmp_path / "memory" / "archive" / "fact" / "arch.md"
        assert archived_path.is_file()
        assert store.find("arch") == []
        store.restore("arch", type_="fact")
        fm, _ = store.peek("arch")
        assert fm["status"] == "active"
        assert store.find("arch")[0]["name"] == "arch"

    def test_archive_missing_raises(self, store):
        with pytest.raises(ValueError):
            store.archive("nope")

    def test_archive_stale_zero_use(self, store):
        """usage_count=0 且超过 90 天 → archive_stale 归档（用旧时间戳模拟）。"""
        store.store(type_="fact", name="old", title="Old", content="x")
        path = store._entry_path("fact", "old")
        # 直接改写 updated 为 100 天前，模拟长期未使用
        old = dt.datetime.now() - dt.timedelta(days=100)
        fm, body = store._load_file(path)
        fm["updated"] = old.strftime("%Y-%m-%d %H:%M:%S")
        store._save_file(path, fm, body)
        archived = store.archive_stale()
        assert "old" in archived
        assert store.find("old") == []

    def test_stat(self, store):
        store.store(type_="fact", name="f1", title="F", content="x")
        store.store(type_="skill", name="s1", title="S", content="y")
        store.store(type_="preference", name="p1", title="P", content="z")
        store.archive("p1")
        s = store.stat()
        assert s["total"] == 2
        assert s["archived"] == 1
        assert s["by_type"]["fact"] == 1
        assert s["by_type"]["skill"] == 1


# ── Journal（恰好一次）───────────────────────────────

class TestJournal:
    def _journal(self, tmp_path):
        return Journal(str(tmp_path / "memory"))

    def test_append_dedup_by_key(self, tmp_path):
        j = self._journal(tmp_path)
        c1 = j.append(key="extract:s1:m1:0", title="A", content="x")
        c2 = j.append(key="extract:s1:m1:0", title="A", content="x")
        assert c1 == c2 == 1
        assert j.pending_count() == 1

    def test_cursor_monotonic(self, tmp_path):
        j = self._journal(tmp_path)
        assert j.cursor() == 0
        j.advance(5)
        assert j.cursor() == 5
        j.advance(2)  # 不回退
        assert j.cursor() == 5

    def test_read_pending_after_cursor(self, tmp_path):
        j = self._journal(tmp_path)
        j.append(key="k1", title="A", content="x")
        j.append(key="k2", title="B", content="y")
        assert j.pending_count() == 2
        pending = j.read_pending(limit=10)
        assert [p["cursor"] for p in pending] == [1, 2]
        j.advance(1)
        assert j.pending_count() == 1
        assert [p["cursor"] for p in j.read_pending()] == [2]

    def test_pending_excludes_integrated(self, tmp_path):
        j = self._journal(tmp_path)
        j.append(key="k1", title="A", content="x", integrated=True)
        assert j.pending_count() == 0

    def test_compact_keeps_recent(self, tmp_path):
        j = self._journal(tmp_path)
        for i in range(10):
            j.append(key=f"k{i}", title=f"t{i}", content="x")
        j.advance(10)  # 全部已整合，才允许裁剪
        removed = j.compact(keep=3)
        assert removed == 7
        records = j._read_records()
        assert len(records) == 3


# ── memory git 目录清理 ─────────────────────────────────────────────


class TestCleanupMemoryGit:
    def test_cleanup_removes_git_dir(self, tmp_path):
        """cleanup_memory_git 删除 memory/.git 目录，不影响条目文件。"""
        mem = tmp_path / "memory"
        mem.mkdir()
        git_dir = mem / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref", encoding="utf-8")
        (mem / "MEMORY.md").write_text("index", encoding="utf-8")

        removed = cleanup_memory_git(mem)
        assert removed == 1
        assert not git_dir.exists()
        assert (mem / "MEMORY.md").exists()

    def test_cleanup_removes_bak_dirs(self, tmp_path):
        """cleanup_memory_git 同时清理 .git.bak.* 备份目录。"""
        mem = tmp_path / "memory"
        mem.mkdir()
        bak1 = mem / ".git.bak.1000"
        bak2 = mem / ".git.bak.2000"
        bak1.mkdir()
        bak2.mkdir()
        (bak1 / "HEAD").write_text("ref", encoding="utf-8")

        removed = cleanup_memory_git(mem)
        assert removed == 2
        assert not bak1.exists()
        assert not bak2.exists()

    def test_cleanup_no_git_returns_zero(self, tmp_path):
        """没有 .git 目录时返回 0。"""
        mem = tmp_path / "memory"
        mem.mkdir()
        assert cleanup_memory_git(mem) == 0
