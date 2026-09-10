"""Memory tool tests"""

import os
import pytest


class TestMemoryTool:
    """Memory tool tests"""

    def test_memory_tool_store_knowledge(self, tools, test_workspace):
        """Test storing knowledge memory"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        result = memory_tool.execute(
            action="store",
            memory_type="knowledge",
            topic="api-design",
            title="REST API Design",
            content="Use plural nouns for resource names",
            source="manual",
            tags=["api", "rest"]
        )
        assert not result.error
        assert "stored" in result.output.lower()

    def test_memory_tool_store_skill(self, tools, test_workspace):
        """Test storing skill memory"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        result = memory_tool.execute(
            action="store",
            memory_type="skill",
            skill_name="python-async",
            name="Python Async Programming",
            description="Techniques for async programming in Python using asyncio",
            content="## Overview\nUse asyncio for concurrent I/O operations",
            tags=["python", "async"]
        )
        assert not result.error
        assert "stored" in result.output.lower()

    def test_memory_tool_update_skill(self, tools, test_workspace):
        """Test updating a skill"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        # Store first
        memory_tool.execute(
            action="store",
            memory_type="skill",
            skill_name="update-test-skill",
            name="Update Test Skill",
            description="Original description",
            content="Original content"
        )
        # Then update
        result = memory_tool.execute(
            action="update",
            memory_type="skill",
            skill_name="update-test-skill",
            name="Updated Skill Name",
            description="Updated description",
            content="Updated content"
        )
        assert not result.error
        assert "updated" in result.output.lower()

    def test_memory_tool_delete_skill(self, tools, test_workspace):
        """Test deleting a skill"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        # Store first
        memory_tool.execute(
            action="store",
            memory_type="skill",
            skill_name="delete-test-skill",
            name="Delete Test Skill",
            description="A skill to be deleted",
            content="Content to delete"
        )
        # Then delete
        result = memory_tool.execute(
            action="delete",
            memory_type="skill",
            skill_name="delete-test-skill"
        )
        assert not result.error
        assert "deleted" in result.output.lower()

    def test_memory_tool_update_knowledge(self, tools, test_workspace):
        """Test updating existing knowledge"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        # Store first
        memory_tool.execute(
            action="store",
            memory_type="knowledge",
            topic="test-topic",
            title="Test Memory",
            content="Original content"
        )
        # Then update
        result = memory_tool.execute(
            action="update",
            memory_type="knowledge",
            topic="test-topic",
            title="Test Memory",
            content="Updated content"
        )
        assert not result.error
        assert "updated" in result.output.lower()

    def test_memory_tool_delete_knowledge(self, tools, test_workspace):
        """Test deleting knowledge"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        # Store first
        memory_tool.execute(
            action="store",
            memory_type="knowledge",
            topic="delete-test",
            title="To Delete",
            content="This will be deleted"
        )
        # Then delete
        result = memory_tool.execute(
            action="delete",
            memory_type="knowledge",
            topic="delete-test",
            title="To Delete"
        )
        assert not result.error
        assert "deleted" in result.output.lower()

    def test_memory_tool_missing_topic(self, tools, test_workspace):
        """Test default topic 'misc' when topic is missing for knowledge"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        result = memory_tool.execute(
            action="store",
            memory_type="knowledge",
            title="No Topic"
        )
        assert not result.error
        assert "misc" in result.output

    def test_memory_tool_skill_name_required(self, tools, test_workspace):
        """Test error when skill_name is missing for skill operations"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        result = memory_tool.execute(
            action="store",
            memory_type="skill",
            name="Test Skill",
            description="Test description"
        )
        assert result.error
        assert "skill_name" in result.output.lower()

    def test_memory_tool_skill_name_length_limit(self, tools, test_workspace):
        """Test skill name length limit (64 chars)"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        result = memory_tool.execute(
            action="store",
            memory_type="skill",
            skill_name="long-name",
            name="A" * 65,  # Exceeds 64 char limit
            description="Test description"
        )
        assert result.error
        assert "64" in result.output

    def test_memory_tool_skill_description_length_limit(self, tools, test_workspace):
        """Test skill description length limit (200 chars)"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        result = memory_tool.execute(
            action="store",
            memory_type="skill",
            skill_name="long-desc",
            name="Test Skill",
            description="A" * 201  # Exceeds 200 char limit
        )
        assert result.error
        assert "200" in result.output

    def test_memory_tool_reject_path_traversal_skill(self, tools, test_workspace):
        """拒绝含路径穿越的 skill_name，防止 rmtree 任意目录。"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        for evil in ("..", "../../..", "a/../..", "C:\\Users\\evil", "C:/Users/evil"):
            result = memory_tool.execute(
                action="delete",
                memory_type="skill",
                skill_name=evil,
            )
            assert result.error, f"skill_name={evil!r} 应被拒绝"
            assert "Invalid" in result.output

    def test_memory_tool_reject_path_traversal_knowledge(self, tools, test_workspace):
        """拒绝含路径穿越的 topic / filename。"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        result = memory_tool.execute(
            action="store",
            memory_type="knowledge",
            topic="../../../evil",
            title="X",
            content="Y",
        )
        assert result.error
        assert "Invalid" in result.output

    def test_memory_tool_reject_absolute_filename(self, tools, test_workspace):
        """拒绝绝对路径 filename。"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        result = memory_tool.execute(
            action="store",
            memory_type="knowledge",
            topic="ok-topic",
            title="X",
            filename="..\\..\\evil.md",
            content="Y",
        )
        assert result.error
        assert "Invalid" in result.output

    def test_memory_tool_skill_name_reject_uuid(self, tools, test_workspace):
        """Test that UUID-like skill names are rejected"""
        from core.tools import get_tool_by_name

        memory_tool = get_tool_by_name(tools, "memory")
        # Test skill-UUID format
        result = memory_tool.execute(
            action="store",
            memory_type="skill",
            skill_name="skill-54bb73ce",
            name="Test Skill",
            description="Test description"
        )
        assert result.error
        assert "UUID" in result.output or "meaningful" in result.output.lower()

        # Test full UUID format
        result = memory_tool.execute(
            action="store",
            memory_type="skill",
            skill_name="550e8400-e29b-41d4-a716-446655440000",
            name="Test Skill",
            description="Test description"
        )
        assert result.error
        assert "UUID" in result.output or "meaningful" in result.output.lower()


class TestMemoryToolFind:
    """memory find action：关键词检索 knowledge + skills"""

    def _get_memory_tool(self, tools):
        from core.tools import get_tool_by_name
        return get_tool_by_name(tools, "memory")

    def test_find_knowledge_by_title(self, tools, test_workspace):
        """按标题关键词检索 knowledge，返回完整路径"""
        memory_tool = self._get_memory_tool(tools)
        memory_tool.execute(
            action="store",
            memory_type="knowledge",
            topic="api-design",
            title="Kubernetes Deploy Guide",
            content="How to deploy applications to kubernetes clusters",
        )

        result = memory_tool.execute(action="find", query="kubernetes")
        assert not result.error
        assert "[knowledge]" in result.output
        assert "Kubernetes Deploy Guide" in result.output
        # 返回完整绝对路径，可直接传给 read
        assert os.path.isabs(_first_path_in(result.output))

    def test_find_by_content(self, tools, test_workspace):
        """正文内容命中也能检索到"""
        memory_tool = self._get_memory_tool(tools)
        memory_tool.execute(
            action="store",
            memory_type="knowledge",
            topic="misc",
            title="Odd Title No Keyword",
            content="The secret zebra protocol requires three hops",
        )

        result = memory_tool.execute(action="find", query="zebra")
        assert not result.error
        assert "[knowledge]" in result.output
        assert "Odd Title No Keyword" in result.output
        assert "zebra" in result.output.lower()

    def test_find_skill(self, tools, test_workspace):
        """按名称/描述检索 skill"""
        memory_tool = self._get_memory_tool(tools)
        memory_tool.execute(
            action="store",
            memory_type="skill",
            skill_name="test-find-skill",
            name="Flask Migration Skill",
            description="Migrate legacy flask apps to fastapi",
            content="Step 1: inventory all routes",
        )

        result = memory_tool.execute(action="find", query="fastapi")
        assert not result.error
        assert "[skill]" in result.output
        assert "Flask Migration Skill" in result.output
        assert os.path.isabs(_first_path_in(result.output))

    def test_find_memory_type_filter(self, tools, test_workspace):
        """memory_type 过滤：只搜指定类型"""
        memory_tool = self._get_memory_tool(tools)
        memory_tool.execute(
            action="store",
            memory_type="knowledge",
            topic="misc",
            title="Deploy Knowledge",
            content="blue-green deploy pipeline",
        )
        memory_tool.execute(
            action="store",
            memory_type="skill",
            skill_name="test-deploy-skill",
            name="Deploy Skill",
            description="blue-green deploy pipeline for production",
            content="run the deploy script",
        )

        result = memory_tool.execute(action="find", query="deploy", memory_type="skill")
        assert not result.error
        assert "[skill]" in result.output
        assert "[knowledge]" not in result.output

    def test_find_no_result(self, tools, test_workspace):
        """无命中时返回友好提示"""
        memory_tool = self._get_memory_tool(tools)
        result = memory_tool.execute(action="find", query="nonexistent-xyz-9182")
        assert not result.error
        assert "No memory matches" in result.output

    def test_find_missing_query(self, tools, test_workspace):
        """缺 query 报错"""
        memory_tool = self._get_memory_tool(tools)
        result = memory_tool.execute(action="find")
        assert result.error
        assert "query" in result.output.lower()

    def test_find_sorted_by_mtime_desc(self, tools, test_workspace):
        """结果按文件修改时间倒序（最新在前）"""
        import time

        memory_tool = self._get_memory_tool(tools)
        memory_tool.execute(
            action="store",
            memory_type="knowledge",
            topic="misc",
            title="Old Entry",
            content="sortable keyword alpha",
        )
        # 保证 mtime 有可分辨的先后
        time.sleep(0.05)
        memory_tool.execute(
            action="store",
            memory_type="knowledge",
            topic="misc",
            title="New Entry",
            content="sortable keyword beta",
        )

        result = memory_tool.execute(action="find", query="sortable keyword")
        assert not result.error
        new_pos = result.output.index("New Entry")
        old_pos = result.output.index("Old Entry")
        assert new_pos < old_pos


def _first_path_in(output: str) -> str:
    """从 find 输出中提取第一个 path: 行的路径。"""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("path:"):
            return line[len("path:"):].strip()
    return ""
