"""Tests for core/sub_agent.py — SubAgent construction and pure logic."""

from unittest.mock import MagicMock, patch

import pytest


class TestSubAgentConstruction:
    """SubAgent initialization."""

    def test_basic_construction(self):
        """SubAgent can be created with minimal params."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(task="test task")

            assert agent.task == "test task"
            assert agent.plan is None
            assert agent.max_iterations == 200  # default
            assert agent.max_consecutive_failures == 5  # default
            assert agent.messages == []

    def test_custom_parameters(self):
        """SubAgent accepts custom parameters."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(
                task="complex task",
                plan=["step 1", "step 2"],
                max_iterations=100,
                max_consecutive_failures=10,
                workspace_uuid="test-ws",
                cwd="/tmp",
            )

            assert agent.task == "complex task"
            assert agent.plan == ["step 1", "step 2"]
            assert agent.max_iterations == 100
            assert agent.max_consecutive_failures == 10
            assert agent.workspace_uuid == "test-ws"
            assert agent.cwd == "/tmp"

    def test_stop_check_stored(self):
        """stop_check callable is stored."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            stop_fn = lambda: False
            agent = SubAgent(task="test", stop_check=stop_fn)

            assert agent.stop_check is stop_fn

    def test_exec_id_stored(self):
        """exec_id is stored for session tracking."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(task="test", exec_id="exec_abc123")

            assert agent._exec_id == "exec_abc123"

    def test_builds_tool_schemas(self):
        """Tool schemas are built from tool instances."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config

            # Create mock tools with to_schema
            mock_tool1 = MagicMock()
            mock_tool1.to_schema.return_value = {"name": "bash", "description": "run commands", "input_schema": {}}
            mock_tool2 = MagicMock()
            mock_tool2.to_schema.return_value = {"name": "read", "description": "read files", "input_schema": {}}
            mock_tools.return_value = [mock_tool1, mock_tool2]

            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(task="test")

            assert len(agent.tool_schemas) == 2
            assert agent.tool_schemas[0]["name"] == "bash"
            assert agent.tool_schemas[1]["name"] == "read"


class TestSubAgentBuildTaskSection:
    """SubAgent._build_task_section() task description formatting."""

    def test_task_only(self):
        """Task without plan."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(task="translate the file")
            section = agent._build_task_section()

            assert "translate the file" in section

    def test_task_with_plan(self):
        """Task with plan includes plan steps."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(task="deploy app", plan=["build", "test", "deploy"])
            section = agent._build_task_section()

            assert "deploy app" in section
            assert "build" in section
            assert "test" in section
            assert "deploy" in section


class TestSubAgentElapsedSeconds:
    """SubAgent._elapsed_seconds() timing."""

    def test_not_started(self):
        """Returns 0 when not started."""
        with patch("core.sub_agent.load_config") as mock_load, \
             patch("core.sub_agent.create_sub_tools") as mock_tools, \
             patch("core.sub_agent.build_sub_prompt", return_value="test prompt"), \
             patch("core.sub_agent.create_llm_client") as mock_client:
            mock_config = MagicMock()
            mock_config.model = MagicMock()
            mock_load.return_value = mock_config
            mock_tools.return_value = []
            mock_client.return_value = MagicMock()

            from core.sub_agent import SubAgent
            agent = SubAgent(task="test")

            assert agent._elapsed_seconds() == 0.0
