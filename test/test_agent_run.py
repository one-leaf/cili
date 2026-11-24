"""Tests for AgentRun records, continuation requests and fork support."""

import json
from unittest.mock import MagicMock, patch

from core.agent_run import AgentRunManager, AgentRunRecord, AgentRunStore
from core.agent_stats import AgentStats
from core.session_runner import SessionRunner
from test.support import make_dgx_config


def _worker(tmp_path, *, epoch=1, max_epochs=3, role="worker", depth=1):
    config = make_dgx_config("anthropic")
    with patch("core.session_runner.create_llm_client", return_value=MagicMock()), \
         patch("core.session_runner.create_tools", return_value=[]):
        runner = SessionRunner(
            config,
            role=role,
            task="task",
            workspace_uuid="test-workspace",
            session_dir=tmp_path / "exec_x",
            exec_id="exec_x",
            master_session_id="root",
            delegation_depth=depth,
            epoch=epoch,
            max_epochs=max_epochs,
        )
    runner.add_message("user", "task")
    return runner


def test_agent_run_store_roundtrip(tmp_path):
    store = AgentRunStore(tmp_path)
    rec = AgentRunRecord(
        exec_id="exec_x", root_session_id="root", role="worker",
        task="t", max_epochs=3,
    )
    store.create(rec)
    loaded = store.get("exec_x")
    assert loaded is not None
    assert loaded.status == "created"

    store.archive_epoch(
        "exec_x", epoch=1, status="completed", exit_reason="completed",
        truncated=False, max_iterations=200, summary="done",
        stats=AgentStats(iterations=2, message_count=1, tool_call_count=0),
        messages=[{"role": "assistant", "content": "ok"}],
    )
    loaded = store.get("exec_x")
    assert loaded is not None
    assert loaded.status == "completed"
    assert len(loaded.epochs) == 1
    assert loaded.epochs[0].epoch == 1
    assert loaded.epochs[0].stats.iterations == 2
    assert loaded.stats.iterations == 2
    assert (tmp_path / "exec_x" / "epochs" / "1.json").exists()
    data = json.loads((tmp_path / "exec_x" / "epochs" / "1.json").read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["stats"]["iterations"] == 2
    assert data["ended_at"]


def test_agent_run_manager_delivery(tmp_path):
    mgr = AgentRunManager(tmp_path)
    mgr.create(
        exec_id="exec_y", root_session_id="root", role="worker",
        task="t", max_epochs=1,
    )
    mgr.start("exec_y", epoch=1)
    mgr.complete(
        "exec_y", status="completed", exit_reason="completed",
        truncated=False, summary="ok",
    )
    assert mgr.get("exec_y").delivery_state == "pending"
    mgr.delivered("exec_y")
    assert mgr.get("exec_y").delivery_state == "delivered"


def test_worker_budget_exhaustion_builds_continuation(tmp_path):
    runner = _worker(tmp_path)
    runner.runner.summarize_for_handoff = MagicMock(return_value="HANDOFF DOC")
    result = runner.build_continuation_request(200)
    assert result is not None
    assert result["status"] == "handoff"
    assert result["epoch"] == 1
    assert result["max_epochs"] == 3
    assert result["truncated"] is True
    assert result["continuation"]["document"] == "HANDOFF DOC"
    assert result["continuation"]["epoch"] == 1


def test_continuation_respects_epoch_limit(tmp_path):
    runner = _worker(tmp_path, epoch=3, max_epochs=3)
    runner.runner.summarize_for_handoff = MagicMock(return_value="HANDOFF DOC")
    assert runner.build_continuation_request(200) is None


def test_continuation_only_for_worker(tmp_path):
    runner = _worker(tmp_path, role="lite", max_epochs=3)
    runner.runner.summarize_for_handoff = MagicMock(return_value="HANDOFF DOC")
    assert runner.build_continuation_request(200) is None


def test_fork_session_snapshot(tmp_path):
    from core.session import SessionStore

    config = make_dgx_config("anthropic")
    store = SessionStore.create_new_session(tmp_path, "source")
    with patch("core.session_runner.create_llm_client", return_value=MagicMock()),          patch("core.session_runner.create_tools", return_value=[]):
        runner = SessionRunner(
            config, role="master", cwd=str(tmp_path),
            workspace_uuid="test-workspace", initial_session=store,
        )
    runner.add_message("user", "hello")
    runner.add_message("assistant", "world")
    runner.session.save(force=True)  # assign seq, reproducing real session copy
    result = runner.fork_session(label="branch")
    assert result["status"] == "idle"
    assert result["mode"] == "snapshot"
    new_id = result["session_id"]
    loaded = SessionStore.load_session(new_id, runner.sessions_dir)
    assert loaded is not None
    assert [m["role"] for m in loaded.messages] == ["user", "assistant"]
    assert (loaded.session_dir / "messages.jsonl").exists()
    assert len((loaded.session_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_fork_empty_next_steps_is_plain_copy_even_over_token_cap(tmp_path):
    """空下一阶段任务只复制会话，不生成 handoff 文档、不自动执行。"""
    from core.session import SessionStore

    config = make_dgx_config("anthropic")
    store = SessionStore.create_new_session(tmp_path, "source")
    with patch("core.session_runner.create_llm_client", return_value=MagicMock()),          patch("core.session_runner.create_tools", return_value=[]):
        runner = SessionRunner(
            config, role="master", cwd=str(tmp_path),
            workspace_uuid="test-workspace", initial_session=store,
        )
    runner.add_message("user", "hello")
    runner.add_message("assistant", "world")
    runner._count_messages_tokens = MagicMock(return_value=1_000_000)
    result = runner.fork_session(next_steps="", label="branch")
    assert result["status"] == "idle"
    assert result["mode"] == "snapshot"
    loaded = SessionStore.load_session(result["session_id"], runner.sessions_dir)
    assert loaded is not None
    assert [m["role"] for m in loaded.messages] == ["user", "assistant"]
    assert (loaded.session_dir / "messages.jsonl").exists()
    assert len((loaded.session_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_fork_session_inherits_model_ref(tmp_path):
    """fork 的新会话继承父会话的模型覆盖（元数据随 fork 带走）。"""
    from core.session import SessionStore

    config = make_dgx_config("anthropic")
    store = SessionStore.create_new_session(tmp_path, "source")
    store.set_model_ref("dgx/dgx")
    with patch("core.session_runner.create_llm_client", return_value=MagicMock()), \
         patch("core.session_runner.create_tools", return_value=[]):
        runner = SessionRunner(
            config, role="master", cwd=str(tmp_path),
            workspace_uuid="test-workspace", initial_session=store,
        )
    runner.add_message("user", "hello")
    runner.session.save(force=True)
    result = runner.fork_session(label="branch")
    loaded = SessionStore.load_session(result["session_id"], runner.sessions_dir)
    assert loaded is not None
    assert loaded.metadata.get("model_ref") == "dgx/dgx"


def test_fork_without_override_does_not_pin_default_model(tmp_path):
    """无会话级覆盖的会话 fork 后不应写入 model_ref：否则会被钉死在当时的
    默认模型上，之后全局改默认模型跟随不到。"""
    from core.session import SessionStore

    config = make_dgx_config("anthropic")
    store = SessionStore.create_new_session(tmp_path, "source")
    with patch("core.session_runner.create_llm_client", return_value=MagicMock()), \
         patch("core.session_runner.create_tools", return_value=[]):
        runner = SessionRunner(
            config, role="master", cwd=str(tmp_path),
            workspace_uuid="test-workspace", initial_session=store,
        )
    runner.add_message("user", "hello")
    runner.session.save(force=True)
    result = runner.fork_session(label="branch")
    loaded = SessionStore.load_session(result["session_id"], runner.sessions_dir)
    assert loaded is not None
    assert "model_ref" not in loaded.metadata


def test_worker_inherits_parent_model_ref(tmp_path):
    """子代理继承父 runner 当前模型引用（输入框切换的会话级覆盖可传导到 agent 工具）。"""
    from core.config import ModelSpec

    config = make_dgx_config("anthropic")
    config.providers["dgx"].models["alt"] = ModelSpec(
        name="alt-model", max_output_tokens=4096, max_context_tokens=256000)
    with patch("core.session_runner.create_llm_client", return_value=MagicMock()), \
         patch("core.session_runner.create_tools", return_value=[]):
        runner = SessionRunner(
            config, role="worker", task="task",
            workspace_uuid="test-workspace", session_dir=tmp_path / "exec_x",
            exec_id="exec_x", master_session_id="root",
            parent_model_ref="dgx/alt",
        )
    assert runner.model_ref == "dgx/alt"
    assert runner.model.name == "alt-model"


def test_worker_invalid_parent_model_ref_falls_back_to_role_default(tmp_path):
    """父模型引用不可解析时回退角色默认模型（dgx 配置下 worker 继承 master）。"""
    with patch("core.session_runner.create_llm_client", return_value=MagicMock()), \
         patch("core.session_runner.create_tools", return_value=[]):
        runner = SessionRunner(
            make_dgx_config("anthropic"), role="worker", task="task",
            workspace_uuid="test-workspace", session_dir=tmp_path / "exec_x",
            exec_id="exec_x", master_session_id="root",
            parent_model_ref="dgx/ghost",
        )
    assert runner.model_ref == "dgx/dgx"


def test_agent_tool_inherit_parent_model_ref(tmp_path):
    """角色未显式配置模型 → 继承父引用；显式配置 → 不被会话级切换覆盖。"""
    from core.config import AgentConfig
    from core.tools.agent import AgentTool

    config = make_dgx_config("anthropic")
    tool = AgentTool(cwd=str(tmp_path), config=config)

    # worker 未配置：跟随父 runner 当前引用
    tool.parent_model_ref = lambda: "dgx/dgx"
    assert tool._inherit_parent_model_ref("worker") == "dgx/dgx"

    # 父无引用（如独立构造的工具）：用角色默认
    tool.parent_model_ref = lambda: None
    assert tool._inherit_parent_model_ref("worker") is None

    # worker 显式配置了模型：不被父引用覆盖
    config.agents["worker"] = AgentConfig(model="dgx/dgx")
    tool.parent_model_ref = lambda: "dgx/dgx"
    assert tool._inherit_parent_model_ref("worker") is None


def test_fork_copies_external_tool_outputs(tmp_path):
    """fork 复制 tool_result._meta.output_path 引用的外部工具输出文件。

    不复制时 fork 侧会把图片/大文本工具结果读成「工具正在执行中...」占位，
    既丢内容也破坏与父会话一致的 prompt 前缀。
    """
    from core.session import SessionStore

    config = make_dgx_config("anthropic")
    store = SessionStore.create_new_session(tmp_path, "source")
    with patch("core.session_runner.create_llm_client", return_value=MagicMock()), \
         patch("core.session_runner.create_tools", return_value=[]):
        runner = SessionRunner(
            config, role="master", cwd=str(tmp_path),
            workspace_uuid="test-workspace", initial_session=store,
        )
    payload = {"type": "multimodal",
               "blocks": [{"type": "text", "text": "[Image: x]"}]}
    (store.session_dir / "call_img1.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    runner.add_message("user", "看图")
    runner.add_message("assistant", [
        {"type": "tool_use", "id": "call_img1", "name": "read_image", "input": {}},
    ])
    runner.add_message("user", [{
        "type": "tool_result", "tool_use_id": "call_img1",
        "content": None, "is_error": False,
        "_meta": {"output_path": "call_img1.json"},
    }])
    runner.session.save(force=True)

    result = runner.fork_session(label="branch")
    fork_dir = runner.sessions_dir / result["session_id"]
    copied = fork_dir / "call_img1.json"
    assert copied.is_file()
    assert json.loads(copied.read_text(encoding="utf-8")) == payload


def test_fork_skips_tool_output_outside_session(tmp_path):
    """output_path 指向会话目录外时不复制（防路径穿越）。"""
    from core.session_runner import _copy_referenced_tool_outputs

    parent = tmp_path / "sessions" / "p"
    fork = tmp_path / "sessions" / "f"
    parent.mkdir(parents=True)
    fork.mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    messages = [{
        "role": "user",
        "content": [{
            "type": "tool_result", "tool_use_id": "t", "content": None,
            "_meta": {"output_path": "../secret.txt"},
        }],
    }]
    assert _copy_referenced_tool_outputs(parent, fork, messages) == 0
    assert list(fork.iterdir()) == []
