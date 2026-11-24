"""Integration tests for BaseSessionRunner architecture.

使用 DGX 本地端点测试，同时覆盖 Anthropic 和 OpenAI 两种协议。
DGX 端点：http://192.168.3.3:8080（本地推理，不消耗 API 配额）。
"""

from __future__ import annotations

import json
import secrets

import pytest

from core.output_sink import OutputSink
from core.session import SessionStore

# 从 conftest 导入 DGX 配置工具
from test.support import make_dgx_config

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(params=["anthropic", "openai"], ids=["anthropic", "openai"])
def protocol(request):
    """当前测试使用的协议类型（anthropic / openai），参数化两种协议。"""
    return request.param


@pytest.fixture
def dgx_config(protocol, dgx_available):
    """当前协议对应的 DGX Config。

    依赖 dgx_available：服务器不可达时使用本 fixture 的测试整组跳过。
    """
    return make_dgx_config(protocol)


@pytest.fixture
def workspace_uuid(tmp_path):
    """测试用独立临时工作区 UUID。

    每个测试函数创建全新工作区并整体清理，避免：① 污染真实工作区；
    ② 集成测试的 Agent 会加载「最新会话」，若复用真实工作区则每次运行
    都追加消息、越滚越大（曾导致 test_tool_execution 因 289 条历史而
    LLM 不再可靠调用工具）。
    """
    import secrets
    import shutil

    from core.config import get_workspace_data_dir, upsert_workspace_entry
    test_uuid = secrets.token_hex(4)
    ws_dir = tmp_path / test_uuid
    ws_dir.mkdir(parents=True, exist_ok=True)
    upsert_workspace_entry({
        "uuid": test_uuid,
        "workspace_name": "Integration Test",
        "directory": str(ws_dir),
    })
    yield test_uuid
    # 整体删除测试工作区（含 sessions/memory），避免残留累积
    shutil.rmtree(get_workspace_data_dir(test_uuid), ignore_errors=True)
    shutil.rmtree(ws_dir, ignore_errors=True)


@pytest.fixture
def test_workspace_dir(tmp_path):
    """临时工作目录。"""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "test.txt").write_text("Hello, World!", encoding="utf-8")
    return workspace_dir


# ── Master Agent 集成测试（参数化协议）─────────────────────────────────────────


@pytest.mark.integration
class TestMasterAgentIntegration:
    """Master Agent 集成测试：使用 DGX 端点，覆盖 Anthropic 和 OpenAI 协议。

    每个测试方法会被参数化运行两次：anthropic 协议和 openai 协议。
    dgx_config fixture 依赖 dgx_available，服务器不可达时跳过。
    """

    def test_basic_conversation(self, dgx_config, workspace_uuid, test_workspace_dir, protocol):
        """基本对话：2+2=4"""
        from core.session_runner import SessionRunner

        runner = SessionRunner(dgx_config, role="master", cwd=str(test_workspace_dir), workspace_uuid=workspace_uuid)
        outputs = []

        runner.run(
            "What is 2 + 2? Reply with just the number.",
            sink=OutputSink(on_text=lambda t: outputs.append(t)),
        )

        full_output = "".join(outputs)
        assert "4" in full_output, f"[{protocol}] Expected '4' in output, got: {full_output}"
        assert len(runner.messages) >= 2
        assert runner.messages[0]["role"] == "user"
        assert runner.messages[1]["role"] == "assistant"
        runner.cleanup()

    def test_tool_execution(self, dgx_config, workspace_uuid, test_workspace_dir, protocol):
        """工具调用：执行 bash 命令。"""
        from core.session_runner import SessionRunner

        runner = SessionRunner(dgx_config, role="master", cwd=str(test_workspace_dir), workspace_uuid=workspace_uuid)
        tool_calls = []
        tool_results = []

        runner.run(
            "Run the command 'echo Hello Test' and show me the output.",
            sink=OutputSink(
                on_text=lambda t: None,
                on_tool_call=lambda name, inp, tid: tool_calls.append((name, inp, tid)),
                on_tool_result=lambda name, out, err, tid: tool_results.append((name, out, err, tid)),
            ),
        )

        assert len(tool_calls) > 0, f"[{protocol}] Should have called at least one tool"
        tool_names = [tc[0] for tc in tool_calls]
        assert "bash" in tool_names, f"[{protocol}] Expected 'bash', got: {tool_names}"

        bash_results = [tr for tr in tool_results if tr[0] == "bash"]
        assert len(bash_results) > 0
        assert "Hello Test" in bash_results[0][1], f"[{protocol}] Output: {bash_results[0][1]}"

        # 小输出内联到消息，不残留外部 .txt（见 test_tool_execution_creates_files）
        runner.cleanup()

    def test_session_persistence(self, dgx_config, workspace_uuid, test_workspace_dir, protocol):
        """会话持久化：跨 agent 实例加载消息。"""
        from core.session_runner import SessionRunner

        agent1 = SessionRunner(dgx_config, role="master", cwd=str(test_workspace_dir), workspace_uuid=workspace_uuid)
        session_id = agent1.current_session_id

        agent1.run(
            "Remember this: The secret word is 'pineapple'.",
            sink=OutputSink(on_text=lambda t: None),
        )
        agent1._sync_to_session()
        agent1.session.save()
        msg_count_1 = len(agent1.messages)
        assert msg_count_1 >= 2
        agent1.cleanup()

        agent2 = SessionRunner(dgx_config, role="master", cwd=str(test_workspace_dir), workspace_uuid=workspace_uuid)
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
        from core.session_runner import SessionRunner

        runner = SessionRunner(dgx_config, role="master", cwd=str(test_workspace_dir), workspace_uuid=workspace_uuid)
        runner.run("Say 'hello'", sink=OutputSink(on_text=lambda t: None))

        usage = runner.get_usage()
        assert usage["api_calls"] >= 1, f"[{protocol}] Should have at least 1 API call"
        assert usage["input_tokens"] > 0
        assert usage["output_tokens"] > 0
        runner.cleanup()

    def test_session_switch(self, dgx_config, workspace_uuid, test_workspace_dir, protocol):
        """会话切换。"""
        from core.session_runner import SessionRunner

        runner = SessionRunner(dgx_config, role="master", cwd=str(test_workspace_dir), workspace_uuid=workspace_uuid)

        session1_id = runner.current_session_id
        runner.run("Session 1 message", sink=OutputSink(on_text=lambda t: None))
        runner._sync_to_session()
        runner.session.save()
        session1_msg_count = len(runner.messages)

        new_session = SessionStore.create_new_session(runner.sessions_dir, "Test Session 2")
        runner.switch_session(new_session.session_id)
        assert len(runner.messages) == 0, f"[{protocol}] New session should be empty"

        runner.run("Session 2 message", sink=OutputSink(on_text=lambda t: None))

        runner.switch_session(session1_id)
        assert len(runner.messages) == session1_msg_count, \
            f"[{protocol}] Expected {session1_msg_count} messages after switch back"
        runner.cleanup()


# ── Agent 集成测试（mock load_config 注入 DGX）─────────────────────────────


@pytest.mark.integration
class TestAgentIntegration:
    """Agent 集成测试：mock load_config() 注入 DGX 配置。

    Agent 内部调用 load_config()，通过 mock 注入 DGX 配置。
    同时覆盖 anthropic / openai 两种协议。
    服务器不可达时整类跳过（dgx_available）。
    """

    @pytest.fixture(autouse=True)
    def _require_dgx(self, dgx_available):
        """本类所有测试均发起真实 DGX 调用；不可达即跳过。"""
        yield

    def _run_agent(self, task, test_workspace_dir, protocol, exec_id=None):
        """创建并运行 Worker Agent（DGX 配置直传）。"""
        from core.session_runner import SessionRunner

        dgx_config = make_dgx_config(protocol)
        session_dir = test_workspace_dir / f"agent_{secrets.token_hex(4)}"
        session_dir.mkdir(parents=True, exist_ok=True)

        kwargs = {
            "task": task,
            "cwd": str(test_workspace_dir),
            "session_dir": session_dir,
        }
        if exec_id:
            kwargs["exec_id"] = exec_id

        runner = SessionRunner(dgx_config, role="worker", **kwargs)
        result = runner.run()
        runner.close()

        return result, session_dir

    @pytest.mark.parametrize("protocol", ["anthropic", "openai"])
    def test_basic_execution(self, protocol, test_workspace_dir):
        """Agent 基本执行。"""
        result, session_dir = self._run_agent(
            "Run the command 'echo Agent Test' and report the output.",
            test_workspace_dir, protocol,
        )

        assert result["status"] in ("completed", "timeout"), \
            f"[{protocol}] Unexpected status: {result['status']}"
        if result["status"] == "completed":
            assert "summary" in result
            assert "Agent Test" in result.get("summary", ""), \
                f"[{protocol}] Expected 'Agent Test' in summary"

        messages = _read_agent_messages(session_dir)
        assert len(messages) > 0, f"[{protocol}] Agent should have messages"

    @pytest.mark.parametrize("protocol", ["anthropic", "openai"])
    def test_tool_execution_creates_files(self, protocol, test_workspace_dir):
        """Agent 工具调用：输出内联到消息，小输出的外部文件应被清理。"""
        result, session_dir = self._run_agent(
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
        from core.session_runner import SessionRunner
        runner = SessionRunner(
            dgx_config,
            role="worker",
            task="Run 'echo Cron Test' and report the output.",
            cwd=str(test_workspace_dir),
            session_dir=session_dir,
            exec_id=exec_id,
        )
        runner.run()
        runner.close()

        index_file = session_dir / "index.json"
        assert index_file.exists(), f"index.json should exist at {index_file}"

        with open(index_file, "r", encoding="utf-8") as f:
            saved_data = json.load(f)
        assert saved_data.get("session_id") == exec_id
        assert "messages" in saved_data


# ── 辅助函数 ──────────────────────────────────────────────────────────────────


def _read_agent_messages(session_dir):
    """从 index.json 读取 Agent 的消息列表。"""
    index_file = session_dir / "index.json"
    if not index_file.exists():
        return []
    with open(index_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("messages", [])


# ── 单元测试（不依赖 DGX，纯逻辑）─────────────────────────────────────────────
