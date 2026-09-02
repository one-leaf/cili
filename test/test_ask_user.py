"""ask_user 工具完整生命周期测试。

测试流程：
1. AskUserTool.execute() 返回 wait_for_user=True 的 ToolResult
2. _execute_tool() 生成带 _meta.wait_for_user=True 标记的 dict
3. Agent loop 检测到 _meta.wait_for_user 后退出（content 为占位符文本）
4. 用户提交答案 → 后端设置 _meta.wait_for_user=False，注入 tool_result content，标记 _answered=True
5. resume_after_ask_user() 继续 agent loop
"""

import json
import secrets
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.config import Config, ModelConfig, SystemConfig
from core.llm.types import LLMResponse, TextBlock, ToolCallBlock
from core.root_agent import RootAgent
from core.tools.shared.base import ToolResult
from core.tools import get_tool_by_name


# ── Fixtures ──

@pytest.fixture
def test_workspace():
    """创建临时测试工作目录"""
    project_dir = Path(__file__).parent.parent
    test_dir = project_dir / "workspace" / ".test_ask_user_tmp"
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir(parents=True, exist_ok=True)
    yield str(test_dir)
    shutil.rmtree(test_dir, ignore_errors=True)


@pytest.fixture
def config():
    """构建测试配置"""
    return Config(
        model=ModelConfig(
            name="test-model",
            interface_type="anthropic",
            api_key="test-key",
            base_url="http://localhost:9999",
            max_tokens=4096,
            max_context_tokens=256000,
            multimodal=True,
            temperature=0.2,
        ),
        system=SystemConfig(),
    )


@pytest.fixture
def agent(config, test_workspace):
    """创建 RootAgent 实例"""
    test_uuid = secrets.token_hex(4)
    agent_instance = RootAgent(config, cwd=test_workspace, workspace_uuid=test_uuid)
    yield agent_instance
    # 清理
    project_dir = Path(__file__).parent.parent
    ws_dir = project_dir / "data" / "agents" / test_uuid
    if ws_dir.exists():
        shutil.rmtree(ws_dir, ignore_errors=True)


def make_test_questions():
    """构造测试用的 questions 参数"""
    return [
        {
            "question": "你喜欢的颜色？",
            "header": "颜色",
            "options": [
                {"label": "红色", "description": "热情的颜色"},
                {"label": "蓝色", "description": "冷静的颜色"},
            ],
            "multi_select": False,
        }
    ]


# ── 测试 1: AskUserTool.execute() 返回正确的 ToolResult ──

class TestAskUserToolExecution:
    """AskUserTool 工具执行测试"""

    def test_execute_returns_wait_for_user(self, agent):
        """execute() 返回 ToolResult，wait_for_user=True"""
        ask_user_tool = get_tool_by_name(agent.tools, "ask_user")
        assert ask_user_tool is not None, "ask_user 工具必须存在"

        questions = make_test_questions()
        result = ask_user_tool.execute(questions=questions)

        assert isinstance(result, ToolResult)
        assert result.wait_for_user is True
        # 不再在 _meta 中重复存储 questions（已在 tool_use input 中）

    def test_execute_output_is_placeholder(self, agent):
        """execute() 的 output 是占位文本"""
        ask_user_tool = get_tool_by_name(agent.tools, "ask_user")
        questions = make_test_questions()
        result = ask_user_tool.execute(questions=questions)

        # output 应该是非空占位文本（用于保存到外部文件）
        assert len(result.output) > 0


# ── 测试 2: _execute_tool() 生成正确的元数据 dict ──

class TestExecuteToolMetadata:
    """_execute_tool() 生成的 dict 结构测试"""

    def test_execute_tool_returns_wait_for_user_flag(self, agent):
        """_execute_tool() 返回的 dict 包含 _meta.wait_for_user=True（新格式）"""
        ask_user_tool = get_tool_by_name(agent.tools, "ask_user")
        questions = make_test_questions()
        tool_use_id = "test_toolu_001"

        result_dict = agent._execute_tool("ask_user", {"questions": questions}, tool_use_id)

        assert result_dict["type"] == "tool_result"
        # 新格式：使用 tool_use_id（Anthropic 格式）
        assert result_dict["tool_use_id"] == tool_use_id
        # 新格式：tool_name 在 _meta 中
        assert result_dict["_meta"]["tool_name"] == "ask_user"
        # 新格式：wait_for_user 在 _meta 中
        assert result_dict["_meta"]["wait_for_user"] is True
        # 不再在 _meta 中重复存储 questions（已在 tool_use input 中）

    def test_execute_tool_no_external_file(self, agent):
        """_execute_tool() 对 ask_user 不创建外部文件（占位符直接放 content）"""
        tool_use_id = "test_toolu_002"
        questions = make_test_questions()

        result_dict = agent._execute_tool("ask_user", {"questions": questions}, tool_use_id)

        output_path = agent.session_dir / f"{tool_use_id}.txt"
        assert not output_path.exists(), f"ask_user 不应创建外部文件: {output_path}"
        # content 应该直接是占位符文本
        assert result_dict["content"] == "Waiting for user input..."


# ── 测试 3: Agent loop 检测 wait_for_user 后退出 ──

class TestAgentLoopWaitForUser:
    """Agent loop 遇到 wait_for_user 时退出测试"""

    def test_loop_exits_on_wait_for_user(self, agent):
        """LLM 返回 ask_user tool_call 后，agent loop 应退出"""
        tool_use_id = "test_toolu_003"
        questions = make_test_questions()

        # Mock LLM: 第一次调用返回 ask_user tool_call
        mock_response = LLMResponse(
            content=[
                ToolCallBlock(
                    id=tool_use_id,
                    name="ask_user",
                    arguments=json.dumps({"questions": questions}),
                )
            ],
            stop_reason="tool_use",
        )

        with patch.object(agent, '_call_llm', return_value=mock_response):
            agent.run("测试 ask user 单选工具")

        # 验证 messages 状态
        messages = agent.messages

        # 应该有一条 user 消息（用户输入）
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) >= 1, "应至少有 1 条 user 消息（用户输入）"

        # 应该有一条 assistant 消息（包含 tool_call）
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant_msgs) >= 1, "应至少有 1 条 assistant 消息"

        # assistant 消息应包含 ask_user tool_call
        last_assistant = assistant_msgs[-1]
        tool_calls = [
            b for b in last_assistant["content"]
            if b.get("type") in ("tool_call", "tool_use") and b.get("name") == "ask_user"
        ]
        assert len(tool_calls) == 1, "应有一个 ask_user tool_call"
        assert tool_calls[0]["id"] == tool_use_id

        # 应该有 tool_result（agent 执行了工具，然后退出）
        user_result_msgs = [
            m for m in messages
            if m["role"] == "user" and isinstance(m.get("content"), list)
            and any(b.get("type") == "tool_result" for b in m["content"])
        ]
        assert len(user_result_msgs) >= 1, "应有 tool_result 消息"

        # tool_result 应有 _meta.wait_for_user=True
        tool_result_block = None
        for m in user_result_msgs:
            for b in m["content"]:
                if b.get("type") == "tool_result" and b.get("_meta", {}).get("tool_name") == "ask_user":
                    tool_result_block = b
                    break
        assert tool_result_block is not None, "应找到 ask_user 的 tool_result"
        assert tool_result_block["_meta"]["wait_for_user"] is True

        # 关键验证：tool_call 块没有 _answered 标记（用户还没回答）
        for b in last_assistant["content"]:
            if b.get("type") in ("tool_call", "tool_use") and b.get("id") == tool_use_id:
                assert b.get("_answered") is not True, "用户未回答前不应有 _answered 标记"

    def test_tool_result_has_placeholder_content(self, agent):
        """ask_user 的 tool_result 直接包含占位符内容"""
        tool_use_id = "test_toolu_004"
        questions = make_test_questions()

        mock_response = LLMResponse(
            content=[
                ToolCallBlock(
                    id=tool_use_id,
                    name="ask_user",
                    arguments=json.dumps({"questions": questions}),
                )
            ],
            stop_reason="tool_use",
        )

        with patch.object(agent, '_call_llm', return_value=mock_response):
            agent.run("测试 ask user 单选工具")

        # 找到 ask_user 的 tool_result
        tool_result_block = None
        for msg in agent.messages:
            if msg["role"] != "user":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_result" and block.get("_meta", {}).get("tool_name") == "ask_user":
                    tool_result_block = block
                    break

        assert tool_result_block is not None, "应找到 tool_result"
        # content 应该是占位符文本
        assert tool_result_block.get("content") == "Waiting for user input...", \
            "ask_user 的 tool_result 应包含占位符文本"
        # _meta.wait_for_user 应为 True
        assert tool_result_block.get("_meta", {}).get("wait_for_user") is True, \
            "等待用户回答时 _meta.wait_for_user 应为 True"


# ── 测试 4: 模拟 answer-ask-user 端点逻辑 ──

class TestAnswerAskUserEndpointLogic:
    """模拟 web_api.py 中 answer-ask-user 端点的核心逻辑"""

    def _setup_ask_user_state(self, agent):
        """设置 agent 到 ask_user 等待状态，返回 tool_use_id"""
        tool_use_id = "test_toolu_answer_001"
        questions = make_test_questions()

        mock_response = LLMResponse(
            content=[
                ToolCallBlock(
                    id=tool_use_id,
                    name="ask_user",
                    arguments=json.dumps({"questions": questions}),
                )
            ],
            stop_reason="tool_use",
        )

        with patch.object(agent, '_call_llm', return_value=mock_response):
            agent.run("测试 ask user 单选工具")

        return tool_use_id

    def test_answer_injects_tool_result_content(self, agent):
        """模拟端点：注入 answer 到 tool_result.content"""
        tool_use_id = self._setup_ask_user_state(agent)
        user_answer = "红色"

        # === 模拟 answer-ask-user 端点逻辑 ===
        # Phase A: 找到并替换占位符 tool_result
        found_placeholder = False
        for msg in reversed(agent.messages):
            if msg["role"] != "user":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                block_tool_id = block.get("tool_use_id") or block.get("tool_call_id")
                if block.get("type") == "tool_result" and block_tool_id == tool_use_id:
                    block["content"] = user_answer
                    found_placeholder = True
                    break
            if found_placeholder:
                break

        assert found_placeholder, "应找到占位符 tool_result"

        # 验证 content 已注入
        for msg in agent.messages:
            if msg["role"] != "user":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_result" and block.get("tool_call_id") == tool_use_id:
                    assert block["content"] == user_answer

    def test_answer_marks_tool_call_as_answered(self, agent):
        """模拟端点：在 tool_call 块上标记 _answered=True"""
        tool_use_id = self._setup_ask_user_state(agent)
        user_answer = "蓝色"

        # Phase A: 注入 answer
        for msg in reversed(agent.messages):
            if msg["role"] != "user":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                block_tool_id = block.get("tool_use_id") or block.get("tool_call_id")
                if block.get("type") == "tool_result" and block_tool_id == tool_use_id:
                    block["content"] = user_answer
                    break

        # Phase B: 标记 tool_call 为 _answered
        found_tool_call = False
        for msg in agent.messages:
            if msg["role"] != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") in ("tool_use", "tool_call") and block.get("id") == tool_use_id:
                    block["_answered"] = True
                    found_tool_call = True
                    break

        assert found_tool_call, "应找到对应的 tool_call 块"

        # 验证 _answered 标记
        for msg in agent.messages:
            if msg["role"] != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") in ("tool_call", "tool_use") and block.get("id") == tool_use_id:
                    assert block["_answered"] is True, "tool_call 应被标记为 _answered"

    def test_wrong_tool_use_id_not_found(self, agent):
        """错误的 tool_use_id 应找不到占位符"""
        self._setup_ask_user_state(agent)

        wrong_id = "nonexistent_tool_id"
        found = False
        for msg in reversed(agent.messages):
            if msg["role"] != "user":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                block_tool_id = block.get("tool_use_id") or block.get("tool_call_id")
                if block.get("type") == "tool_result" and block_tool_id == wrong_id:
                    found = True
                    break

        assert not found, "错误的 tool_use_id 不应匹配到任何 tool_result"


# ── 测试 5: resume_after_ask_user 继续 agent loop ──

class TestResumeAfterAskUser:
    """resume_after_ask_user() 继续 agent loop 测试"""

    def test_resume_continues_loop(self, agent):
        """注入 answer 后，resume 继续 loop，LLM 收到 tool_result 后给出最终回复"""
        tool_use_id = "test_toolu_resume_001"
        questions = make_test_questions()

        # 第一次 LLM 调用：返回 ask_user tool_call
        first_response = LLMResponse(
            content=[
                ToolCallBlock(
                    id=tool_use_id,
                    name="ask_user",
                    arguments=json.dumps({"questions": questions}),
                )
            ],
            stop_reason="tool_use",
        )

        with patch.object(agent, '_call_llm', return_value=first_response):
            agent.run("测试 ask user 单选工具")

        # 验证 agent 已退出
        assert not agent.is_running()

        # 注入 answer（模拟端点逻辑）
        for msg in reversed(agent.messages):
            if msg["role"] != "user":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                block_tool_id = block.get("tool_use_id") or block.get("tool_call_id")
                if block.get("type") == "tool_result" and block_tool_id == tool_use_id:
                    block["content"] = "红色"
                    break

        # 标记 _answered
        for msg in agent.messages:
            if msg["role"] != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") in ("tool_use", "tool_call") and block.get("id") == tool_use_id:
                    block["_answered"] = True
                    break

        # 第二次 LLM 调用：返回纯文本回复（不再有 tool_call）
        second_response = LLMResponse(
            content=[TextBlock(text="你选择了红色，很好的选择！")],
            stop_reason="end_turn",
        )

        with patch.object(agent, '_call_llm', return_value=second_response):
            agent.resume_after_ask_user()

        # 验证最终消息包含 LLM 的回复
        text_blocks = []
        for msg in agent.messages:
            if msg["role"] != "assistant":
                continue
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text" and block.get("text"):
                        text_blocks.append(block["text"])

        assert any("红色" in t for t in text_blocks), \
            f"LLM 回复应包含用户选择的内容。实际 text_blocks: {text_blocks}"

    def test_resume_exits_on_no_tool_calls(self, agent):
        """resume 后 LLM 不再调用工具 → loop 正常退出"""
        tool_use_id = "test_toolu_resume_002"
        questions = make_test_questions()

        first_response = LLMResponse(
            content=[
                ToolCallBlock(
                    id=tool_use_id,
                    name="ask_user",
                    arguments=json.dumps({"questions": questions}),
                )
            ],
            stop_reason="tool_use",
        )

        with patch.object(agent, '_call_llm', return_value=first_response):
            agent.run("测试")

        # 注入 answer
        for msg in reversed(agent.messages):
            if msg["role"] != "user":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                block_tool_id = block.get("tool_use_id") or block.get("tool_call_id")
                if block.get("type") == "tool_result" and block_tool_id == tool_use_id:
                    block["content"] = "蓝色"
                    break

        # resume：LLM 只返回文本
        final_response = LLMResponse(
            content=[TextBlock(text="收到")],
            stop_reason="end_turn",
        )

        with patch.object(agent, '_call_llm', return_value=final_response):
            agent.resume_after_ask_user()

        # agent 不再运行
        assert not agent.is_running()

        # 最后一条 assistant 消息应是纯文本
        last_assistant = None
        for msg in reversed(agent.messages):
            if msg["role"] == "assistant":
                last_assistant = msg
                break

        assert last_assistant is not None
        has_tool_calls = any(
            b.get("type") in ("tool_call", "tool_use")
            for b in last_assistant["content"]
            if isinstance(b, dict)
        )
        assert not has_tool_calls, "最后一条 assistant 消息不应有 tool_call"


# ── 测试 6: _resolve_tool_results_for_session 标记 _answered ──

class TestResolveToolResultsForSession:
    """web_api.py 中 _resolve_tool_results_for_session 的 _answered 标记逻辑

    预扫描逻辑：当 tool_result 的 _meta.wait_for_user == False（用户已提交答案）时，
    才将对应 tool_call 标记为 _answered=True。
    如果 _meta.wait_for_user == True（占位符状态，content 是 "Waiting for user input..."），则不标记。
    """

    def _simulate_pre_scan(self, messages):
        """模拟 _resolve_tool_results_for_session 的预扫描逻辑（新格式版本）"""
        answered_ids = set()
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            for block in content:
                # 新格式：wait_for_user == False 表示用户已回答
                if block.get("type") == "tool_result" and block.get("_meta", {}).get("wait_for_user") is False:
                    tool_id = block.get("tool_use_id") or block.get("tool_call_id")
                    if tool_id:
                        answered_ids.add(tool_id)
        return answered_ids

    def _simulate_mark_answered(self, messages, answered_ids):
        """模拟 _resolve_tool_results_for_session 的第二遍标记"""
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") in ("tool_use", "tool_call") and block.get("id") in answered_ids:
                    block["_answered"] = True

    def test_pre_scan_marks_answered_when_wait_for_user_false(self, agent):
        """预扫描：_meta.wait_for_user=False 且有 content → 对应 tool_call 标记 _answered"""
        tool_use_id = "test_toolu_prescan_001"
        questions = make_test_questions()

        # 模拟 ask_user 执行后的消息状态
        mock_response = LLMResponse(
            content=[
                ToolCallBlock(
                    id=tool_use_id,
                    name="ask_user",
                    arguments=json.dumps({"questions": questions}),
                )
            ],
            stop_reason="tool_use",
        )

        with patch.object(agent, '_call_llm', return_value=mock_response):
            agent.run("测试")

        # 模拟用户已回答：设置 _meta.wait_for_user=False + 实际答案 content
        for msg in reversed(agent.messages):
            if msg["role"] != "user":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                block_tool_id = block.get("tool_use_id") or block.get("tool_call_id")
                if block.get("type") == "tool_result" and block_tool_id == tool_use_id:
                    block["content"] = "红色"  # 注入实际答案
                    if "_meta" not in block:
                        block["_meta"] = {}
                    block["_meta"]["wait_for_user"] = False  # 表示用户已回答
                    break

        # 执行预扫描
        answered_ids = self._simulate_pre_scan(agent.messages)
        assert tool_use_id in answered_ids, \
            f"wait_for_user=False 时应被收集。收集到: {answered_ids}"

        # 执行标记
        self._simulate_mark_answered(agent.messages, answered_ids)

        # 验证 tool_call 被标记 _answered=True
        for msg in agent.messages:
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content", []):
                if block.get("type") in ("tool_call", "tool_use") and block.get("id") == tool_use_id:
                    assert block["_answered"] is True, "已回答的 tool_call 应标记 _answered=True"

    def test_pre_scan_does_not_mark_when_wait_for_user_true(self, agent):
        """预扫描：_meta.wait_for_user=True（占位符状态）→ 不标记 _answered"""
        tool_use_id = "test_toolu_prescan_002"
        questions = make_test_questions()

        mock_response = LLMResponse(
            content=[
                ToolCallBlock(
                    id=tool_use_id,
                    name="ask_user",
                    arguments=json.dumps({"questions": questions}),
                )
            ],
            stop_reason="tool_use",
        )

        with patch.object(agent, '_call_llm', return_value=mock_response):
            agent.run("测试")

        # ask_user 执行后 _meta.wait_for_user=True，content="Waiting for user input..."
        # 这是占位符状态，不应被标记为已回答

        # 执行预扫描
        answered_ids = self._simulate_pre_scan(agent.messages)
        assert tool_use_id not in answered_ids, \
            f"wait_for_user=True（占位符）不应被收集。收集到: {answered_ids}"

        # 验证 tool_call 没有被标记 _answered
        for msg in agent.messages:
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content", []):
                if block.get("type") in ("tool_call", "tool_use") and block.get("id") == tool_use_id:
                    assert not block.get("_answered"), \
                        "未回答的 tool_call 不应标记 _answered"

    def test_frontend_will_render_interactive_card_when_unanswered(self, agent):
        """前端逻辑：未回答时（_answered 非 true），应渲染交互式问题卡片"""
        tool_use_id = "test_toolu_prescan_003"
        questions = make_test_questions()

        mock_response = LLMResponse(
            content=[
                ToolCallBlock(
                    id=tool_use_id,
                    name="ask_user",
                    arguments=json.dumps({"questions": questions}),
                )
            ],
            stop_reason="tool_use",
        )

        with patch.object(agent, '_call_llm', return_value=mock_response):
            agent.run("测试")

        # 模拟前端 normalizeContent 处理
        for msg in agent.messages:
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content", []):
                if block.get("type") in ("tool_call", "tool_use") and block.get("name") == "ask_user":
                    # 前端逻辑：block._answered 为 falsy 时渲染交互式卡片
                    # block._answered || false → 如果 block 没有 _answered 字段，返回 false
                    answered = block.get("_answered", False)
                    assert not answered, "未回答时 _answered 应为 false/undefined，前端应渲染交互卡片"
