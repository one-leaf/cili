"""Tests for core/agent.py — Lite（最小 autonomous 角色）构造与行为。

Lite 由 core/agents/lite.json 定义：只读 read/write/edit/bash 四工具、
无检查阶段、无预算预警、streaming。验证配置驱动行为分叉与流式中断路径。
"""

import threading
import time
from unittest.mock import MagicMock, patch

from core.agent import Agent


def _make_mock_config(max_iterations=200):
    config = MagicMock()
    config.model = MagicMock()
    config.system.max_iterations = max_iterations
    return config


def _make_agent(config=None, tools=None, **kwargs):
    """构造 Lite Agent，mock 掉工具实例化与 LLM client。"""
    if config is None:
        config = _make_mock_config()
    with patch("core.agent.create_tools") as mock_tools, \
         patch("core.agent.create_llm_client") as mock_client:
        mock_tools.return_value = tools if tools is not None else []
        mock_client.return_value = MagicMock()
        return Agent(config, role="lite", **kwargs)


def _make_tool_call_response(call_id="toolu_1", name="read"):
    tc = MagicMock()
    tc.name = name
    tc.id = call_id
    tc.parse_arguments.return_value = {"file_path": "a.txt"}
    resp = MagicMock()
    resp.get_tool_calls.return_value = [tc]
    resp.content_as_dicts.return_value = [
        {"type": "tool_use", "id": call_id, "name": name, "input": {"file_path": "a.txt"}}
    ]
    return resp


def _make_text_response(text):
    resp = MagicMock()
    resp.get_tool_calls.return_value = []
    resp.get_text.return_value = text
    resp.content_as_dicts.return_value = [{"type": "text", "text": text}]
    return resp


def _make_tool_result():
    return {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}


class TestLiteConstruction:
    """Lite 角色构造：白名单/行为开关从 JSON 加载。"""

    def test_construction(self):
        agent = _make_agent(task="read file")
        assert agent.role == "lite"
        assert agent._mode == "autonomous"
        assert agent.task == "read file"
        # lite.json 显式 max_iterations=200，支持批量循环，不继承 config 默认
        assert agent.max_iterations == 200
        assert agent.max_consecutive_failures == 5  # 缺省
        assert agent.role_cfg.check_phase is False
        assert agent.role_cfg.budget_notice is False
        assert agent.role_cfg.streaming is True

    def test_tool_whitelist(self):
        """Lite 白名单含 read/write/edit/bash/python。"""
        agent = _make_agent(task="t")
        assert agent.role_cfg.tools == ["read", "write", "edit", "bash", "python"]

    def test_tools_instantiated(self):
        tools = [MagicMock() for _ in range(4)]
        agent = _make_agent(task="t", tools=tools)
        assert len(agent.tools) == 4

    def test_system_prompt_no_skills_section(self):
        """Lite 无 skills 块：system prompt 不含技能列表，只含固定 role 文案。"""
        agent = _make_agent(task="t")
        assert "## Available Skills" not in agent._system_prompt
        assert "autonomous task-execution agent" in agent._system_prompt

    def test_pinned_task_message(self):
        """任务消息首条 pinned 且含目标描述。"""
        agent = _make_agent(task="读取 a.txt 并追加一行")
        section = agent._build_task_message()
        assert "读取 a.txt 并追加一行" in section


class TestLiteRunFlow:
    """Lite autonomous 执行流：无检查阶段、直接交付。"""

    def _make_agent(self):
        return _make_agent(task="read file")

    def test_delivers_directly_no_check_phase(self):
        """工具调用后模型输出文本 → 直接 completed，无检查阶段与预算消息。"""
        agent = self._make_agent()
        responses = [_make_tool_call_response(), _make_text_response("已完成")]
        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_execute_tool", return_value=_make_tool_result()), \
             patch.object(agent, "_call_llm", side_effect=responses):
            result = agent.run()

        assert result["status"] == "completed"
        assert result["summary"] == "已完成"
        assert result["iterations"] == 2
        assert "check_iterations" not in result
        assert not any(m.get("_meta", {}).get("budget") for m in agent.messages)
        assert not any(
            isinstance(m.get("content"), str) and "检查阶段" in m["content"]
            for m in agent.messages
        )

    def test_stop_check_immediate(self):
        """stop_check 恒真 → 循环顶部直接返回 stopped。"""
        agent = _make_agent(task="t", stop_check=lambda: True)
        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_call_llm") as mock_call:
            result = agent.run()

        assert result["status"] == "stopped"
        mock_call.assert_not_called()

    def test_stopped_after_llm_call(self):
        """流式中断后检查：LLM 返回时 _stopped 已置位 → stopped（不误判完成）。"""
        agent = self._make_agent()

        def interrupt(*args, **kwargs):
            agent._stopped = True
            return _make_text_response("done")

        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_call_llm", side_effect=interrupt):
            result = agent.run()

        assert result["status"] == "stopped"
        assert result["iterations"] == 0

    def test_stop_from_background_thread(self):
        """后台线程 run + agent.stop() → 返回 stopped。"""
        agent = self._make_agent()
        release = threading.Event()
        results: list[dict] = []

        def slow_llm(*args, **kwargs):
            release.wait(timeout=5)
            return _make_text_response("done")

        with patch.object(agent, "_check_and_compress"), \
             patch.object(agent, "_call_llm", side_effect=slow_llm):
            thread = threading.Thread(target=lambda: results.append(agent.run()))
            thread.start()
            time.sleep(0.2)
            agent.stop()
            release.set()
            thread.join(timeout=5)

        assert not thread.is_alive()
        assert len(results) == 1
        assert results[0]["status"] == "stopped"

    def test_no_tools_uses_real_tool_whitelist(self):
        """无 mock 工具时由注册表实例化（仅 read/write/edit/bash 白名单内）。"""
        # 注册表按 lite 白名单实例化，需 config 提供 approval 相关字段
        from core.config import Config
        from types import SimpleNamespace
        config = MagicMock()
        config.model = MagicMock()
        config.system.max_iterations = 50
        config.workspaces = []
        config.system.approval = None
        with patch("core.agent.create_llm_client", return_value=MagicMock()):
            agent = Agent(config, role="lite", task="t", cwd=".")
        names = {t.name for t in agent.tools}
        assert names == {"read", "write", "edit", "bash", "python"}
