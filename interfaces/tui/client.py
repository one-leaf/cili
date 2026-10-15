"""TUI 客户端的 API 层：web 服务器就是服务端，这里只做 httpx 封装。

cwd → workspace 解析（方案 §四）：按 ``normcase+abspath`` 匹配服务端已注册
workspace 的 ``directory``，未命中自动注册（目录名为显示名）。会话与回合走
现有端点；回合输出是 SSE 流，:meth:`chat_stream` 把 ``data: {...}`` 帧解析成
dict 逐个 yield，``{"type": "done"}`` 即回合结束。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import httpx

TUI_INTERFACE = "tui"


class CiliServerError(Exception):
    """服务端不可达 / 响应异常。message 面向终端用户（中文）。"""


def _normalize_dir(path: str) -> str:
    """Windows 盘符大小写与正反斜杠归一化（匹配用，不改变用户可见路径）。"""
    return os.path.normcase(os.path.abspath(path))


def _dir_name(path: str) -> str:
    return os.path.basename(os.path.abspath(path)) or path


class CiliClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        headers = {"X-Access-Token": token} if token else {}
        self._http = httpx.Client(
            base_url=self.base_url, headers=headers, timeout=timeout, transport=transport
        )

    def close(self) -> None:
        self._http.close()

    # ---------- workspace ----------

    def list_workspaces(self) -> list[dict]:
        return self._get("/api/workspaces").get("workspaces", [])

    def create_workspace(self, name: str, directory: str) -> dict:
        return self._post("/api/workspaces", {"name": name, "directory": directory})

    def resolve_workspace(self, cwd: str) -> dict:
        """cwd → workspace（匹配已注册的 directory，未命中自动注册）。"""
        want = _normalize_dir(cwd)
        workspaces = self.list_workspaces()
        for ws in workspaces:
            if ws.get("directory") and _normalize_dir(ws["directory"]) == want:
                return ws
        name = _dir_name(cwd)
        taken = {ws.get("name", "") for ws in workspaces}
        if name in taken:
            suffix = 2
            while f"{name}-{suffix}" in taken:
                suffix += 1
            name = f"{name}-{suffix}"
        try:
            return self.create_workspace(name, cwd)
        except CiliServerError as e:
            raise CiliServerError(f"自动注册 workspace 失败（{cwd}）：{e}") from e

    # ---------- session ----------

    def list_sessions(self, workspace_uuid: str) -> list[dict]:
        data = self._get(f"/api/workspaces/{workspace_uuid}/sessions")
        return data.get("sessions", [])

    def create_session(self, workspace_uuid: str, name: str | None = None) -> dict:
        body = {"name": name} if name else {}
        return self._post(f"/api/workspaces/{workspace_uuid}/sessions", body)

    def session_messages(self, workspace_uuid: str, session_id: str) -> list:
        return self._get(f"/api/workspaces/{workspace_uuid}/sessions/{session_id}").get("messages", [])

    # ---------- 回合 ----------

    def chat_stream(
        self, workspace_uuid: str, session_id: str, content: str
    ) -> Iterator[dict]:
        """发送一条消息并迭代回合的 SSE 帧（dict）；done 帧由调用方判断。

        读超时必须放开：工具执行（bash/python 可达数分钟）期间没有 SSE 帧产出，
        client 级的 10s read timeout 会把正常回合掐断（服务端会继续跑完落盘，
        客户端却收不到结果）。connect 保持 10s 以便快速发现服务端未启动。
        """
        url = f"/api/workspaces/{workspace_uuid}/sessions/{session_id}/messages"
        body = {"content": content, "interface": TUI_INTERFACE}
        stream_timeout = httpx.Timeout(10.0, read=None)
        try:
            with self._http.stream("POST", url, json=body, timeout=stream_timeout) as resp:
                if resp.status_code != 200:
                    detail = _extract_detail(resp)
                    raise CiliServerError(f"发送失败（HTTP {resp.status_code}）：{detail}")
                yield from _iter_sse_frames(resp)
        except httpx.HTTPError as e:
            raise CiliServerError(f"与服务端通信失败：{e}") from e

    def stop(self, workspace_uuid: str, session_id: str) -> dict:
        return self._post(f"/api/workspaces/{workspace_uuid}/sessions/{session_id}/stop", {})

    # ---------- 内部 ----------

    def _get(self, path: str) -> dict:
        try:
            resp = self._http.get(path)
        except httpx.HTTPError as e:
            raise _unreachable(e) from e
        if resp.status_code != 200:
            raise CiliServerError(f"HTTP {resp.status_code}: {_detail_of(resp)}")
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        try:
            resp = self._http.post(path, json=body)
        except httpx.HTTPError as e:
            raise _unreachable(e) from e
        if resp.status_code != 200:
            raise CiliServerError(f"HTTP {resp.status_code}: {_detail_of(resp)}")
        return resp.json()


def _unreachable(e: Exception) -> CiliServerError:
    return CiliServerError(
        f"无法连接服务端（{e.__class__.__name__}: {e}）。请先启动服务端：start.cmd（或 python main.py）"
    )


def _extract_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return str(data.get("detail", data))
    except Exception:  # noqa: BLE001
        return resp.text[:200]


def _detail_of(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return str(data.get("detail", data))[:200]
    except Exception:  # noqa: BLE001
        return resp.text[:200]


def _iter_sse_frames(resp: httpx.Response) -> Iterator[dict]:
    """解析 SSE：收集 ``data:`` 行，空行提交一帧。帧内 JSON 单行（服务端保证）。"""
    for raw in resp.iter_lines():
        line = raw.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue  # 非法帧跳过，不中断回合
