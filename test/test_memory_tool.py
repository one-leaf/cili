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
