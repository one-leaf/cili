"""Integration tests for BaseAgent architecture.

使用 DGX 本地端点测试，同时覆盖 Anthropic 和 OpenAI 两种协议。
DGX 端点：http://192.168.3.3:8080（本地推理，不消耗 API 配额）。
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from unittest.mock import patch

import pytest

from core.config import Config, ModelConfig, SystemConfig
from core.session import SessionManager

# 从 conftest 导入 DGX 配置工具
from test.conftest import make_dgx_config


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(params=["anthropic", "openai"], ids=["anthropic", "openai"])
def protocol(request):
    """当前测试使用的协议类型（anthropic / openai），参数化两种协议。"""
    return request.param


@pytest.fixture
def dgx_config(protocol):
    """当前协议对应的 DGX Config。"""
    return make_dgx_config(protocol)


@pytest.fixture
def workspace_uuid():
    """测试用 workspace UUID（使用第一个可用 workspace，不存在则 skip）。

    测试完成后清理测试期间创建的 session 目录和 memory 文件。
    """
    from core.config import DATA_DIR
    import shutil
    workspace_dir = DATA_DIR / "workspace"
    if not workspace_dir.exists():
        pytest.skip("No workspace found")

    found_uuid = None
    for item in workspace_dir.iterdir():
        if item.is_dir():
            config_file = item / "setting.json"
            if config_file.exists():
                found_uuid = item.name
                break

    if not found_uuid:
        pytest.skip("No workspace with config found")

    # 记录测试前已有的 session
    sessions_dir = workspace_dir / found_uuid / "sessions"
    existing_sessions = set()
    if sessions_dir.exists():
        existing_sessions = {d.name for d in sessions_dir.iterdir() if d.is_dir()}

    # 记录测试前已有的 memory 文件
    memory_dir = workspace_dir / found_uuid / "memory"
    existing_memory_files = set()
    if memory_dir.exists():
        existing_memory_files = {f for f in memory_dir.rglob("*") if f.is_file()}

    yield found_uuid

    # 清理测试期间创建的 session
    if sessions_dir.exists():
        for d in sessions_dir.iterdir():
            if d.is_dir() and d.name not in existing_sessions:
                shutil.rmtree(d, ignore_errors=True)

    # 清理测试期间创建的 memory 文件
    if memory_dir.exists():
        for f in memory_dir.rglob("*"):
            if f.is_file() and f not in existing_memory_files:
                f.unlink(missing_ok=True)
        # 清理空目录
        for d in sorted(memory_dir.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()  # 只删除空目录
                except OSError:
                    pass


@pytest.fixture
def test_workspace_dir(tmp_path):
    """临时工作目录。"""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "test.txt").write_text("Hello, World!", encoding="utf-8")
    return workspace_dir


# ── RootAgent 集成测试（参数化协议）────────────────────────────────────────────


class TestRootAgentIntegration:
    """RootAgent 集成测试：使用 DGX 端点，覆盖 Anthropic 和 OpenAI 协议。

    每个测试方法会被参数化运行两次：anthropic 协议和 openai 协议。
    """

    def test_basic_conversation(self, dgx_config, workspace_uuid, test_workspace_dir, protocol):
        """基本对话：2+2=4"""
        from core.root_agent import RootAgent

        agent = RootAgent(dgx_config, cwd=str(test_workspace_dir), workspace_uuid=workspace_uuid)
        outputs = []

        agent.run(
            "What is 2 + 2? Reply with just the number.",
            on_text=lambda t: outputs.append(t),
        )

        full_output = "".join(outputs)
        assert "4" in full_output, f"[{protocol}] Expected '4' in output, got: {full_output}"
        assert len(agent.messages) >= 2
        assert agent.messages[0]["role"] == "user"
        assert agent.messages[1]["role"] == "assistant"
        agent.cleanup()

    def test_tool_execution(self, dgx_config, workspace_uuid, test_workspace_dir, protocol):
        """工具调用：执行 bash 命令。"""
        from core.root_agent import RootAgent

        agent = RootAgent(dgx_config, cwd=str(test_workspace_dir), workspace_uuid=workspace_uuid)
        tool_calls = []
        tool_results = []

        agent.run(
            "Run the command 'echo Hello Test' and show me the output.",
            on_text=lambda t: None,
            on_tool_call=lambda name, inp, tid: tool_calls.append((name, inp, tid)),
            on_tool_result=lambda name, out, err, tid: tool_results.append((name, out, err, tid)),
        )

        assert len(tool_calls) > 0, f"[{protocol}] Should have called at least one tool"
        tool_names = [tc[0] for tc in tool_calls]
        assert "bash" in tool_names, f"[{protocol}] Expected 'bash', got: {tool_names}"

        bash_results = [tr for tr in tool_results if tr[0] == "bash"]
        assert len(bash_results) > 0
        assert "Hello Test" in bash_results[0][1], f"[{protocol}] Output: {bash_results[0][1]}"

        session_dir = agent.session_dir
        output_files = list(session_dir.glob("*.txt"))
        assert len(output_files) > 0, f"[{protocol}] Output files should be created"
        agent.cleanup()

    def test_session_persistence(self, dgx_config, workspace_uuid, test_workspace_dir, protocol):
        """会话持久化：跨 agent 实例加载消息。"""
        from core.root_agent import RootAgent

        agent1 = RootAgent(dgx_config, cwd=str(test_workspace_dir), workspace_uuid=workspace_uuid)
        session_id = agent1.current_session_id

        agent1.run(
            "Remember this: The secret word is 'pineapple'.",
            on_text=lambda t: None,
        )
        agent1._sync_to_session_manager()
        agent1.session_manager.save()
        msg_count_1 = len(agent1.messages)
        assert msg_count_1 >= 2
        agent1.cleanup()

        agent2 = RootAgent(dgx_config, cwd=str(test_workspace_dir), workspace_uuid=workspace_uuid)
        agent2.switch_session(session_id)

        assert len(agent2.messages) == msg_count_1, \
            f"[{protocol}] Expected {msg_count_1} messages, got {len(agent2.messages)}"

        user_msg = next(
            (m for m in agent2.messages
             if m["role"] == "user" and isinstance(m["content"], str) and "pineapple" in m["content"]),
            None
        )
        assert user_msg is not None, f"[{protocol}] 'pineapple' message not found"
        agent2.cleanup()

    def test_usage_tracking(self, dgx_config, workspace_uuid, test_workspace_dir, protocol):
        """使用量追踪。"""
        from core.root_agent import RootAgent

        agent = RootAgent(dgx_config, cwd=str(test_workspace_dir), workspace_uuid=workspace_uuid)
        agent.run("Say 'hello'", on_text=lambda t: None)

        usage = agent.get_usage()
        assert usage["api_calls"] >= 1, f"[{protocol}] Should have at least 1 API call"
        assert usage["input_tokens"] > 0
        assert usage["output_tokens"] > 0
        agent.cleanup()

    def test_session_switch(self, dgx_config, workspace_uuid, test_workspace_dir, protocol):
        """会话切换。"""
        from core.root_agent import RootAgent

        agent = RootAgent(dgx_config, cwd=str(test_workspace_dir), workspace_uuid=workspace_uuid)

        session1_id = agent.current_session_id
        agent.run("Session 1 message", on_text=lambda t: None)
        agent._sync_to_session_manager()
        agent.session_manager.save()
        session1_msg_count = len(agent.messages)

        new_session = SessionManager.create_new_session(agent.sessions_dir, "Test Session 2")
        agent.switch_session(new_session.session_id)
        assert len(agent.messages) == 0, f"[{protocol}] New session should be empty"

        agent.run("Session 2 message", on_text=lambda t: None)

        agent.switch_session(session1_id)
        assert len(agent.messages) == session1_msg_count, \
            f"[{protocol}] Expected {session1_msg_count} messages after switch back"
        agent.cleanup()


# ── SubAgent 集成测试（mock load_config 注入 DGX）─────────────────────────────


class TestSubAgentIntegration:
    """SubAgent 集成测试：mock load_config() 注入 DGX 配置。

    SubAgent 内部调用 load_config()，通过 mock 注入 DGX 配置。
    同时覆盖 anthropic / openai 两种协议。
    """

    def _run_subagent(self, task, test_workspace_dir, protocol, max_iterations=5, exec_id=None):
        """创建并运行 SubAgent（mock load_config 注入 DGX 配置）。"""
        from core.sub_agent import SubAgent

        dgx_config = make_dgx_config(protocol)
        session_dir = test_workspace_dir / f"subagent_{secrets.token_hex(4)}"
        session_dir.mkdir(parents=True, exist_ok=True)

        kwargs = {
            "task": task,
            "cwd": str(test_workspace_dir),
            "max_iterations": max_iterations,
            "session_dir": session_dir,
        }
        if exec_id:
            kwargs["exec_id"] = exec_id

        with patch("core.sub_agent.load_config", return_value=dgx_config):
            subagent = SubAgent(**kwargs)
            result = subagent.run()
            subagent.close()

        return result, session_dir

    @pytest.mark.parametrize("protocol", ["anthropic", "openai"])
    def test_basic_execution(self, protocol, test_workspace_dir):
        """SubAgent 基本执行。"""
        result, session_dir = self._run_subagent(
            "Run the command 'echo SubAgent Test' and report the output.",
            test_workspace_dir, protocol,
        )

        assert result["status"] in ("completed", "timeout"), \
            f"[{protocol}] Unexpected status: {result['status']}"
        if result["status"] == "completed":
            assert "summary" in result
            assert "SubAgent Test" in result.get("summary", ""), \
                f"[{protocol}] Expected 'SubAgent Test' in summary"

        messages = _read_subagent_messages(session_dir)
        assert len(messages) > 0, f"[{protocol}] SubAgent should have messages"

    @pytest.mark.parametrize("protocol", ["anthropic", "openai"])
    def test_tool_execution_creates_files(self, protocol, test_workspace_dir):
        """SubAgent 工具调用：输出文件写入。"""
        result, session_dir = self._run_subagent(
            "Use bash to run 'ls -la' in the current directory and report what files you see.",
            test_workspace_dir, protocol,
        )

        assert result["status"] in ("completed", "timeout"), \
            f"[{protocol}] Unexpected status: {result['status']}"
        if result["status"] == "completed":
            output_files = list(session_dir.glob("*.txt"))
            assert len(output_files) > 0, f"[{protocol}] Should have created output files"
            assert any(
                f.read_text(encoding="utf-8", errors="replace").strip()
                for f in output_files
            ), f"[{protocol}] Output files should not all be empty"

    def test_cron_session_persistence(self, test_workspace_dir):
        """Cron 风格的会话持久化（exec_id）。"""
        protocol = "anthropic"
        exec_id = "exec_abc123"
        session_dir = test_workspace_dir / "cron_sessions" / exec_id
        session_dir.mkdir(parents=True, exist_ok=True)

        dgx_config = make_dgx_config(protocol)
        from core.sub_agent import SubAgent
        with patch("core.sub_agent.load_config", return_value=dgx_config):
            subagent = SubAgent(
                task="Run 'echo Cron Test' and report the output.",
                cwd=str(test_workspace_dir),
                max_iterations=3,
                session_dir=session_dir,
                exec_id=exec_id,
            )
            result = subagent.run()
            subagent.close()

        index_file = session_dir / "index.json"
        assert index_file.exists(), f"index.json should exist at {index_file}"

        with open(index_file, "r", encoding="utf-8") as f:
            saved_data = json.load(f)
        assert saved_data.get("session_id") == exec_id
        assert "messages" in saved_data


# ── 辅助函数 ──────────────────────────────────────────────────────────────────


def _read_subagent_messages(session_dir):
    """从 index.json 读取 SubAgent 的消息列表。"""
    index_file = session_dir / "index.json"
    if not index_file.exists():
        return []
    with open(index_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("messages", [])


# ── 单元测试（不依赖 DGX，纯逻辑）─────────────────────────────────────────────


class TestBaseAgentUnitTests:
    """BaseAgent 单元测试：纯逻辑，不调用 LLM。"""

    def test_add_message_with_valid_flag(self):
        """add_message 为 content block 添加 _valid 标记。"""
        from core.base_agent import BaseAgent

        config = make_dgx_config("anthropic")
        agent = BaseAgent(config=config)
        agent.add_message("user", [{"type": "text", "text": "hello"}])

        assert len(agent.messages) == 1
        assert agent.messages[0]["role"] == "user"
        for block in agent.messages[0]["content"]:
            assert "_valid" in block

    def test_get_valid_messages_filters_invalid(self):
        """get_valid_messages 过滤无效 block。"""
        from core.base_agent import BaseAgent

        config = make_dgx_config("anthropic")
        agent = BaseAgent(config=config)
        agent.messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi", "_valid": True}]},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "test", "_valid": False}]},
        ]

        valid = agent.get_valid_messages()
        assert len(valid) == 2  # user message + valid assistant message

    def test_count_tokens(self):
        """_count_tokens 估算。"""
        from core.base_agent import BaseAgent

        config = make_dgx_config("anthropic")
        agent = BaseAgent(config=config)

        assert agent._count_tokens("hello world") > 0
        assert agent._count_tokens("你好世界") > 0
        assert agent._count_tokens("") == 0

    def test_iter_content_blocks(self):
        """iter_content_blocks 产出正确类型。"""
        from core.base_agent import BaseAgent

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "id": "1", "name": "bash", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "1", "content": "output"},
            ]},
        ]

        blocks = list(BaseAgent.iter_content_blocks(messages))
        types = [b[0] for b in blocks]

        assert "text" in types
        assert "tool_use" in types
        assert "tool_result_str" in types
