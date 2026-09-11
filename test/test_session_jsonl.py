"""Tests for the new 3-layer session layout (messages.jsonl + index.json commits + meta.json).

覆盖不变式：UI 读 jsonl 全历史（压缩前消息保留）、模型读 commits 视图、
invalid 删条目但 jsonl 保留、spill 后模型 content=None / UI 原始内容、
ask_user 回答 reload 从文件恢复、revert/clear 截断、并发追加、断尾恢复、旧格式迁移。
"""

import json
import threading
from pathlib import Path

from core.session import (
    MESSAGES_FILE,
    SessionManager,
    build_model_messages,
    load_history_messages,
    read_jsonl,
    read_view,
)
from core.migration import migrate_session_to_new_layout


def _new_session(test_workspace, name="RT"):
    sessions_dir = Path(test_workspace) / ".sess_jsonl"
    return SessionManager.create_new_session(sessions_dir, name), sessions_dir


def _session_dir(sessions_dir, session_id):
    return sessions_dir / session_id


class TestRoundTrip:
    def test_round_trip_full_history_and_model_view(self, test_workspace):
        sm, sessions_dir = _new_session(test_workspace)
        for m in [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result text",
                 "_meta": {"tool_name": "bash", "completed": False}},
            ]},
        ]:
            sm.add_message(m["role"], m["content"], _meta=m.get("_meta"))
        sm.save()

        sdir = _session_dir(sessions_dir, sm.session_id)

        # UI 全历史 = 全部 jsonl 行 + 从 commits 合并 _meta
        hist = load_history_messages(sdir)
        assert len(hist) == 3
        assert hist[0]["content"] == "hello"
        assert hist[0]["_meta"]["id"]
        assert hist[0]["_meta"]["seq"] == 0
        assert hist[1]["_meta"]["seq"] == 1
        # block 级 _meta 合并回来
        block = hist[2]["content"][0]
        assert block["tool_use_id"] == "toolu_1"
        assert block["_meta"]["tool_name"] == "bash"
        assert block["_meta"]["completed"] is False

        # jsonl 行不含 block _meta（只存内容历史）
        lines = read_jsonl(sdir / MESSAGES_FILE)
        assert len(lines) == 3
        assert "_meta" not in lines[2]["content"][0]
        assert lines[2]["id"] == hist[2]["_meta"]["id"]

        # 模型视图与内存消息逐字段相等
        loaded = SessionManager.load_session(sm.session_id, sessions_dir)
        assert loaded is not None
        assert [m["role"] for m in loaded.messages] == ["user", "assistant", "user"]
        assert [m["content"] for m in loaded.messages] == [m["content"] for m in sm.messages]
        assert [m["_meta"]["seq"] for m in loaded.messages] == [0, 1, 2]

    def test_commits_order_matches_messages(self, test_workspace):
        sm, sessions_dir = _new_session(test_workspace)
        for i in range(4):
            sm.add_message("user" if i % 2 == 0 else "assistant", f"msg{i}")
        sm.save()
        view = read_view(_session_dir(sessions_dir, sm.session_id))
        seqs = [c["seq"] for c in view["commits"]]
        assert seqs == [0, 1, 2, 3]
        assert view["next_seq"] == 4
        assert view["schema_version"] == 2


class TestCompaction:
    def test_full_compact_preserves_jsonl_history(self, test_workspace):
        sm, sessions_dir = _new_session(test_workspace)
        for i in range(5):
            sm.add_message("user", f"msg{i}")
        sm.add_message("assistant", "summary here", _meta={"summary": True})
        for m in sm.messages[:3]:
            m.setdefault("_meta", {})["valid"] = False
        sm.save()

        sdir = _session_dir(sessions_dir, sm.session_id)

        # 模型视图：invalid 跳过 + summary 内嵌
        view = read_view(sdir)
        commits = view["commits"]
        assert any("summary" in c for c in commits)
        model = build_model_messages(sdir)
        assert len(model) == 3
        assert model[0]["content"] == "msg3"
        assert model[-1]["content"] == "summary here"
        assert model[-1]["_meta"]["summary"] is True

        # UI 全历史：5 条原始消息完整保留，摘要不进 jsonl
        hist = load_history_messages(sdir)
        assert [m["content"] for m in hist] == [f"msg{i}" for i in range(5)]
        assert len(read_jsonl(sdir / MESSAGES_FILE)) == 5

    def test_invalidate_removes_commit_keeps_jsonl(self, test_workspace):
        sm, sessions_dir = _new_session(test_workspace)
        sm.add_message("user", "keep me")
        sm.add_message("user", "drop me")
        sm.save()
        sm.messages[1]["_meta"]["valid"] = False
        sm.save()

        sdir = _session_dir(sessions_dir, sm.session_id)
        assert len(read_view(sdir)["commits"]) == 1
        assert len(load_history_messages(sdir)) == 2


class TestMicroCompactSpill:
    def test_inline_compact_no_spill_content_in_jsonl(self, test_workspace):
        """压缩内联结果：不生成外置文件，原文保留在 messages.jsonl。"""
        from core.compression import microcompact_tool_results

        sm, sessions_dir = _new_session(test_workspace)
        inline_content = "x" * 500  # 内联结果（<10KB 不截断、无外置文件）
        sm.add_message("user", [{"type": "tool_result", "tool_use_id": "toolu_old",
                                 "content": inline_content}])
        sm.add_message("user", [{"type": "tool_result", "tool_use_id": "toolu_recent",
                                 "content": "recent"}])
        sm.save()
        sdir = _session_dir(sessions_dir, sm.session_id)

        # 压缩：标记 compacted + 清空内存，不生成外置文件
        saved = microcompact_tool_results(sm.messages, keep_recent=1)
        assert saved == len(inline_content)
        block = sm.messages[0]["content"][0]
        assert block["_meta"]["compacted"] is True
        assert block["content"] is None
        assert "output_path" not in block["_meta"]
        assert not (sdir / "toolu_old.txt").exists()  # 不 spill 外置文件
        sm.save()

        # 模型视图：compacted → content None（无 output_path 也清空）
        model = build_model_messages(sdir)
        b = model[0]["content"][0]
        assert b["content"] is None
        assert b["_meta"]["compacted"] is True

        # UI 全历史：jsonl 保留原始内联内容（去 messages.jsonl 查看）
        hist = load_history_messages(sdir)
        assert hist[0]["content"][0]["content"] == inline_content
        assert hist[0]["content"][0]["_meta"]["compacted"] is True

    def test_inline_compact_skips_unpersisted(self, test_workspace):
        """未持久化（无 seq）的内联结果不被压缩，避免清空后原文丢失。"""
        from core.compression import microcompact_tool_results

        sm, sessions_dir = _new_session(test_workspace)
        content = "y" * 500
        sm.add_message("user", [{"type": "tool_result", "tool_use_id": "toolu_ns",
                                 "content": content}])
        sm.add_message("user", [{"type": "tool_result", "tool_use_id": "toolu_recent",
                                 "content": "recent"}])
        # 未保存（无 seq）
        saved = microcompact_tool_results(sm.messages, keep_recent=1)
        assert saved == 0
        assert sm.messages[0]["content"][0]["content"] == content  # 原文未清空

    def test_spill_model_content_none_ui_original(self, test_workspace):
        sm, sessions_dir = _new_session(test_workspace)
        sm.add_message("user", "question")
        sm.add_message("assistant", "thinking")
        sm.add_message("user", [{"type": "tool_result", "tool_use_id": "toolu_1",
                                 "content": "LONG ORIGINAL CONTENT"}])
        sm.save()

        # 模拟 microcompact：内存 content=None + compacted/output_path
        block = sm.messages[-1]["content"][0]
        block["_meta"] = {"compacted": True, "output_path": "toolu_1.txt", "file_size": 21}
        block["content"] = None
        (_session_dir(sessions_dir, sm.session_id) / "toolu_1.txt").write_text("spilled", encoding="utf-8")
        sm.save()

        sdir = _session_dir(sessions_dir, sm.session_id)

        # 模型视图重建：content=None + output_path（_resolve_tool_results 会读文件）
        model = build_model_messages(sdir)
        b = model[-1]["content"][0]
        assert b["content"] is None
        assert b["_meta"]["compacted"] is True
        assert b["_meta"]["output_path"] == "toolu_1.txt"

        # UI 全历史：jsonl 保留压缩前原始内容
        hist = load_history_messages(sdir)
        assert hist[-1]["content"][0]["content"] == "LONG ORIGINAL CONTENT"
        assert hist[-1]["content"][0]["_meta"]["compacted"] is True


class TestAskUserAnswer:
    def test_answer_reload_from_file(self, test_workspace):
        sm, sessions_dir = _new_session(test_workspace)
        sm.add_message("user", "question?")
        sm.add_message("assistant", [{"type": "tool_use", "id": "toolu_q", "name": "ask_user",
                                      "input": {"question": "question?"}}])
        sm.add_message("user", [{"type": "tool_result", "tool_use_id": "toolu_q",
                                 "content": "等待用户回答..."}])
        sm.save()

        # 模拟 web_api answer_ask_user：总是写答案文件 + completed/output_path
        sdir = _session_dir(sessions_dir, sm.session_id)
        block = sm.messages[-1]["content"][0]
        answer = "答案是 42"
        block["content"] = answer
        block["_meta"] = {
            "completed": True,
            "output_path": "toolu_q.txt",
            "file_size": len(answer.encode("utf-8")),
        }
        (sdir / "toolu_q.txt").write_text(answer, encoding="utf-8")
        sm.save()

        # 模型视图：content 被 _apply_model_content_rules 清空，等待 _resolve_tool_results 读文件
        loaded = SessionManager.load_session(sm.session_id, sessions_dir)
        b = loaded.messages[-1]["content"][0]
        assert b["content"] is None
        assert b["_meta"]["completed"] is True
        assert (loaded.session_dir / b["_meta"]["output_path"]).read_text(encoding="utf-8") == answer

        # UI 全历史：jsonl 占位保留 + completed 标记（web_api 据 completed+output_path 读文件）
        hist = load_history_messages(sdir)
        b2 = hist[-1]["content"][0]
        assert b2["content"] == "等待用户回答..."
        assert b2["_meta"]["completed"] is True
        assert b2["_meta"]["output_path"] == "toolu_q.txt"


class TestRevertClear:
    def test_revert_truncates_jsonl_and_rolls_back_seq(self, test_workspace):
        sm, sessions_dir = _new_session(test_workspace)
        for i in range(5):
            sm.add_message("user", f"m{i}")
        sm.save()
        sdir = _session_dir(sessions_dir, sm.session_id)

        target_id = sm.messages[2]["_meta"]["id"]
        deleted = sm.revert_to_message(target_id)
        assert deleted == 3  # m2、m3、m4 删除（撤销到 m2 之前，与旧 API 一致）

        lines = read_jsonl(sdir / MESSAGES_FILE)
        assert [l["seq"] for l in lines] == [0, 1]
        assert [m["content"] for m in sm.messages] == ["m0", "m1"]

        # next_seq 回退：新消息复用被截断的序号
        sm.add_message("user", "after revert")
        sm.save()
        lines = read_jsonl(sdir / MESSAGES_FILE)
        assert lines[-1]["seq"] == 2

    def test_revert_unknown_id_raises(self, test_workspace):
        sm, sessions_dir = _new_session(test_workspace)
        sm.add_message("user", "m0")
        sm.save()
        try:
            sm.revert_to_message("no-such-id")
            assert False, "should raise ValueError"
        except ValueError:
            pass

    def test_clear_writes_disk(self, test_workspace):
        sm, sessions_dir = _new_session(test_workspace)
        sm.add_message("user", "m0")
        sm.add_message("assistant", "m1")
        sm.save()
        sm.clear()
        sdir = _session_dir(sessions_dir, sm.session_id)
        assert sm.messages == []
        assert load_history_messages(sdir) == []
        assert read_view(sdir)["commits"] == []


class TestTornTail:
    def test_torn_tail_dropped(self, test_workspace):
        sm, sessions_dir = _new_session(test_workspace)
        for i in range(3):
            sm.add_message("user", f"m{i}")
        sm.save()
        sdir = _session_dir(sessions_dir, sm.session_id)

        # 追加一个损坏行（缺闭合括号）
        with open(sdir / MESSAGES_FILE, "a", encoding="utf-8") as f:
            f.write('{"seq": 3, "id": "x", "role": "user", "content": "crashed"\n')

        assert len(read_jsonl(sdir / MESSAGES_FILE)) == 3
        assert len(load_history_messages(sdir)) == 3


class TestConcurrency:
    def test_concurrent_save_no_loss_no_dup(self, test_workspace):
        sm, sessions_dir = _new_session(test_workspace)
        n_threads, per_thread = 4, 20

        def worker(n):
            for i in range(per_thread):
                sm.add_message("user", f"t{n}-{i}")
                sm.save()

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        hist = load_history_messages(_session_dir(sessions_dir, sm.session_id))
        assert len(hist) == n_threads * per_thread
        seqs = [m["_meta"]["seq"] for m in hist]
        assert len(set(seqs)) == len(seqs)  # 无重复 seq


class TestMigration:
    def test_old_layout_migrated_to_new(self, test_workspace):
        sessions_dir = Path(test_workspace) / ".sess_mig"
        sid = "abc12345"
        session_dir = sessions_dir / sid
        session_dir.mkdir(parents=True, exist_ok=True)
        old = {
            "session_id": sid,
            "name": "Old",
            "metadata": {"created_at": "2026-01-01 00:00:00"},
            "messages": [
                {"role": "user", "content": "old q"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1",
                                                   "name": "read", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                              "content": "res", "_meta": {"tool_name": "read"}}]},
            ],
        }
        (session_dir / "index.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

        assert migrate_session_to_new_layout(session_dir) is True
        assert (session_dir / "messages.jsonl").exists()
        assert (session_dir / "meta.json").exists()
        assert (session_dir / "index.json.legacy").exists()

        sm = SessionManager.load_session(sid, sessions_dir)
        assert sm is not None
        assert sm.name == "Old"
        assert len(sm.messages) == 3
        assert sm.messages[2]["content"][0]["_meta"]["tool_name"] == "read"
        assert [m["_meta"]["seq"] for m in sm.messages] == [0, 1, 2]
        assert len(load_history_messages(session_dir)) == 3

        # 幂等：已是新布局则不再迁移
        assert migrate_session_to_new_layout(session_dir) is False

    def test_invalid_messages_survive_migration_in_jsonl(self, test_workspace):
        sessions_dir = Path(test_workspace) / ".sess_mig_inv"
        sid = "def67890"
        session_dir = sessions_dir / sid
        session_dir.mkdir(parents=True, exist_ok=True)
        old = {
            "session_id": sid,
            "name": "N",
            "metadata": {},
            "messages": [
                {"role": "user", "content": "valid"},
                {"role": "user", "content": "invalid", "_meta": {"valid": False}},
            ],
        }
        (session_dir / "index.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        assert migrate_session_to_new_layout(session_dir) is True
        # 模型视图只剩有效消息；jsonl 两条都保留（UI 可见）
        assert len(build_model_messages(session_dir)) == 1
        assert len(load_history_messages(session_dir)) == 2
