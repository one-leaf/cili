"""Integration tests for BaseAgent architecture.

使用 DGX 本地端点测试，同时覆盖 Anthropic 和 OpenAI 两种协议。
DGX 端点：http://192.168.3.3:8080（本地推理，不消耗 API 配额）。
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def _run_subagent(self, task, test_workspace_dir, protocol, exec_id=None):
        """创建并运行 SubAgent（mock load_config 注入 DGX 配置）。"""
        from core.sub_agent import SubAgent

        dgx_config = make_dgx_config(protocol)
        session_dir = test_workspace_dir / f"subagent_{secrets.token_hex(4)}"
        session_dir.mkdir(parents=True, exist_ok=True)

        kwargs = {
            "task": task,
            "cwd": str(test_workspace_dir),
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
        """SubAgent 工具调用：输出内联到消息，小输出的外部文件应被清理。"""
        result, session_dir = self._run_subagent(
            "Use bash to run 'ls -la' in the current directory and report what files you see.",
            test_workspace_dir, protocol,
        )

        assert result["status"] in ("completed", "timeout"), \
            f"[{protocol}] Unexpected status: {result['status']}"
        if result["status"] == "completed":
            # Small outputs are inlined in messages; external .txt files should
            # be cleaned up (no orphaned files).
            output_files = list(session_dir.glob("call_*.txt"))
            assert len(output_files) == 0, \
                f"[{protocol}] Small output files should be cleaned up, found: {output_files}"
            # Content is still persisted in messages
            import json as _json
            idx_file = session_dir / "index.json"
            if idx_file.exists():
                data = _json.loads(idx_file.read_text(encoding="utf-8"))
                messages = data.get("messages", [])
                tool_results = [
                    block for msg in messages if msg.get("role") == "user"
                    for block in (msg.get("content") if isinstance(msg.get("content"), list) else [])
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                ]
                assert any(
                    tr.get("content") for tr in tool_results
                ), f"[{protocol}] Tool results should be inlined in messages"

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
        """add_message 不再添加 block 级别的 _valid（新格式使用 message 级别的 _meta.valid）。"""
        from core.base_agent import BaseAgent

        config = make_dgx_config("anthropic")
        agent = BaseAgent(config=config)
        agent.add_message("user", [{"type": "text", "text": "hello"}])

        assert len(agent.messages) == 1
        assert agent.messages[0]["role"] == "user"
        # 新格式：不再自动添加 block 级别的 _valid
        # 消息级别的 _meta.valid 由其他逻辑设置

    def test_get_valid_messages_filters_invalid(self):
        """get_valid_messages 过滤无效消息（使用新格式 _meta.valid）。"""
        from core.base_agent import BaseAgent

        config = make_dgx_config("anthropic")
        agent = BaseAgent(config=config)
        agent.messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            # 使用新格式：消息级别的 _meta.valid = False
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "test"}], "_meta": {"valid": False}},
        ]

        valid = agent.get_valid_messages()
        assert len(valid) == 2  # user message + valid assistant message

    def test_count_tokens(self):
        """_count_messages_tokens 委托 compression.count_messages_tokens（含 reasoning 块）。"""
        from core.base_agent import BaseAgent

        config = make_dgx_config("anthropic")
        agent = BaseAgent(config=config)

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "hi"},
                {"type": "reasoning", "text": "思考内容"},  # reasoning 块也应计入
            ]},
        ]

        assert agent._count_messages_tokens(messages) > 0
        assert agent._count_messages_tokens([]) == 0

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

    def test_full_compact_failure_does_not_invalidate(self, tmp_path):
        """摘要生成失败时旧消息不应被标记无效（防止历史永久丢失）。"""
        from core.base_agent import BaseAgent

        config = make_dgx_config("anthropic")
        agent = BaseAgent(config=config, session_dir=tmp_path)
        agent.messages = [
            {"role": "user", "content": "question 1"},
            {"role": "assistant", "content": "answer 1"},
            {"role": "user", "content": "question 2"},
            {"role": "assistant", "content": "answer 2"},
            {"role": "user", "content": "recent question"},
        ]

        with patch.object(agent, "_summarize_messages", return_value="（摘要生成失败，请查看完整历史）"):
            before, after = agent._perform_full_compact(keep_user_messages=1)

        # 摘要失败后所有消息仍有效
        assert all(m.get("_meta", {}).get("valid") is not False for m in agent.messages)
        assert len(agent.get_valid_messages()) == len(agent.messages)

    def test_full_compact_success_invalidates_old(self, tmp_path):
        """摘要成功后旧消息才被标记无效。"""
        from core.base_agent import BaseAgent

        config = make_dgx_config("anthropic")
        agent = BaseAgent(config=config, session_dir=tmp_path)
        agent.messages = [
            {"role": "user", "content": "question 1"},
            {"role": "assistant", "content": "answer 1"},
            {"role": "user", "content": "question 2"},
            {"role": "assistant", "content": "answer 2"},
            {"role": "user", "content": "recent question"},
        ]

        with patch.object(agent, "_summarize_messages", return_value="之前的对话摘要内容"):
            agent._perform_full_compact(keep_user_messages=1)

        # 摘要成功：旧消息被标记无效，摘要消息已插入
        valid = agent.get_valid_messages()
        assert len(valid) < len(agent.messages)
        assert any("摘要" in (m.get("content", "") if isinstance(m.get("content"), str) else "")
                   for m in agent.messages)

    def test_save_messages_preserves_metadata(self, tmp_path):
        """save_messages 不应覆盖 SessionManager 写入的 name/metadata。"""
        from core.base_agent import BaseAgent

        config = make_dgx_config("anthropic")
        session_dir = tmp_path / "sess"
        agent = BaseAgent(config=config, session_dir=session_dir)
        agent._session_id = "sess123"

        # 预写一个带 name/metadata 的会话文件（模拟 SessionManager.save()）
        session_dir.mkdir(parents=True, exist_ok=True)
        existing = {
            "session_id": "sess123",
            "name": "我的会话",
            "messages": [],
            "metadata": {
                "usage": {"input_tokens": 100, "output_tokens": 50, "api_calls": 2},
                "subagent_count": 3,
            },
        }
        (session_dir / "index.json").write_text(
            json.dumps(existing, ensure_ascii=False), encoding="utf-8"
        )

        agent.messages = [{"role": "user", "content": "hi"}]
        agent.save_messages()

        data = json.loads((session_dir / "index.json").read_text(encoding="utf-8"))
        assert data["name"] == "我的会话"
        assert data["metadata"]["usage"]["input_tokens"] == 100
        assert data["metadata"]["subagent_count"] == 3
        assert data["messages"] == agent.messages

    def test_mark_all_images_invalid_preserves_pairing(self):
        """图片失效改为占位符替换，tool_use/tool_result 配对不被破坏。"""
        from core.base_agent import BaseAgent

        config = make_dgx_config("anthropic")
        agent = BaseAgent(config=config)
        agent.messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "a"}},
            ]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "t1", "content": [
                    {"type": "text", "text": "screenshot"},
                    {"type": "image", "source": {"media_type": "image/png", "data": "A" * 100}},
                ],
            }]},
        ]

        agent._mark_all_images_invalid()

        # 消息仍有效（不被整条过滤），图片被替换为文本占位符
        assert len(agent.get_valid_messages()) == 2
        subs = agent.messages[1]["content"][0]["content"]
        assert [s["type"] for s in subs] == ["text", "text"]

    def test_mark_old_images_invalid_replaces_not_invalidates(self):
        """_mark_old_images_invalid 替换旧图片，保留消息有效性。"""
        from core.base_agent import BaseAgent

        config = make_dgx_config("anthropic")
        agent = BaseAgent(config=config)
        agent.messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
            ]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "t1", "content": [
                    {"type": "image", "source": {"data": "A" * 200}},
                ],
            }]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t2", "name": "read", "input": {}},
            ]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "t2", "content": [
                    {"type": "image", "source": {"data": "B" * 100}},
                ],
            }]},
        ]

        saved = agent._mark_old_images_invalid(keep_recent=1)

        assert saved == 200  # 只替换最早一条的图片
        assert len(agent.get_valid_messages()) == 4  # 全部消息仍有效
        first_subs = agent.messages[1]["content"][0]["content"]
        assert first_subs[0]["type"] == "text"  # 已替换为占位符
        last_subs = agent.messages[3]["content"][0]["content"]
        assert last_subs[0]["type"] == "image"  # 最近一条保留
