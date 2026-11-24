"""合并升级演练：在一份「旧格式」数据快照上跑完整迁移链。

覆盖旧数据的全部已知形态：
- setting.json：provider 键 `chatgpt-siwc`、协议键 `interface_type`、凭证前缀 `siwc:`
- workspaces.json + 会话目录：metadata 通道 `tui`、旧文件 URL、block 级 `completed`
断言：一次 run_pending 全部改写完成、schema_version 记录齐全、配置备份存在、二次运行无变化。
"""

from __future__ import annotations

import json

from core.migrations import load_completed, run_pending
from core.migrations.steps import (
    migrate_chatgpt_oauth_naming,
    migrate_legacy_file_urls,
    migrate_placeholder_meta,
    migrate_provider_interface_type,
    migrate_tui_channel,
)


def _build_old_data(tmp_path):
    data_dir = tmp_path / "cili"
    data_dir.mkdir()
    setting = data_dir / "setting.json"
    setting.write_text(json.dumps({
        "providers": {
            "chatgpt-siwc": {
                "interface_type": "openai-responses",
                "credential": "siwc:acc1",
                "models": {"default": {}},
            },
        },
        "agents": {"master": {"model": "chatgpt-siwc/default"}},
    }), encoding="utf-8")

    workspace_dir = tmp_path / "ws"
    session_dir = workspace_dir / ".cili" / "sessions" / "abc12345"
    session_dir.mkdir(parents=True)
    (session_dir / "meta.json").write_text(json.dumps({
        "session_id": "abc12345",
        "metadata": {"channel": "tui", "channel_account": "", "channel_chat": ""},
    }), encoding="utf-8")
    (session_dir / "messages.jsonl").write_text(json.dumps({
        "role": "assistant",
        "content": "见 [图](/api/workspace/files/uploads/a.png)",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    (session_dir / "index.json").write_text(json.dumps({
        "schema_version": 2,
        "next_seq": 2,
        "commits": [{"seq": 1, "msg_meta": {}, "blocks": {
            "tu1": {"completed": False, "tool_name": "ask_user"},
        }}],
    }), encoding="utf-8")

    workspaces = data_dir / "workspaces.json"
    workspaces.write_text(json.dumps([{
        "uuid": "w1", "workspace_name": "w1", "directory": str(workspace_dir),
    }]), encoding="utf-8")
    return data_dir, setting, session_dir


def test_full_migration_chain_on_old_snapshot(tmp_path):
    data_dir, setting, session_dir = _build_old_data(tmp_path)

    def data_dir_for(uuid: str):
        return tmp_path / "ws" / ".cili"

    steps = [
        ("provider_protocol_key", lambda: migrate_provider_interface_type(setting)),
        ("chatgpt_oauth_naming", lambda: migrate_chatgpt_oauth_naming(setting)),
        ("session_placeholder_meta",
         lambda: migrate_placeholder_meta(data_dir / "workspaces.json", data_dir_for)),
        ("tui_channel_to_cli",
         lambda: migrate_tui_channel(data_dir / "workspaces.json", data_dir_for)),
        ("legacy_file_urls",
         lambda: migrate_legacy_file_urls(data_dir / "workspaces.json", data_dir_for)),
    ]
    executed = run_pending(steps, data_dir)
    assert executed == [sid for sid, _ in steps]
    assert load_completed(data_dir) == [sid for sid, _ in steps]

    # 配置：键/协议/凭证前缀全部迁移
    raw = json.loads(setting.read_text(encoding="utf-8"))
    provider = raw["providers"]["chatgpt-oauth"]
    assert provider["protocol"] == "openai-responses"
    assert provider["credential"] == "chatgpt-oauth:acc1"
    assert raw["agents"]["master"]["model"] == "chatgpt-oauth/default"
    assert "chatgpt-siwc" not in raw["providers"]

    # 会话：channels/meta/URL/占位标记
    meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["metadata"]["channel"] == "cli"
    text = (session_dir / "messages.jsonl").read_text(encoding="utf-8")
    assert "/api/workspaces/w1/files/uploads/a.png" in text
    index = json.loads((session_dir / "index.json").read_text(encoding="utf-8"))
    block_meta = index["commits"][0]["blocks"]["tu1"]
    assert block_meta["ask_user_completed"] is False

    # 备份存在，且二次运行无步骤可执行
    assert list((data_dir / "backup" / "migrations").iterdir())
    assert run_pending(steps, data_dir) == []
