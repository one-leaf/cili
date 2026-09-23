"""Memory tool tests (v3) - action=store/find/read/update/delete/list/stat/consolidate。

v3 数据模型：entries/{type}/{name}.md，定位键是全局唯一的 name（slug）。
type ∈ {fact, preference, skill, reference}，旧的 memory_type/topic/skill_name 已废弃。
"""

import pytest

from core.tools import get_tool_by_name


def _memory_tool(tools):
    return get_tool_by_name(tools, "memory")


class TestMemoryToolStore:
    """store：按 type + name（缺省由 title 派生）写入。"""

    def test_store_fact(self, tools, test_workspace):
        result = _memory_tool(tools).execute(
            action="store",
            type="fact",
            name="rest-api",
            title="REST API Design",
            description="Use plural nouns for resource names",
            content="## Rules\n- use plural nouns",
            tags=["api", "rest"],
            source="user",
        )
        assert not result.error
        assert "Stored fact 'rest-api'" in result.output

    def test_store_derives_name_from_title(self, tools, test_workspace):
        result = _memory_tool(tools).execute(
            action="store", type="skill", title="Python Async", content="use asyncio"
        )
        assert not result.error
        assert "'python-async'" in result.output

    def test_store_chinese_title_hash_name(self, tools, test_workspace):
        result = _memory_tool(tools).execute(
            action="store", type="preference", title="用户偏好简洁回复", content="用简洁中文回复"
        )
        assert not result.error
        assert "memory-" in result.output

    def test_store_replaces_same_name(self, tools, test_workspace):
        t = _memory_tool(tools)
        t.execute(action="store", type="fact", name="same", title="V1", content="v1")
        result = t.execute(action="store", type="fact", name="same", title="V2", content="v2")
        assert not result.error
        assert "Updated fact 'same'" in result.output

    def test_store_missing_type(self, tools, test_workspace):
        result = _memory_tool(tools).execute(action="store", title="X", content="Y")
        assert result.error
        assert "type is required" in result.output

    def test_store_invalid_type(self, tools, test_workspace):
        result = _memory_tool(tools).execute(action="store", type="bogus", title="X", content="Y")
        assert result.error
        assert "Unknown memory type" in result.output

    def test_store_reject_uuid_name(self, tools, test_workspace):
        result = _memory_tool(tools).execute(
            action="store", type="fact", name="550e8400-e29b-41d4-a716-446655440000",
            title="X", content="Y",
        )
        assert result.error
        assert "meaningful" in result.output

    def test_store_reject_path_traversal_name(self, tools, test_workspace):
        for evil in ("..", "../../../evil", "a/../b", "C:\\Users\\evil", "C:/Users/evil"):
            result = _memory_tool(tools).execute(
                action="store", type="fact", name=evil, title="X", content="Y"
            )
            assert result.error, f"name={evil!r} 应被拒绝"

    def test_store_global_name_uniqueness(self, tools, test_workspace):
        """同一 name 不能跨类型复用。"""
        t = _memory_tool(tools)
        t.execute(action="store", type="fact", name="dup", title="A", content="x")
        result = t.execute(action="store", type="skill", name="dup", title="B", content="y")
        assert result.error
        assert "globally unique" in result.output


class TestMemoryToolFind:
    """find：frontmatter 关键词召回（name/title/description/tags/refs）。"""

    def test_find_by_title_keyword(self, tools, test_workspace):
        t = _memory_tool(tools)
        t.execute(
            action="store", type="fact", name="k8s-guide", title="Kubernetes Deploy Guide",
            description="Deploy apps to kubernetes clusters", content="steps", tags=["k8s"],
        )
        result = t.execute(action="find", query="kubernetes")
        assert not result.error
        assert "[fact]" in result.output
        assert "Kubernetes Deploy Guide" in result.output
        assert "name: k8s-guide" in result.output

    def test_find_by_tag_and_case_insensitive(self, tools, test_workspace):
        t = _memory_tool(tools)
        t.execute(action="store", type="fact", name="x", title="X Entry", content="c", tags=["FastAPI"])
        result = t.execute(action="find", query="FASTAPI")
        assert not result.error
        assert "[fact]" in result.output

    def test_find_type_filter(self, tools, test_workspace):
        t = _memory_tool(tools)
        t.execute(action="store", type="fact", name="dep-fact", title="Deploy Fact",
                  description="blue-green deploy pipeline", content="x")
        t.execute(action="store", type="skill", name="dep-skill", title="Deploy Skill",
                  description="blue-green deploy for prod", content="x")
        result = t.execute(action="find", query="deploy", type="skill")
        assert not result.error
        assert "[skill]" in result.output
        assert "[fact]" not in result.output

    def test_find_no_result(self, tools, test_workspace):
        result = _memory_tool(tools).execute(action="find", query="nonexistent-xyz-9182")
        assert not result.error
        assert "No memory matches" in result.output

    def test_find_missing_query(self, tools, test_workspace):
        result = _memory_tool(tools).execute(action="find")
        assert result.error
        assert "query is required" in result.output

    def test_find_does_not_increment_usage(self, tools, test_workspace):
        """find 只检索不递增 usage（真实使用以 read 为准）。"""
        t = _memory_tool(tools)
        t.execute(action="store", type="fact", name="counter", title="C", content="body")
        t.execute(action="find", query="counter")
        result = t.execute(action="find", query="counter")
        assert "uses: 0" in result.output


class TestMemoryToolReadUpdateDelete:
    def test_read_increments_usage(self, tools, test_workspace):
        t = _memory_tool(tools)
        t.execute(action="store", type="fact", name="counter", title="C", content="the body text")
        result = t.execute(action="read", name="counter")
        assert not result.error
        assert "the body text" in result.output
        # read 后 usage 从 0 → 1
        find = t.execute(action="find", query="counter")
        assert "uses: 1" in find.output

    def test_read_missing_name(self, tools, test_workspace):
        result = _memory_tool(tools).execute(action="read")
        assert result.error
        assert "name is required" in result.output

    def test_read_unknown_name(self, tools, test_workspace):
        result = _memory_tool(tools).execute(action="read", name="does-not-exist")
        assert result.error
        assert "no memory entry named" in result.output

    def test_update_entry(self, tools, test_workspace):
        t = _memory_tool(tools)
        t.execute(action="store", type="fact", name="up", title="Old", content="v1")
        result = t.execute(action="update", name="up", title="New", content="v2")
        assert not result.error
        assert "Updated 'up'" in result.output
        read = t.execute(action="read", name="up")
        assert "v2" in read.output
        assert "v1" not in read.output

    def test_update_missing_name(self, tools, test_workspace):
        result = _memory_tool(tools).execute(action="update", content="x")
        assert result.error
        assert "name is required" in result.output

    def test_delete_entry(self, tools, test_workspace):
        t = _memory_tool(tools)
        t.execute(action="store", type="fact", name="del", title="X", content="y")
        result = t.execute(action="delete", name="del")
        assert not result.error
        assert "Deleted fact 'del'" in result.output
        # 删除后 find 不到
        assert "No memory matches" in t.execute(action="find", query="del").output

    def test_delete_missing_name(self, tools, test_workspace):
        result = _memory_tool(tools).execute(action="delete")
        assert result.error
        assert "name is required" in result.output


class TestMemoryToolListStat:
    def test_list_entries(self, tools, test_workspace):
        t = _memory_tool(tools)
        t.execute(action="store", type="fact", name="one", title="One Entry", content="x")
        result = t.execute(action="list")
        assert not result.error
        assert "1 memory entry" in result.output
        assert "One Entry" in result.output

    def test_list_empty(self, tools, test_workspace):
        result = _memory_tool(tools).execute(action="list")
        assert not result.error
        assert "No memory entries." in result.output

    def test_stat_shows_counts(self, tools, test_workspace):
        t = _memory_tool(tools)
        t.execute(action="store", type="fact", name="f", title="F", content="x")
        result = t.execute(action="stat")
        assert not result.error
        assert "entries: 1" in result.output
        assert "fact: 1" in result.output


class TestMemoryToolConsolidate:
    def test_consolidate_delegates(self, monkeypatch, tools, test_workspace):
        """consolidate 动作委托 core.memory_pipeline.consolidate_all（方法内 import）。"""
        from core import memory_pipeline

        fake_result = [{
            "workspace_uuid": "w-fake",
            "processed": 2,
            "applied": [{"op": "store", "name": "a"}, {"op": "update", "name": "b"}],
            "archived": [],
            "pending_after": 0,
            "committed": True,
        }]
        monkeypatch.setattr(memory_pipeline, "consolidate_all", lambda **kw: fake_result)

        result = _memory_tool(tools).execute(action="consolidate")
        assert not result.error
        assert "w-fake" in result.output
        assert "1 stored, 1 updated" in result.output

    def test_consolidate_no_workspaces(self, monkeypatch, tools, test_workspace):
        from core import memory_pipeline

        monkeypatch.setattr(memory_pipeline, "consolidate_all", lambda **kw: [])
        result = _memory_tool(tools).execute(action="consolidate")
        assert not result.error
        assert "No workspace memory to consolidate." in result.output
