"""core/migrations 测试：版本文件幂等、步骤失败中止、占位标记重写。"""

from __future__ import annotations

import json

from core.migrations import load_completed, run_pending
from core.migrations.steps import migrate_placeholder_meta


def test_run_pending_records_and_is_idempotent(tmp_path):
    calls: list[str] = []
    steps = [
        ("a", lambda: calls.append("a")),
        ("b", lambda: calls.append("b")),
    ]
    assert run_pending(steps, tmp_path) == ["a", "b"]
    assert calls == ["a", "b"]
    # 第二次：全部已记录，不重复执行
    assert run_pending(steps, tmp_path) == []
    assert calls == ["a", "b"]
    assert load_completed(tmp_path) == ["a", "b"]


def test_run_pending_stops_on_failure(tmp_path):
    def boom():
        raise RuntimeError("fail")

    steps = [("ok", lambda: None), ("bad", boom), ("never", lambda: None)]
    try:
        run_pending(steps, tmp_path)
    except RuntimeError:
        pass
    else:  # pragma: no cover - 必须抛
        raise AssertionError("step 失败应上抛")
    # 失败步骤及其后续都不记入版本文件
    assert load_completed(tmp_path) == ["ok"]


def test_migrate_placeholder_meta_rewrites_session(tmp_path):
    workspace = tmp_path / "ws"
    session_dir = workspace / "sessions" / "abc12345"
    session_dir.mkdir(parents=True)
    records = [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "t1", "content": "Waiting...",
            "_meta": {"completed": False, "tool_name": "ask_user"},
        }]},
    ]
    jsonl = session_dir / "messages.jsonl"
    jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    # block 级 _meta 的真实落点：index.json commits[].blocks
    index = session_dir / "index.json"
    index.write_text(json.dumps({
        "schema_version": 2, "next_seq": 3,
        "commits": [
            {"seq": 1, "msg_meta": {}, "blocks": {}},
            {"seq": 2, "msg_meta": {}, "blocks": {
                "t1": {"completed": False, "tool_name": "ask_user"},
            }},
        ],
    }), encoding="utf-8")
    workspaces_json = tmp_path / "workspaces.json"
    workspaces_json.write_text(json.dumps([{"uuid": "w1"}]), encoding="utf-8")

    changed = migrate_placeholder_meta(workspaces_json, lambda uuid: workspace)
    assert changed == 2  # messages.jsonl + index.json 各重写一次
    lines = [json.loads(x) for x in jsonl.read_text(encoding="utf-8").splitlines()]
    meta = lines[1]["content"][0]["_meta"]
    assert meta["ask_user_completed"] is False
    assert meta["completed"] is False  # 旧键保留，双读兼容

    index_data = json.loads(index.read_text(encoding="utf-8"))
    block_meta = index_data["commits"][1]["blocks"]["t1"]
    assert block_meta["ask_user_completed"] is False
    assert block_meta["completed"] is False

    # 幂等：再次执行不再改写
    assert migrate_placeholder_meta(workspaces_json, lambda uuid: workspace) == 0


def test_migrate_provider_interface_type(tmp_path):
    from core.migrations.steps import migrate_provider_interface_type

    path = tmp_path / "setting.json"
    path.write_text(json.dumps({
        "providers": {
            "p1": {"interface_type": "openai", "models": {}},
            "p2": {"protocol": "anthropic", "models": {}},
        },
    }), encoding="utf-8")

    assert migrate_provider_interface_type(path) is True
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["providers"]["p1"]["protocol"] == "openai"
    assert "interface_type" not in raw["providers"]["p1"]
    assert raw["providers"]["p2"]["protocol"] == "anthropic"
    # 幂等
    assert migrate_provider_interface_type(path) is False


def test_migrate_chatgpt_oauth_naming(tmp_path):
    from core.migrations.steps import migrate_chatgpt_oauth_naming

    path = tmp_path / "setting.json"
    path.write_text(json.dumps({
        "providers": {
            "chatgpt-siwc": {
                "backend": "chatgpt-siwc",
                "credential": "siwc:abc123",
                "models": {},
            },
        },
        "agents": {"master": {"model": "chatgpt-siwc/default"}},
    }), encoding="utf-8")

    assert migrate_chatgpt_oauth_naming(path) is True
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "chatgpt-siwc" not in raw["providers"]
    entry = raw["providers"]["chatgpt-oauth"]
    assert entry["backend"] == "chatgpt-oauth"
    assert entry["credential"] == "chatgpt-oauth:abc123"
    assert raw["agents"]["master"]["model"] == "chatgpt-oauth/default"
    assert migrate_chatgpt_oauth_naming(path) is False


def test_migrate_chatgpt_oauth_merges_models_when_target_exists(tmp_path):
    from core.migrations.steps import migrate_chatgpt_oauth_naming

    path = tmp_path / "setting.json"
    path.write_text(json.dumps({
        "providers": {
            "chatgpt-siwc": {"credential": "siwc:a", "models": {"old": {}}},
            "chatgpt-oauth": {"credential": "chatgpt-oauth:b", "models": {"new": {}}},
        },
    }), encoding="utf-8")

    assert migrate_chatgpt_oauth_naming(path) is True
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "chatgpt-siwc" not in raw["providers"]
    target = raw["providers"]["chatgpt-oauth"]
    # 已有的新配置优先；旧模型并入而不是静默丢失
    assert target["credential"] == "chatgpt-oauth:b"
    assert set(target["models"]) == {"old", "new"}


def test_migrate_tui_channel(tmp_path):
    from core.migrations.steps import migrate_tui_channel

    workspace = tmp_path / "ws"
    session_dir = workspace / "sessions" / "abc12345"
    session_dir.mkdir(parents=True)
    (session_dir / "meta.json").write_text(json.dumps({
        "session_id": "abc12345",
        "metadata": {"channel": "tui", "channel_account": "", "channel_chat": ""},
    }), encoding="utf-8")
    workspaces_json = tmp_path / "workspaces.json"
    workspaces_json.write_text(json.dumps([{"uuid": "w1"}]), encoding="utf-8")

    assert migrate_tui_channel(workspaces_json, lambda uuid: workspace) == 1
    meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["metadata"]["channel"] == "cli"
    assert migrate_tui_channel(workspaces_json, lambda uuid: workspace) == 0


def test_migrate_chatgpt_oauth_handles_null_models(tmp_path):
    from core.migrations.steps import migrate_chatgpt_oauth_naming

    path = tmp_path / "setting.json"
    path.write_text(json.dumps({
        "providers": {
            "chatgpt-siwc": {"models": {"old": {}}},
            "chatgpt-oauth": {"models": None},
        },
    }), encoding="utf-8")

    assert migrate_chatgpt_oauth_naming(path) is True
    target = json.loads(path.read_text(encoding="utf-8"))["providers"]["chatgpt-oauth"]
    assert target["models"] == {"old": {}}
