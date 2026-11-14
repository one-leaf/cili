"""ask_user 工具完整生命周期测试。

测试流程：
1. AskUserTool.execute() 返回 completed=False 的 ToolResult
2. _execute_tool() 生成带 _meta.completed=False 标记的 dict
3. Agent loop 检测到 _meta.completed=False 后退出（content 为占位符文本）
4. 用户提交答案 → 后端设置 _meta.completed=True，注入 tool_result content，标记 _meta.answered=True
5. resume_loop() 继续 agent loop
"""

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from core.config import (
    AgentConfig,
    Config,
    ModelSpec,
    ProviderConfig,
    SystemConfig,
)
from core.llm.types import LLMResponse, TextBlock, ToolCallBlock
from core.session_runner import SessionRunner
from core.tools import get_tool_by_name
from core.tools.base import ToolResult

# ── Fixtures ──

# 使用 conftest.py 中的全局 test_workspace fixture（.test 目录）


@pytest.fixture
def config():
    """构建测试配置"""
    return Config(
        providers={"test": ProviderConfig(
            interface_type="anthropic",
            api_key="test-key",
            base_url="http://localhost:9999",
            models={"test": ModelSpec(name="test-model", max_output_tokens=4096, max_context_tokens=256000)},
        )},
        agents={"master": AgentConfig(model="test/test")},
        system=SystemConfig(),
    )


@pytest.fixture
def runner(config, test_workspace):
    """创建 Master Agent 实例"""
    agent_instance = SessionRunner(config, role="master", cwd=test_workspace, workspace_uuid="test")
    yield agent_instance
    # 清理（Agent 数据写入 {workspace}/.cili/，即 test_workspace 目录）
    ws_dir = Path(test_workspace) / ".cili"
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

    def test_execute_returns_not_completed(self, runner):
        """execute() 返回 ToolResult，completed=False"""
        ask_user_tool = get_tool_by_name(runner.tools, "ask_user")
        assert ask_user_tool is not None, "ask_user 工具必须存在"

        questions = make_test_questions()
        result = ask_user_tool.execute(questions=questions)

        assert isinstance(result, ToolResult)
        assert result.completed is False
        # 不再在 _meta 中重复存储 questions（已在 tool_use input 中）

    def test_execute_output_is_placeholder(self, runner):
        """execute() 的 output 是占位文本"""
        ask_user_tool = get_tool_by_name(runner.tools, "ask_user")
        questions = make_test_questions()
        result = ask_user_tool.execute(questions=questions)

        # output 应该是非空占位文本（用于保存到外部文件）
        assert len(result.output) > 0


# ── 测试 2: _execute_tool() 生成正确的元数据 dict ──

class TestExecuteToolMetadata:
    """_execute_tool() 生成的 dict 结构测试"""

    def test_execute_tool_returns_not_completed_flag(self, runner):
        """_execute_tool() 返回的 dict 包含 _meta.completed=False（新格式）"""
        assert get_tool_by_name(runner.tools, "ask_user") is not None
        questions = make_test_questions()
        tool_use_id = "test_toolu_001"

        result_dict = runner._execute_tool("ask_user", {"questions": questions}, tool_use_id)

        assert result_dict["type"] == "tool_result"
        # 新格式：使用 tool_use_id（Anthropic 格式）
        assert result_dict["tool_use_id"] == tool_use_id
        # 新格式：tool_name 在 _meta 中
        assert result_dict["_meta"]["tool_name"] == "ask_user"
        # 新格式：completed 在 _meta 中，False 表示等待用户
        assert result_dict["_meta"]["completed"] is False
        # 不再在 _meta 中重复存储 questions（已在 tool_use input 中）

    def test_execute_tool_no_external_file(self, runner):
        """_execute_tool() 对 ask_user 不创建外部文件（占位符直接放 content）"""
        tool_use_id = "test_toolu_002"
        questions = make_test_questions()

        result_dict = runner._execute_tool("ask_user", {"questions": questions}, tool_use_id)

        output_path = runner.session_dir / f"{tool_use_id}.txt"
        assert not output_path.exists(), f"ask_user 不应创建外部文件: {output_path}"
        # content 应该直接是占位符文本
        assert result_dict["content"] == "Waiting for user input..."


# ── 测试 3: Agent loop 检测外部等待（completed=False）后退出 ──

class TestAgentLoopWaitForExternal:
    """Agent loop 遇到外部等待（completed=False）时退出测试"""

    def test_loop_exits_on_not_completed(self, runner):
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

        with patch.object(runner, '_call_llm', return_value=mock_response):
            runner.run("测试 ask user 单选工具")

        # 验证 messages 状态
        messages = runner.messages

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

        # tool_result 应有 _meta.completed=False
        tool_result_block = None
        for m in user_result_msgs:
            for b in m["content"]:
                if b.get("type") == "tool_result" and b.get("_meta", {}).get("tool_name") == "ask_user":
                    tool_result_block = b
                    break
        assert tool_result_block is not None, "应找到 ask_user 的 tool_result"
        assert tool_result_block["_meta"]["completed"] is False

        # 关键验证：tool_call 块没有 _meta.answered 标记（用户还没回答）
        for b in last_assistant["content"]:
            if b.get("type") in ("tool_call", "tool_use") and b.get("id") == tool_use_id:
                assert b.get("_meta", {}).get("answered") is not True, "用户未回答前不应有 _meta.answered 标记"

    def test_tool_result_has_placeholder_content(self, runner):
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

        with patch.object(runner, '_call_llm', return_value=mock_response):
            runner.run("测试 ask user 单选工具")

        # 找到 ask_user 的 tool_result
        tool_result_block = None
        for msg in runner.messages:
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
        # _meta.completed 应为 False（等待中）
        assert tool_result_block.get("_meta", {}).get("completed") is False, \
            "等待用户回答时 _meta.completed 应为 False"


# ── 测试 4: 模拟 answer-ask-user 端点逻辑 ──

class TestAnswerAskUserEndpointLogic:
    """模拟 web_api.py 中 answer-ask-user 端点的核心逻辑"""

    def _setup_ask_user_state(self, runner):
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

        with patch.object(runner, '_call_llm', return_value=mock_response):
            runner.run("测试 ask user 单选工具")

        return tool_use_id

    def test_answer_injects_tool_result_content(self, runner):
        """模拟端点：注入 answer 到 tool_result.content"""
        tool_use_id = self._setup_ask_user_state(runner)
        user_answer = "红色"

        # === 模拟 answer-ask-user 端点逻辑 ===
        # Phase A: 找到并替换占位符 tool_result
        found_placeholder = False
        for msg in reversed(runner.messages):
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
        for msg in runner.messages:
            if msg["role"] != "user":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_result" and block.get("tool_call_id") == tool_use_id:
                    assert block["content"] == user_answer

    def test_answer_marks_tool_call_as_answered(self, runner):
        """模拟端点：在 tool_call 块上标记 _meta.answered=True"""
        tool_use_id = self._setup_ask_user_state(runner)
        user_answer = "蓝色"

        # Phase A: 注入 answer
        for msg in reversed(runner.messages):
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

        # Phase B: 标记 tool_call 为 _meta.answered
        found_tool_call = False
        for msg in runner.messages:
            if msg["role"] != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") in ("tool_use", "tool_call") and block.get("id") == tool_use_id:
                    if "_meta" not in block:
                        block["_meta"] = {}
                    block["_meta"]["answered"] = True
                    found_tool_call = True
                    break

        assert found_tool_call, "应找到对应的 tool_call 块"

        # 验证 _meta.answered 标记
        for msg in runner.messages:
            if msg["role"] != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") in ("tool_call", "tool_use") and block.get("id") == tool_use_id:
                    assert block["_meta"]["answered"] is True, "tool_call 应被标记为 _meta.answered"

    def test_wrong_tool_use_id_not_found(self, runner):
        """错误的 tool_use_id 应找不到占位符"""
        self._setup_ask_user_state(runner)

        wrong_id = "nonexistent_tool_id"
        found = False
        for msg in reversed(runner.messages):
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

    def test_resume_continues_loop(self, runner):
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

        with patch.object(runner, '_call_llm', return_value=first_response):
            runner.run("测试 ask user 单选工具")

        # 验证 agent 已退出
        assert not runner.is_running()

        # 注入 answer（模拟端点逻辑）
        for msg in reversed(runner.messages):
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

        # 标记 _meta.answered
        for msg in runner.messages:
            if msg["role"] != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") in ("tool_use", "tool_call") and block.get("id") == tool_use_id:
                    if "_meta" not in block:
                        block["_meta"] = {}
                    block["_meta"]["answered"] = True
                    break

        # 第二次 LLM 调用：返回纯文本回复（不再有 tool_call）
        second_response = LLMResponse(
            content=[TextBlock(text="你选择了红色，很好的选择！")],
            stop_reason="end_turn",
        )

        with patch.object(runner, '_call_llm', return_value=second_response):
            runner.resume_after_ask_user()

        # 验证最终消息包含 LLM 的回复
        text_blocks = []
        for msg in runner.messages:
            if msg["role"] != "assistant":
                continue
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text" and block.get("text"):
                        text_blocks.append(block["text"])

        assert any("红色" in t for t in text_blocks), \
            f"LLM 回复应包含用户选择的内容。实际 text_blocks: {text_blocks}"

    def test_resume_exits_on_no_tool_calls(self, runner):
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

        with patch.object(runner, '_call_llm', return_value=first_response):
            runner.run("测试")

        # 注入 answer
        for msg in reversed(runner.messages):
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

        with patch.object(runner, '_call_llm', return_value=final_response):
            runner.resume_after_ask_user()

        # agent 不再运行
        assert not runner.is_running()

        # 最后一条 assistant 消息应是纯文本
        last_assistant = None
        for msg in reversed(runner.messages):
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


# ── 测试 6: _resolve_tool_results_for_session 标记 _meta.answered ──

class TestResolveToolResultsForSession:
    """web_api.py 中 _resolve_tool_results_for_session 的 _meta.answered 标记逻辑

    预扫描逻辑：当 tool_result 的 _meta.completed == True（用户已提交答案）时，
    才将对应 tool_call 标记为 _meta.answered=True。
    如果 _meta.completed == False（占位符状态，content 是 "Waiting for user input..."），则不标记。
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
                # 新格式：completed == True 表示用户已回答
                if block.get("type") == "tool_result" and block.get("_meta", {}).get("completed") is True:
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
                    if "_meta" not in block:
                        block["_meta"] = {}
                    block["_meta"]["answered"] = True

    def test_pre_scan_marks_answered_when_completed_true(self, runner):
        """预扫描：_meta.completed=True 且有 content → 对应 tool_call 标记 _answered"""
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

        with patch.object(runner, '_call_llm', return_value=mock_response):
            runner.run("测试")

        # 模拟用户已回答：设置 _meta.completed=True + 实际答案 content
        for msg in reversed(runner.messages):
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
                    block["_meta"]["completed"] = True  # 表示用户已回答
                    break

        # 执行预扫描
        answered_ids = self._simulate_pre_scan(runner.messages)
        assert tool_use_id in answered_ids, \
            f"completed=True 时应被收集。收集到: {answered_ids}"

        # 执行标记
        self._simulate_mark_answered(runner.messages, answered_ids)

        # 验证 tool_call 被标记 _meta.answered=True
        for msg in runner.messages:
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content", []):
                if block.get("type") in ("tool_call", "tool_use") and block.get("id") == tool_use_id:
                    assert block["_meta"]["answered"] is True, "已回答的 tool_call 应标记 _meta.answered=True"

    def test_pre_scan_does_not_mark_when_not_completed(self, runner):
        """预扫描：_meta.completed=False（占位符状态）→ 不标记 _answered"""
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

        with patch.object(runner, '_call_llm', return_value=mock_response):
            runner.run("测试")

        # ask_user 执行后 _meta.completed=False，content="Waiting for user input..."
        # 这是占位符状态，不应被标记为已回答

        # 执行预扫描
        answered_ids = self._simulate_pre_scan(runner.messages)
        assert tool_use_id not in answered_ids, \
            f"completed=False（占位符）不应被收集。收集到: {answered_ids}"

        # 验证 tool_call 没有被标记 _meta.answered
        for msg in runner.messages:
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content", []):
                if block.get("type") in ("tool_call", "tool_use") and block.get("id") == tool_use_id:
                    assert not block.get("_meta", {}).get("answered"), \
                        "未回答的 tool_call 不应标记 _meta.answered"

    def test_frontend_will_render_interactive_card_when_unanswered(self, runner):
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

        with patch.object(runner, '_call_llm', return_value=mock_response):
            runner.run("测试")

        # 模拟前端 normalizeContent 处理
        for msg in runner.messages:
            if msg.get("role") != "assistant":
                continue
            for block in msg.get("content", []):
                if block.get("type") in ("tool_call", "tool_use") and block.get("name") == "ask_user":
                    # 前端逻辑：block._meta.answered 为 falsy 时渲染交互式卡片
                    answered = block.get("_meta", {}).get("answered", False)
                    assert not answered, "未回答时 _meta.answered 应为 false/undefined，前端应渲染交互卡片"


# ── 工具契约：参数校验与卡片契约 ──────────────────────────────────────

class TestAskUserContract:
    def test_empty_questions_is_error(self, runner):
        """questions 为空 → error，不产生等待用户的占位符（否则会挂死会话）。"""
        tool = get_tool_by_name(runner.tools, "ask_user")
        result = tool.execute(questions=[])
        assert result.error is True
        assert result.completed is not False

    def test_empty_questions_via_execute_tool(self, runner):
        result = runner._execute_tool("ask_user", {"questions": []}, "tu_empty")
        assert result["is_error"] is True
        assert result["_meta"].get("completed") is not False

    def test_schema_matches_card_contract(self, runner):
        """1-4 题、每题 2-6 选项、多选题标记——前端卡片按这套字段渲染。"""
        params = get_tool_by_name(runner.tools, "ask_user").parameters
        questions = params["properties"]["questions"]
        assert questions["minItems"] == 1 and questions["maxItems"] == 4
        item = questions["items"]
        assert item["required"] == ["question", "header", "options"]
        assert item["properties"]["options"]["minItems"] == 2
        assert item["properties"]["options"]["maxItems"] == 6
        assert item["properties"]["multi_select"]["type"] == "boolean"


# ── 结构化答案：多题 / 多选 / 自定义 / id 与下标 ─────────────────────

def _question_session(tmp_path, questions, tool_use_id="tu_q1"):
    """构造带提问卡占位符的最小 session 替身。"""
    from types import SimpleNamespace

    messages = [
        {"role": "assistant", "content": [{
            "type": "tool_use", "id": tool_use_id, "name": "ask_user",
            "input": {"questions": questions},
        }]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": tool_use_id,
            "content": "Waiting for user input...", "is_error": False,
            "_meta": {"tool_name": "ask_user", "completed": False},
        }]},
    ]
    session_dir = tmp_path / "sessions" / "sess-q"
    session_dir.mkdir(parents=True, exist_ok=True)
    sm = SimpleNamespace(messages=messages, session_dir=session_dir)
    sm.saved = []
    sm.save = lambda: sm.saved.append(True)
    sm.mark_dirty = lambda: None
    return sm, tool_use_id


TWO_QUESTIONS = [
    {"question": "你喜欢的颜色？", "header": "颜色",
     "options": [{"label": "红色", "description": ""}, {"label": "蓝色", "description": ""}]},
    {"question": "接受的方案？", "header": "方案", "multi_select": True,
     "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""},
                 {"label": "C", "description": ""}]},
]


class TestStructuredAnswers:
    def test_multi_question_by_index(self, tmp_path):
        from core.tools.ask_user import format_structured_answers

        sm, tu = _question_session(tmp_path, TWO_QUESTIONS)
        text = format_structured_answers(sm, tu, [
            {"id": "0", "selected": ["红色"], "custom": ""},
            {"id": "1", "selected": ["A", "B"], "custom": "并把 C 记下来"},
        ])
        assert text == "你喜欢的颜色？ 红色\n接受的方案？ A, B, 并把 C 记下来"

    def test_by_question_id(self, tmp_path):
        from core.tools.ask_user import format_structured_answers

        questions = [dict(TWO_QUESTIONS[0], id="q-color"),
                     dict(TWO_QUESTIONS[1], id="q-plan")]
        sm, tu = _question_session(tmp_path, questions)
        text = format_structured_answers(sm, tu, [
            {"id": "q-plan", "selected": ["C"], "custom": ""},
            {"id": "q-color", "selected": [], "custom": "紫色"},
        ])
        assert text == "你喜欢的颜色？ 紫色\n接受的方案？ C"

    def test_missing_answer_keeps_question_line(self, tmp_path):
        from core.tools.ask_user import format_structured_answers

        sm, tu = _question_session(tmp_path, TWO_QUESTIONS)
        text = format_structured_answers(sm, tu, [{"id": "0", "selected": ["蓝色"], "custom": ""}])
        lines = text.split("\n")
        assert lines[0] == "你喜欢的颜色？ 蓝色"
        assert lines[1].startswith("接受的方案？")

    def test_no_questions_falls_back_to_join(self, tmp_path):
        from core.tools.ask_user import format_structured_answers

        sm, tu = _question_session(tmp_path, TWO_QUESTIONS)
        sm.messages[0]["content"] = []  # tool_use 丢失（旧数据/裁剪）
        text = format_structured_answers(sm, tu, [
            {"id": "0", "selected": ["A", "B"], "custom": "X"},
        ])
        assert text == "A, B, X"

    def test_apply_answer_settles_and_writes_file(self, tmp_path):
        from core.tools.ask_user import apply_answer, format_structured_answers

        sm, tu = _question_session(tmp_path, TWO_QUESTIONS)
        answers = [{"id": "0", "selected": ["红色"], "custom": ""},
                   {"id": "1", "selected": ["A"], "custom": ""}]
        assert apply_answer(sm, tu, answers=answers) == "answered"

        expected = format_structured_answers(sm, tu, answers)
        block = sm.messages[1]["content"][0]
        assert block["content"] == expected
        assert block["_meta"]["completed"] is True
        assert block["_meta"]["outcome"] == "answered"
        assert sm.messages[0]["content"][0]["_meta"]["answered"] is True
        path = sm.session_dir / block["_meta"]["output_path"]
        assert path.read_text(encoding="utf-8") == expected

    def test_apply_answer_single_turn_idempotent(self, tmp_path):
        from core.tools.ask_user import apply_answer

        sm, tu = _question_session(tmp_path, TWO_QUESTIONS)
        assert apply_answer(sm, tu, text="红色") == "answered"
        # 重复提交不再改写历史
        assert apply_answer(sm, tu, text="蓝色") != "answered"
        assert "红色" in sm.messages[1]["content"][0]["content"]


# ── 卡片分类：提问卡不能被审批状态劫持 ───────────────────────────────

class TestClassifyPending:
    def test_question_card(self, tmp_path):
        from core.tools.ask_user import classify_pending

        sm, tu = _question_session(tmp_path, TWO_QUESTIONS)
        assert classify_pending(sm) == ("question", tu)

    def test_settled_card_is_none(self, tmp_path):
        from core.tools.ask_user import apply_answer, classify_pending

        sm, tu = _question_session(tmp_path, TWO_QUESTIONS)
        apply_answer(sm, tu, text="红色")
        assert classify_pending(sm) is None

    def test_legacy_approval_by_header(self, tmp_path):
        """升级前的审批卡没有 kind/payload，靠 header「权限确认」识别。"""
        from core.tools.ask_user import classify_pending

        questions = [{"question": "批准该命令？", "header": "权限确认",
                      "options": [{"label": "允许本次会话", "description": ""}]}]
        sm, tu = _question_session(tmp_path, questions)
        assert classify_pending(sm) == ("approval", tu)

    def test_approval_payload_wins(self, tmp_path):
        from core.tools.ask_user import APPROVAL_META_KEY, classify_pending

        questions = [{"question": "批准该命令？", "header": "命令",
                      "options": [{"label": "允许本次会话", "description": ""}]}]
        sm, tu = _question_session(tmp_path, questions)
        sm.messages[1]["content"][0]["_meta"].update({
            "kind": "approval", APPROVAL_META_KEY: {"decision_id": "d1", "command": "ls"}})
        assert classify_pending(sm) == ("approval", tu)

    def test_question_card_with_approval_header_not_hijacked(self, tmp_path):
        """header 恰好叫「权限确认」的普通提问卡，不能只因 header 被当成审批。"""
        from core.tools.ask_user import apply_answer, classify_pending
        from core.tools.permission import PermissionStore

        questions = [{"question": "继续吗？", "header": "权限确认",
                      "options": [{"label": "继续", "description": ""}]}]
        sm, tu = _question_session(tmp_path, questions)
        store = PermissionStore()
        store.set_pending({"decision_id": "stale", "command": "rm -rf /tmp/x"})

        assert classify_pending(sm, store) == ("question", tu)
        assert apply_answer(sm, tu, text="继续", permission_store=store) == "answered"
        block = sm.messages[1]["content"][0]
        assert block["content"] == "继续"
        assert block["_meta"]["outcome"] == "answered"

    def test_question_card_not_hijacked_by_stale_pending(self, tmp_path):
        """回归：单槽 pending 残留时，提问卡不能被判成审批卡。

        曾经的顺序是「header → store.pending → tool_name」，残留 pending 会让
        提问卡走审批分支，用户答案被按「未知文本＝拒绝」处理掉。
        """
        from core.tools.ask_user import classify_pending
        from core.tools.permission import PermissionStore

        sm, tu = _question_session(tmp_path, TWO_QUESTIONS)
        store = PermissionStore()
        store.set_pending({"decision_id": "stale", "command": "rm -rf /tmp/x"})
        assert classify_pending(sm, store) == ("question", tu)

    def test_apply_answer_treats_question_card_as_question(self, tmp_path):
        """结算路径与分类路径一致：残留 pending 不该把提问卡结算成「拒绝」。"""
        from core.tools.ask_user import apply_answer
        from core.tools.permission import PermissionStore

        sm, tu = _question_session(tmp_path, TWO_QUESTIONS)
        store = PermissionStore()
        store.set_pending({"decision_id": "stale", "command": "rm -rf /tmp/x"})

        outcome = apply_answer(
            sm, tu,
            answers=[{"id": "0", "selected": ["红色"], "custom": ""},
                     {"id": "1", "selected": ["A"], "custom": ""}],
            permission_store=store,
        )
        assert outcome == "answered"
        block = sm.messages[1]["content"][0]
        assert block["content"] == "你喜欢的颜色？ 红色\n接受的方案？ A"
        assert block["_meta"]["outcome"] == "answered"
        # 提问卡结算不碰别人的 pending
        assert store.pending is not None


# ── 端到端：答案真的进了模型上下文 ──────────────────────────────────

class TestAnswerReachesModel:
    @staticmethod
    def _ask(runner, questions, tool_use_id="tu_ask_e2e"):
        response = LLMResponse(
            content=[ToolCallBlock(
                id=tool_use_id, name="ask_user",
                arguments=json.dumps({"questions": questions}),
            )],
            stop_reason="tool_use",
        )
        with patch.object(runner, "_call_llm", return_value=response):
            runner.run("问用户一个问题")
        return tool_use_id

    def test_text_answer_is_visible_to_model(self, runner, monkeypatch):
        import asyncio  # noqa: F401 —— 仅为与既有测试同风格保留

        from core.output_sink import OutputSink
        from core.tools.ask_user import find_pending_ask_user
        from core.turn_dispatch import execute_turn, prepare_turn

        tool_use_id = self._ask(runner, make_test_questions())
        assert find_pending_ask_user(runner.session) == tool_use_id

        seen: list[str] = []

        def fake_llm(**_kw):
            msgs = runner._prepare_messages_for_llm()
            seen.append(json.dumps([m.to_dict() for m in msgs], ensure_ascii=False))
            return LLMResponse(content=[TextBlock(text="收到")], stop_reason="end_turn")

        monkeypatch.setattr(runner, "_call_llm", fake_llm)
        prepared = prepare_turn(runner, content="红色")
        assert prepared.mode == "resume"
        execute_turn(prepared, OutputSink(), after_turn_hooks=False)

        assert seen, "回答后应恢复循环并调用 LLM"
        assert "你喜欢的颜色？ 红色" in seen[0]
        assert "Waiting for user input..." not in seen[0], "占位文本不应原样送到模型"

        block = next(b for m in runner.session.messages
                     if m.get("role") == "user" and isinstance(m.get("content"), list)
                     for b in m["content"]
                     if b.get("type") == "tool_result" and b.get("tool_use_id") == tool_use_id)
        assert block["_meta"]["completed"] is True
        assert block["_meta"]["outcome"] == "answered"
        answer_file = runner.session.session_dir / block["_meta"]["output_path"]
        assert answer_file.read_text(encoding="utf-8") == "你喜欢的颜色？ 红色"

    def test_multi_question_other_text_repeats_each_question(self, runner):
        """自由文本回答多个问题时，按卡片约定给每个问题都带上原文。"""
        from core.tools.ask_user import build_other_answer

        tool_use_id = self._ask(runner, TWO_QUESTIONS)
        answer = build_other_answer(runner.session, tool_use_id, "都听你的")
        assert answer == "你喜欢的颜色？ 都听你的\n接受的方案？ 都听你的"
