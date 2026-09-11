"""Tests for core/migration.py — 会话格式迁移。

重点验证迁移的字段层级约定：
- 消息级 _meta.valid（整条消息有效性）
- block 级 _meta.{compacted, output_path, file_size, truncated, tool_name, completed}
  （工具结果存储字段，运行时 _resolve_tool_results 只读 block 级）
"""

import json

import pytest

from core.migration import migrate_message, migrate_session_file, migrate_todos_from_metadata


def _tool_result_block(tool_use_id: str = "tool_1") -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": "",
    }


class TestMigrateMessageFieldLevel:
    """存储字段必须迁到 block 级 _meta，valid 迁到消息级。"""

    def test_output_path_goes_to_block_meta(self):
        """_output_path/_file_size/_truncated/tool_name 迁到 block 级 _meta。"""
        msg = {
            "role": "user",
            "content": [
                {
                    **_tool_result_block(),
                    "_output_path": "tool_1.txt",
                    "_file_size": 1234,
                    "_truncated": True,
                    "tool_name": "bash",
                }
            ],
        }
        assert migrate_message(msg) is True
        block = msg["content"][0]
        assert block["_meta"]["output_path"] == "tool_1.txt"
        assert block["_meta"]["file_size"] == 1234
        assert block["_meta"]["truncated"] is True
        assert block["_meta"]["tool_name"] == "bash"
        # 旧字段已移除，且消息级 _meta 不持有这些存储字段
        assert "_output_path" not in block
        assert "output_path" not in msg.get("_meta", {})

    def test_compacted_goes_to_block_meta(self):
        msg = {
            "role": "user",
            "content": [
                {**_tool_result_block(), "_compacted": True},
            ],
        }
        migrate_message(msg)
        block = msg["content"][0]
        assert block["_meta"]["compacted"] is True

    def test_block_valid_false_goes_to_message_meta(self):
        """block _valid=False → 消息级 _meta.valid=False（get_valid_messages 只读消息级）。"""
        msg = {
            "role": "user",
            "content": [
                {**_tool_result_block(), "_valid": False},
            ],
        }
        assert migrate_message(msg) is True
        assert msg["_meta"]["valid"] is False

    def test_block_valid_true_not_stored(self):
        """block _valid=True 无需存储（默认为有效）。"""
        msg = {
            "role": "user",
            "content": [
                {**_tool_result_block(), "_valid": True},
            ],
        }
        assert migrate_message(msg) is True
        assert "valid" not in msg.get("_meta", {})

    def test_string_content_valid(self):
        msg = {"role": "user", "content": "hello", "_valid": False}
        assert migrate_message(msg) is True
        assert msg["_meta"]["valid"] is False

    def test_multiple_blocks_each_own_meta(self):
        """多个 tool_result block 各自持有自己的输出路径。"""
        msg = {
            "role": "user",
            "content": [
                {**_tool_result_block("a"), "_output_path": "a.txt"},
                {**_tool_result_block("b"), "_output_path": "b.txt"},
            ],
        }
        migrate_message(msg)
        assert msg["content"][0]["_meta"]["output_path"] == "a.txt"
        assert msg["content"][1]["_meta"]["output_path"] == "b.txt"

    def test_wait_for_user_goes_to_block_completed(self):
        """wait_for_user → block 级 _meta.completed（反转语义）。"""
        msg = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "ask_1",
                    "content": "",
                    "_meta": {"wait_for_user": True},
                }
            ],
        }
        assert migrate_message(msg) is True
        block = msg["content"][0]
        assert block["_meta"]["completed"] is False  # wait_for_user=True → 等待中
        assert "wait_for_user" not in block["_meta"]

    def test_tool_call_id_to_tool_use_id(self):
        msg = {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_call_id": "t1", "content": "x"},
            ],
        }
        migrate_message(msg)
        block = msg["content"][0]
        assert block["tool_use_id"] == "t1"
        assert "tool_call_id" not in block

    def test_tool_call_to_tool_use_arguments_to_input(self):
        msg = {
            "role": "assistant",
            "content": [
                {"type": "tool_call", "name": "bash", "arguments": '{"command": "ls"}'},
            ],
        }
        migrate_message(msg)
        block = msg["content"][0]
        assert block["type"] == "tool_use"
        assert block["input"] == {"command": "ls"}

    def test_reasoning_to_thinking(self):
        msg = {
            "role": "assistant",
            "content": [
                {"type": "reasoning", "text": "思考过程"},
            ],
        }
        migrate_message(msg)
        block = msg["content"][0]
        assert block["type"] == "thinking"
        assert block["thinking"] == "思考过程"

    def test_sub_block_valid_false_sets_message_valid(self):
        """tool_result 子块 _valid=False（坏图片）不丢弃，置消息级 valid=False。"""
        msg = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "img_1",
                    "content": [{"type": "image", "source": {"data": "xxx"}, "_valid": False}],
                }
            ],
        }
        assert migrate_message(msg) is True
        assert msg["_meta"]["valid"] is False

    def test_no_old_format_returns_false(self):
        """已经是新格式的消息返回 False，不修改。"""
        msg = {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "x",
                 "_meta": {"output_path": "t1.txt"}},
            ],
        }
        assert migrate_message(msg) is False


class TestMigrateSessionFile:
    """migrate_session_file 整体流程 + 原子写。"""

    def test_migrates_and_saves(self, tmp_path):
        session_file = tmp_path / "index.json"
        session_file.write_text(json.dumps({
            "session_id": "sess123",
            "name": "会话",
            "metadata": {},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {**_tool_result_block(), "_output_path": "tool_1.txt", "_file_size": 100},
                    ],
                }
            ],
        }, ensure_ascii=False), encoding="utf-8")

        assert migrate_session_file(session_file) is True
        data = json.loads(session_file.read_text(encoding="utf-8"))
        block = data["messages"][0]["content"][0]
        assert block["_meta"]["output_path"] == "tool_1.txt"
        assert block["_meta"]["file_size"] == 100
        # 无 .tmp 残留（原子写）
        assert not (tmp_path / "index.json.tmp").exists()

    def test_already_migrated_returns_false(self, tmp_path):
        session_file = tmp_path / "index.json"
        session_file.write_text(json.dumps({
            "session_id": "sess456",
            "name": "s",
            "metadata": {},
            "messages": [],
        }), encoding="utf-8")
        assert migrate_session_file(session_file) is False

    def test_todos_migrate_uses_session_id(self, tmp_path, monkeypatch):
        """todos 迁移用 data.session_id 而非目录名。"""
        import core.tools.todo as todo_mod
        calls = {}

        def fake_write_todos(session_id, todos):
            calls["session_id"] = session_id
            calls["todos"] = todos

        monkeypatch.setattr(todo_mod, "write_todos", fake_write_todos)
        session_file = tmp_path / "sessxyz" / "index.json"
        session_file.parent.mkdir(parents=True)
        session_file.write_text(json.dumps({
            "session_id": "sessxyz",
            "name": "s",
            "metadata": {"todos": [{"task": "写代码", "status": "pending"}]},
            "messages": [],
        }), encoding="utf-8")

        assert migrate_session_file(session_file) is True
        assert calls["session_id"] == "sessxyz"
        assert calls["todos"][0]["task"] == "写代码"
