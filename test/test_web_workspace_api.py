"""web 工作区域测试：依赖校验、列表、创建、指令文件、上传、删除、导入导出。"""

from unittest.mock import MagicMock

import pytest


class TestRequireWorkspace:
    """require_workspace_data_dir() dependency validation."""

    def test_nonexistent_workspace_raises_404(self):
        from fastapi import HTTPException

        from web.deps import require_workspace_data_dir
        with pytest.raises(HTTPException) as exc_info:
            # require_workspace_data_dir is an async dependency
            import asyncio
            asyncio.run(require_workspace_data_dir("nonexistent-uuid"))
        assert exc_info.value.status_code == 404


class TestValidateWorkspaceUuid:
    """validate_workspace_uuid() 路径穿越校验。"""

    def test_valid_uuid_passes(self):
        from web.deps import validate_workspace_uuid
        validate_workspace_uuid("abc123")  # 不应抛异常

    def test_path_traversal_rejected(self):
        from fastapi import HTTPException

        from web.deps import validate_workspace_uuid
        for evil in ("..", "../..", "a/b", "C:\\evil", "a%2Fb"):
            with pytest.raises(HTTPException) as exc_info:
                validate_workspace_uuid(evil)
            assert exc_info.value.status_code == 400


class TestListAllWorkspaces:
    """_list_all_workspaces() reads workspaces.json index."""

    def test_returns_empty_when_no_workspaces(self, tmp_path, monkeypatch):
        """Empty index returns empty list."""
        import core.config as config_mod
        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        import web.deps as api_module
        result = api_module._list_all_workspaces()
        assert result == []

    def test_reads_index_entries(self, tmp_path, monkeypatch):
        """Index entries are returned with their metadata."""
        import core.config as config_mod
        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        config_mod.upsert_workspace_entry({
            "uuid": "ws1",
            "workspace_name": "WS One",
            "directory": str(tmp_path / "ws1"),
            "created_at": "2026-09-21 10:00:00",
        })
        import web.deps as api_module
        result = api_module._list_all_workspaces()
        assert result == [{
            "uuid": "ws1",
            "name": "WS One",
            "directory": str(tmp_path / "ws1"),
            "created_at": "2026-09-21 10:00:00",
            "system": False,
        }]


class TestCreateWorkspaceDefaults:
    """POST /api/workspaces 新建工作区默认开启版本管理与记忆。"""

    def test_create_defaults_enabled(self, tmp_path, monkeypatch):
        """新建工作区：git_enabled/memory_enabled 默认 True，且立即初始化 git"""
        import asyncio

        import core.config as config_mod
        from web.routes_workspace import CreateWorkspaceRequest, create_workspace

        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        monkeypatch.setattr("core.workspace_git.is_git_initialized", lambda uuid: False)

        inited = []

        def _fake_init(uuid):
            inited.append(uuid)
            return True, "ok"

        monkeypatch.setattr("core.workspace_git.init_workspace_git", _fake_init)

        result = asyncio.run(create_workspace(
            CreateWorkspaceRequest(name="测试工作区", directory=str(tmp_path / "ws"))
        ))
        entry = config_mod.get_workspace_meta(result["uuid"])
        assert entry["git_enabled"] is True
        assert entry["memory_enabled"] is True
        assert inited == [result["uuid"]]

    def test_create_git_init_failure_not_fatal(self, tmp_path, monkeypatch):
        """git 初始化失败不影响工作区创建，开关保持开启"""
        import asyncio

        import core.config as config_mod
        from web.routes_workspace import CreateWorkspaceRequest, create_workspace

        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        monkeypatch.setattr("core.workspace_git.is_git_initialized", lambda uuid: False)
        monkeypatch.setattr("core.workspace_git.init_workspace_git", lambda uuid: (False, "Git 未安装"))

        result = asyncio.run(create_workspace(
            CreateWorkspaceRequest(name="测试工作区", directory=str(tmp_path / "ws"))
        ))
        entry = config_mod.get_workspace_meta(result["uuid"])
        assert entry["git_enabled"] is True
        assert entry["memory_enabled"] is True


class TestSaveInstructionsFilenameGuard:
    """save_instructions 文件名校验：拒绝逃出工作区的路径。

    回归：此前只校验「白名单文件名 or .md 后缀」，"../../evil.md" 与
    绝对路径 "C:/evil.md" 都能通过，os.path.join 后可写出工作区外。
    """

    def _prepare(self, tmp_path, monkeypatch):
        import core.config as config_mod
        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        config_mod.upsert_workspace_entry({
            "uuid": "wstraverse",
            "workspace_name": "Traverse",
            "directory": str(ws_dir),
        })
        return ws_dir

    def _call(self, filename, ws_dir):
        import asyncio

        from web.routes_workspace import SaveInstructionsRequest, save_instructions
        return asyncio.run(save_instructions(
            "wstraverse",
            SaveInstructionsRequest(filename=filename, content="x"),
            ws_dir=ws_dir,
        ))

    @pytest.mark.parametrize("bad", [
        "../../evil.md",
        "../evil.md",
        "sub/evil.md",
        "sub\\evil.md",
        "..",
        "",
    ])
    def test_rejects_path_escapes(self, tmp_path, monkeypatch, bad):
        from fastapi import HTTPException

        ws_dir = self._prepare(tmp_path, monkeypatch)
        with pytest.raises(HTTPException) as exc:
            self._call(bad, ws_dir)
        assert exc.value.status_code == 400
        # 工作区外不得留下任何文件
        assert not (tmp_path / "evil.md").exists()

    def test_accepts_plain_markdown_name(self, tmp_path, monkeypatch):
        ws_dir = self._prepare(tmp_path, monkeypatch)
        result = self._call("NOTES.md", ws_dir)
        assert result["success"] is True
        assert (ws_dir / "NOTES.md").read_text(encoding="utf-8") == "x"


class TestUploadEndpointRequest:
    """P0-1 回归：upload_files 必须声明 request: Request 形参。

    此前函数体调用 csrf_protect(request) 但签名无此参数，上传端点必抛
    NameError → 500，上传功能完全不可用。
    """

    def _prepare(self, tmp_path, monkeypatch):
        import core.config as config_mod
        monkeypatch.setattr(config_mod, "WORKSPACES_JSON", tmp_path / "workspaces.json")
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        config_mod.upsert_workspace_entry({
            "uuid": "wstraverse",
            "workspace_name": "Traverse",
            "directory": str(ws_dir),
        })
        return ws_dir

    @staticmethod
    def _fake_request():
        from starlette.requests import Request as StarletteRequest
        scope = {
            "type": "http", "method": "POST", "path": "/api/files/upload",
            "headers": [], "query_string": b"", "client": ("127.0.0.1", 1),
        }
        return StarletteRequest(scope)

    def test_upload_writes_file(self, tmp_path, monkeypatch):
        import io

        from fastapi import UploadFile

        from web.routes_files import upload_files

        ws_dir = self._prepare(tmp_path, monkeypatch)
        upload = UploadFile(file=io.BytesIO(b"hello"), filename="a.txt")
        result = upload_files(self._fake_request(), "wstraverse", "", [upload])
        assert (ws_dir / "a.txt").read_text(encoding="utf-8") == "hello"
        assert not result.get("errors")

    def test_upload_does_not_overwrite_same_name(self, tmp_path, monkeypatch):
        import io

        from fastapi import UploadFile

        from web.routes_files import upload_files

        ws_dir = self._prepare(tmp_path, monkeypatch)
        (ws_dir / "a.txt").write_text("first", encoding="utf-8")
        upload = UploadFile(file=io.BytesIO(b"second"), filename="a.txt")
        result = upload_files(self._fake_request(), "wstraverse", "", [upload])
        assert (ws_dir / "a.txt").read_text(encoding="utf-8") == "first"
        assert (ws_dir / "a-1.txt").read_text(encoding="utf-8") == "second"
        assert result["uploaded"][0]["name"] == "a-1.txt"


class TestSessionDeleteNoGhost:
    """删除会话不残留空会话（回归）。

    根因：runner.cleanup() 会 save 当前会话；若删除路由先 rmtree 再从
    registry 摘除 runner，save 经 atomic_write_json 的 mkdir 把已删目录
    重建为空壳（index/meta 在、jsonl 缺失 → 0 条消息）。批量删除此前
    甚至不摘 runner，之后 LRU 淘汰/关停时同样复活空会话。
    """

    def _snapshot(self):
        from core.session_registry import registry
        return dict(registry._hosted), dict(registry._access)

    def _restore(self, snapshot):
        from core.session_registry import registry
        hosted, access = snapshot
        registry._hosted.clear()
        registry._hosted.update(hosted)
        registry._access.clear()
        registry._access.update(access)

    def _make_hosted_session(self, sessions_dir):
        """建一个有消息的会话 + 在 registry 里挂一个 cleanup 会 save 的假 runner。

        cleanup 模拟真实 SessionRunner.cleanup() 的行为（sync + save），
        使「先删目录后摘 runner」的错误顺序能在测试中复现残留。
        """
        import time

        from core.session import SessionStore
        from core.session_registry import HostedSession, registry, session_key

        store = SessionStore.create_new_session(sessions_dir, "待删会话")
        store.add_message("user", "你好", flush=True)
        store.save()

        def cleanup():
            store.save(force=True)

        runner = MagicMock()
        runner.cleanup.side_effect = cleanup
        runner.is_running.return_value = False
        key = session_key("test-workspace", store.session_id)
        registry._hosted[key] = HostedSession(key=key, runner=runner)
        registry._access[key] = time.time()
        return store, key

    def test_delete_session_no_ghost_dir(self, test_workspace):
        """单删：先摘 runner 再 rmtree，目录不复活、registry 不残留。"""
        import asyncio

        from core.config import get_workspace_data_dir
        from core.session_registry import registry
        from web.routes_workspace import delete_session

        ws_dir = get_workspace_data_dir("test-workspace")
        sessions_dir = ws_dir / "sessions"
        snap = self._snapshot()
        try:
            store, key = self._make_hosted_session(sessions_dir)
            result = asyncio.run(delete_session("test-workspace", store.session_id, ws_dir=ws_dir))
            assert result == {"success": True}
            assert not store.session_dir.exists()
            assert key not in registry._hosted
        finally:
            self._restore(snap)

    def test_batch_delete_no_ghost_dir(self, test_workspace):
        """批量删：同样先摘 runner，目录不复活。"""
        import asyncio

        from core.config import get_workspace_data_dir
        from core.session_registry import registry
        from web.routes_workspace import BatchSessionRequest, batch_session_operation

        ws_dir = get_workspace_data_dir("test-workspace")
        sessions_dir = ws_dir / "sessions"
        snap = self._snapshot()
        try:
            store, key = self._make_hosted_session(sessions_dir)
            request = BatchSessionRequest(session_ids=[store.session_id], action="delete")
            result = asyncio.run(batch_session_operation("test-workspace", request, ws_dir=ws_dir))
            assert result["success"] is True
            assert result["results"] == [{"session_id": store.session_id, "success": True}]
            assert not store.session_dir.exists()
            assert key not in registry._hosted
        finally:
            self._restore(snap)

    def test_persist_skips_when_dir_deleted(self, tmp_path):
        """兜底：会话目录被外部删除后，save 不经 mkdir 重建空壳。"""
        import shutil

        from core.session import SessionStore

        store = SessionStore.create_new_session(tmp_path / "sessions", "x")
        store.add_message("user", "你好", flush=True)
        store.save()
        shutil.rmtree(store.session_dir)

        store._bump_index_version()  # 模拟 cleanup 前 _sync_to_session 置脏
        store.save()

        assert not store.session_dir.exists()


class TestSessionExportImport:
    """会话导出 / 导入：JSON 往返，导入生成新会话且保留消息顺序。"""

    def test_export_import_roundtrip(self, test_workspace):
        import asyncio

        from core.config import get_workspace_data_dir
        from core.session import SessionStore
        from web.routes_workspace import export_session, import_session

        ws_dir = get_workspace_data_dir("test-workspace")
        sessions_dir = ws_dir / "sessions"
        store = SessionStore.create_new_session(sessions_dir, "导出会话")
        store.add_message("user", "你好", flush=True)
        store.add_message("assistant", "你也好", flush=True)
        store.save()

        exported = asyncio.run(export_session("test-workspace", store.session_id, ws_dir=ws_dir))
        assert exported["format"] == "cili-session"
        assert exported["name"] == "导出会话"
        assert [m["role"] for m in exported["messages"]] == ["user", "assistant"]

        imported = asyncio.run(import_session("test-workspace", exported, ws_dir=ws_dir))
        assert imported["session_id"] != store.session_id
        assert imported["name"] == "导出会话"
        loaded = SessionStore.load_session(imported["session_id"], sessions_dir)
        assert loaded is not None
        assert [m["role"] for m in loaded.messages] == ["user", "assistant"]

    def test_import_rejects_bad_payload(self, test_workspace):
        import asyncio

        from fastapi import HTTPException

        from core.config import get_workspace_data_dir
        from web.routes_workspace import import_session

        ws_dir = get_workspace_data_dir("test-workspace")
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(import_session(
                "test-workspace",
                {"messages": [{"role": "system", "content": "x"}]},
                ws_dir=ws_dir))
        assert exc_info.value.status_code == 400


class TestGetSessionTodos:
    """会话详情接口要返回独立 todo 文件的内容（切换会话后任务清单可恢复）。"""

    def test_get_session_includes_todos_from_file(self, tmp_path, monkeypatch):
        import asyncio

        from core import todo_store as todo_mod
        from web import routes_workspace

        (tmp_path / "sessions" / "abc12345").mkdir(parents=True)
        (tmp_path / "sessions" / "abc12345" / "index.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(
            routes_workspace, "load_history_meta",
            lambda d: {"session_id": "abc12345", "name": "n", "metadata": {}},
        )
        monkeypatch.setattr(routes_workspace, "load_history_messages", lambda d: [])
        monkeypatch.setattr(todo_mod, "_get_todo_dir", lambda ws="": tmp_path / "todo")
        todos = [{"content": "写代码", "status": "in_progress"}]
        todo_mod.write_todos("abc12345", todos, "ws-uuid")

        result = asyncio.run(
            routes_workspace.get_session("ws-uuid", "abc12345", None, 0, ws_dir=tmp_path)
        )
        assert result["todos"] == todos

    def test_get_session_without_todos_returns_empty_list(self, tmp_path, monkeypatch):
        import asyncio

        from core import todo_store as todo_mod
        from web import routes_workspace

        (tmp_path / "sessions" / "abc12345").mkdir(parents=True)
        (tmp_path / "sessions" / "abc12345" / "index.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(
            routes_workspace, "load_history_meta",
            lambda d: {"session_id": "abc12345", "name": "n", "metadata": {}},
        )
        monkeypatch.setattr(routes_workspace, "load_history_messages", lambda d: [])
        monkeypatch.setattr(todo_mod, "_get_todo_dir", lambda ws="": tmp_path / "todo")

        result = asyncio.run(
            routes_workspace.get_session("ws-uuid", "abc12345", None, 0, ws_dir=tmp_path)
        )
        assert result["todos"] == []
