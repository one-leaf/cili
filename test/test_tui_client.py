"""TUI API 客户端测试：httpx.MockTransport 注入，不依赖真实服务端。"""

from __future__ import annotations

import json

import httpx
import pytest

from interfaces.tui.client import CiliClient, CiliServerError, _iter_sse_frames


def sse_body(frames: list[dict]) -> str:
    return "".join(f"data: {json.dumps(f, ensure_ascii=False)}\n\n" for f in frames)


def make_client(handler) -> CiliClient:
    return CiliClient(
        "http://test", token="tok", transport=httpx.MockTransport(handler)
    )


class TestResolveWorkspace:
    def test_match_normalizes_path(self):
        """目录匹配不区分盘符大小写与斜杠方向（Windows 归一化）。"""
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(200, json={"workspaces": [
                {"uuid": "ws1", "name": "tmp", "directory": "E:/TMP"},
            ]})

        client = make_client(handler)
        ws = client.resolve_workspace("e:\\tmp\\")
        assert ws["uuid"] == "ws1"

    def test_miss_registers_with_dir_name(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.url.path == "/api/workspaces" and request.method == "GET":
                return httpx.Response(200, json={"workspaces": []})
            if request.method == "POST":
                body = json.loads(request.content)
                assert body["name"] == "tmp"
                assert body["directory"] == "e:\\tmp"
                return httpx.Response(200, json={"uuid": "ws9", "name": "tmp", "directory": body["directory"]})
            return httpx.Response(404)

        client = make_client(handler)
        ws = client.resolve_workspace("e:\\tmp")
        assert ws["uuid"] == "ws9"
        assert calls == [("GET", "/api/workspaces"), ("POST", "/api/workspaces")]

    def test_name_conflict_gets_suffix(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"workspaces": [
                    {"uuid": "a", "name": "tmp", "directory": "d:/other"},
                    {"uuid": "b", "name": "tmp-2", "directory": "d:/other2"},
                ]})
            body = json.loads(request.content)
            assert body["name"] == "tmp-3"
            return httpx.Response(200, json={"uuid": "c", "name": "tmp-3", "directory": body["directory"]})

        client = make_client(handler)
        ws = client.resolve_workspace("e:\\tmp")
        assert ws["name"] == "tmp-3"

    def test_register_failure_wraps_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"workspaces": []})
            return httpx.Response(400, json={"detail": "名字不合法"})

        client = make_client(handler)
        with pytest.raises(CiliServerError, match="自动注册 workspace 失败"):
            client.resolve_workspace("e:\\tmp")


class TestChatStream:
    def test_parses_sse_frames(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/messages")
            body = json.loads(request.content)
            assert body["interface"] == "tui"  # 回合按 TUI 档案渲染（方案 §五）
            assert request.headers["X-Access-Token"] == "tok"
            body_text = sse_body([
                {"type": "text", "content": "你好"},
                {"type": "tool_use", "tool": "read", "input": {}, "tool_use_id": "t1"},
                {"type": "done"},
            ])
            return httpx.Response(200, text=body_text)

        client = make_client(handler)
        frames = list(client.chat_stream("ws1", "s1", "测试消息"))
        assert [f["type"] for f in frames] == ["text", "tool_use", "done"]

    def test_busy_session_error_frame(self):
        """并发驱动同一会话：服务端认领失败，返回 error 帧 + done（方案 §五）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=sse_body([
                {"type": "error", "content": "当前会话正在执行中，请等待完成后再发送消息"},
                {"type": "done"},
            ]))

        client = make_client(handler)
        frames = list(client.chat_stream("ws1", "s1", "hi"))
        assert frames[0]["type"] == "error"

    def test_http_error_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "boom"})

        client = make_client(handler)
        with pytest.raises(CiliServerError, match="boom"):
            list(client.chat_stream("ws1", "s1", "hi"))

    def test_connection_error_has_start_hint(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        client = make_client(handler)
        with pytest.raises(CiliServerError, match="start.cmd"):
            client.list_workspaces()


class TestSessions:
    def test_list_and_create(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"sessions": [
                    {"session_id": "s1", "name": "最近", "preview": "p"},
                ]})
            body = json.loads(request.content)
            assert body == {"name": "新会话"}
            return httpx.Response(200, json={"session_id": "s2", "name": "新会话"})

        client = make_client(handler)
        sessions = client.list_sessions("ws1")
        assert sessions[0]["session_id"] == "s1"
        session = client.create_session("ws1", "新会话")
        assert session["session_id"] == "s2"

    def test_stop(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/stop")
            return httpx.Response(200, json={"success": True, "message": "已发送停止信号"})

        client = make_client(handler)
        assert client.stop("ws1", "s1")["success"] is True


class TestSseParsing:
    def test_multiline_chunks(self):
        """iter_lines 逐行喂入，空行定帧边界。"""
        lines = [
            "data: " + json.dumps({"type": "text", "content": "a"}),
            "",
            "data: " + json.dumps({"type": "done"}),
            "",
        ]
        frames = list(_iter_sse_frames(_fake_response(lines)))
        assert [f["type"] for f in frames] == ["text", "done"]

    def test_invalid_json_skipped(self):
        lines = ["data: not-json", "", "data: " + json.dumps({"type": "done"}), ""]
        frames = list(_iter_sse_frames(_fake_response(lines)))
        assert [f["type"] for f in frames] == ["done"]


def _fake_response(lines: list[str]) -> httpx.Response:
    return httpx.Response(200, text="\n".join(lines))
