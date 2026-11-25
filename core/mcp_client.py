"""MCP 连接管理器：后台 asyncio loop + 每 server 的 ClientSession 生命周期。

同步工具层通过 ``MCPProvider.run_in_loop`` 提交协程并阻塞等待；工具包装类
``MCPToolWrapper`` 在 ``core/tools/mcp.py``。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
import urllib.parse
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from core.config import MCPConfig
from core.mcp_naming import _sanitize_mcp_tool_name
from core.tools.base import ToolResult

if TYPE_CHECKING:
    from core.tools.mcp import MCPToolWrapper

logger = logging.getLogger(__name__)

# 后台 loop 线程启动等待上限（秒）
_LOOP_START_TIMEOUT_S = 5.0

# 临时连接错误：单次重试（server 重启或网络瞬时中断）
_TRANSIENT_EXC_NAMES: frozenset[str] = frozenset((
    "ClosedResourceError",
    "BrokenResourceError",
    "EndOfStream",
    "BrokenPipeError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "ConnectionAbortedError",
    "ConnectionError",
))


def _is_transient(exc: BaseException) -> bool:
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


# ── MCP inputSchema 归一化（JSON Schema → 模型 API 兼容）──────────────────

def _extract_nullable_branch(options: Any) -> dict[str, Any] | None:
    """返回 nullable union 的唯一非 null 分支。"""
    if not isinstance(options, list):
        return None
    non_null: list[dict[str, Any]] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)
    if saw_null and len(non_null) == 1:
        return non_null[0]
    return None


def _resolve_local_schema_ref(root: dict[str, Any], ref: str) -> Any:
    """解析本地 JSON Pointer，不接受远程引用。"""
    if not ref.startswith("#"):
        raise ValueError("not a local JSON Pointer")
    pointer = urllib.parse.unquote(ref[1:], errors="strict")
    if not pointer:
        return root
    if not pointer.startswith("/"):
        raise ValueError("not a local JSON Pointer")
    current: Any = root
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[part]
        elif isinstance(current, list):
            current = current[int(part)]
        else:
            raise KeyError(part)
    return current


def _rewrite_local_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """把任意本地 JSON Pointer $ref 提升为模型 API 兼容的 $defs。"""
    rewritten_refs: dict[str, str] = {}
    generated_defs: dict[str, Any] = {}

    def rewrite(value: Any) -> Any:
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if not isinstance(value, dict):
            return value
        rewritten = dict(value)
        raw_ref = rewritten.get("$ref")
        ref = raw_ref if isinstance(raw_ref, str) else None
        is_rewritable_ref = False
        if ref is not None and not ref.startswith("#/$defs/"):
            try:
                pointer = urllib.parse.unquote(ref[1:], errors="strict")
            except (UnicodeDecodeError, ValueError):
                pass
            else:
                is_rewritable_ref = ref.startswith("#") and (
                    not pointer or pointer.startswith("/")
                )
        if is_rewritable_ref:
            name = rewritten_refs.get(ref)
            if name is None:
                try:
                    target = _resolve_local_schema_ref(schema, ref)
                except (KeyError, IndexError, TypeError, UnicodeDecodeError, ValueError):
                    logger.warning(f"[MCP] schema 含无法解析的本地 $ref: {ref}")
                else:
                    name = f"ref_{hashlib.sha256(ref.encode()).hexdigest()[:12]}"
                    existing_defs = schema.get("$defs")
                    while isinstance(existing_defs, dict) and name in existing_defs:
                        name += "_"
                    rewritten_refs[ref] = name
                    generated_defs[name] = rewrite(target)
            if name is not None:
                rewritten["$ref"] = f"#/$defs/{name}"
        return {key: rewrite(item) for key, item in rewritten.items()}

    result = rewrite(schema)
    if generated_defs:
        existing_defs = result.get("$defs")
        result["$defs"] = {
            **(existing_defs if isinstance(existing_defs, dict) else {}),
            **generated_defs,
        }
    return result


def _normalize_nullable_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """把 nullable/oneOf/anyOf 结构归一化为模型 API 接受的单类型 schema。"""
    normalized = dict(schema)
    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]
            normalized["nullable"] = True

    for key in ("oneOf", "anyOf"):
        branch = _extract_nullable_branch(normalized.get(key))
        if branch is not None:
            merged = {k: v for k, v in normalized.items() if k != key}
            merged.update(branch)
            normalized = merged
            normalized["nullable"] = True
            break

    properties = normalized.get("properties")
    if isinstance(properties, dict):
        normalized["properties"] = {
            name: (
                _normalize_nullable_schema(prop) if isinstance(prop, dict) else prop
            )
            for name, prop in properties.items()
        }
    items = normalized.get("items")
    if isinstance(items, dict):
        normalized["items"] = _normalize_nullable_schema(items)
    definitions = normalized.get("$defs")
    if isinstance(definitions, dict):
        normalized["$defs"] = {
            name: _normalize_nullable_schema(definition)
            if isinstance(definition, dict)
            else definition
            for name, definition in definitions.items()
        }

    if normalized.get("type") == "object":
        normalized.setdefault("properties", {})
        normalized.setdefault("required", [])
    return normalized


def _normalize_schema(schema: Any) -> dict[str, Any]:
    """把 MCP 提供的 schema 归一化为规范 JSON Schema（input_schema 承载）。

    MCP server 可能输出 nullable/oneOf/anyOf、本地 $ref 等非规范结构，
    统一在此折叠为单类型 schema，供 wrapper 存储 / 模型 API 消费。
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    return _normalize_nullable_schema(_rewrite_local_schema_refs(schema))


# ── 工具结果渲染 ───────────────────────────────────────────────────────────

def _render_tool_result(result: Any) -> str:
    """把 MCP call_tool 的 content blocks 渲染为文本。"""
    try:
        from mcp import types
    except ImportError:
        parts = []
        for block in getattr(result, "content", []) or []:
            parts.append(str(getattr(block, "text", block)))
        return "\n".join(parts) or "(no output)"

    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        elif isinstance(block, getattr(types, "ImageContent", ())):
            parts.append(f"[图片内容: {len(getattr(block, 'data', '') or '')} bytes]")
        else:
            parts.append(str(block))
    return "\n".join(parts) or "(no output)"


# ── MCP 连接管理器 ─────────────────────────────────────────────────────────


class MCPProvider:
    """全局单例：管理后台 asyncio loop + 每个（工作区, server）的 ClientSession。

    后台线程跑一个常驻 asyncio event loop（与 BrowserService 的 worker 线程
    范式一致）。同步线程通过 run_in_loop 提交协程并阻塞等待。

    MCP 配置按工作区隔离：内部所有连接表的键是 ``(workspace_uuid, server_name)``，
    ``get_wrappers(workspace_uuid)`` 只返回该工作区的工具。
    """

    def __init__(self, connect_timeout: int = 60):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._connect_timeout = connect_timeout
        self._connect_thread_lock = threading.Lock()

        self._workspace_sigs: dict[str, dict[str, dict]] = {}  # ws -> 已连接配置签名
        self._warming: set[str] = set()  # 后台预热中的工作区
        self._server_sigs: dict[tuple[str, str], dict] = {}  # key -> 已应用配置签名
        self._connections: dict[tuple[str, str], Any] = {}  # key -> ClientSession
        self._stacks: dict[tuple[str, str], AsyncExitStack] = {}
        self._wrappers: dict[tuple[str, str], list[MCPToolWrapper]] = {}
        self._status: dict[tuple[str, str], str] = {}
        self._stop_events: dict[tuple[str, str], asyncio.Event] = {}
        self._conn_events: dict[tuple[str, str], asyncio.Event] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="mcp-provider", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + _LOOP_START_TIMEOUT_S
        while self._loop is None:
            if not self._thread.is_alive():
                raise RuntimeError("MCP provider 线程在 event loop 就绪前退出")
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"MCP provider event loop 在 {_LOOP_START_TIMEOUT_S}s 内未就绪"
                )
            time.sleep(0.01)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        loop.run_forever()
        self._loop = None

    def close(self) -> None:
        if self._loop is None or not self._loop.is_running():
            self._thread = None
            return
        try:
            self.run_in_loop(self._close_all(), timeout=self._connect_timeout)
        except Exception as exc:
            logger.warning(f"[MCP] 关闭连接异常: {exc}")
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._thread = None

    async def _close_all(self) -> None:
        for event in self._stop_events.values():
            event.set()
        tasks = [t for t in self._tasks.values() if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._stop_events.clear()
        self._conn_events.clear()
        self._tasks.clear()
        self._connections.clear()
        self._stacks.clear()
        self._wrappers.clear()
        self._server_sigs.clear()
        self._workspace_sigs.clear()
        self._warming.clear()
        self._status.clear()

    def run_in_loop(self, coro, timeout: float = 60) -> Any:
        """在后台 loop 中执行协程并同步等待结果。"""
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("MCP provider 事件循环未运行")
        if threading.current_thread() is self._thread:
            # 在 loop 线程内 run_coroutine_threadsafe(...).result() 会死锁
            raise RuntimeError("run_in_loop 不能在 MCP 事件循环线程内调用")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    # ── 连接 ──────────────────────────────────────────────────────────────

    def ensure_connected(self, workspace_uuid: str, servers: dict[str, MCPConfig]) -> None:
        """确保工作区的服务器按给定配置连接；签名无变化则 no-op。

        进程级串行化：并发调用（启动线程 / 首个会话重建）只允许一个连接流程，
        避免互相覆盖 _conn_events/_tasks 导致重复连接或等待超时。
        """
        with self._connect_thread_lock:
            if self._loop is None or not self._loop.is_running():
                try:
                    self.start()
                except RuntimeError as exc:
                    # MCP 是可选能力：启动失败降级为「本次无 MCP 工具」，不阻断会话
                    logger.warning(f"[MCP] provider 启动失败，本次跳过 MCP: {exc}")
                    return
            sig = {name: cfg.to_dict() for name, cfg in servers.items()}
            if self._workspace_sigs.get(workspace_uuid, {}) == sig:
                return
            try:
                self.run_in_loop(
                    self._connect_all(workspace_uuid, servers),
                    timeout=self._connect_timeout + 5,
                )
            except Exception as exc:
                logger.warning(f"[MCP] 连接失败（工作区 {workspace_uuid}）: {exc}")
                return
            # 全部连接成功才记签名；失败/超时的 server 留待下次 ensure_connected 重试
            if all(self._status.get((workspace_uuid, name)) == "connected" for name in servers):
                self._workspace_sigs[workspace_uuid] = sig
            else:
                logger.warning("[MCP] 部分服务器未连接，保留旧签名以便下次重试")

    def ensure_connected_async(self, workspace_uuid: str, servers: dict[str, MCPConfig]) -> None:
        """后台线程预热连接（供状态查询/设置页使用，不阻塞 HTTP 请求）。"""
        sig = {name: cfg.to_dict() for name, cfg in servers.items()}
        # 非阻塞拿锁：已有连接流程在跑时不排队（避免阻塞调用方的事件循环）
        if not self._connect_thread_lock.acquire(blocking=False):
            return
        try:
            if self._workspace_sigs.get(workspace_uuid, {}) == sig:
                return
            if workspace_uuid in self._warming:
                return
            self._warming.add(workspace_uuid)
            # 立即回写「连接中」，设置页状态不必等后台握手完成
            for name in servers:
                key = (workspace_uuid, name)
                if key not in self._connections:
                    self._status[key] = "connecting"
        finally:
            self._connect_thread_lock.release()

        def _run() -> None:
            try:
                self.ensure_connected(workspace_uuid, servers)
            finally:
                for name in servers:
                    key = (workspace_uuid, name)
                    if self._status.get(key) == "connecting" and key not in self._connections:
                        self._status[key] = "failed"
                with self._connect_thread_lock:
                    self._warming.discard(workspace_uuid)

        threading.Thread(
            target=_run, name=f"mcp-warm-{workspace_uuid or 'default'}", daemon=True
        ).start()

    async def _connect_all(self, workspace_uuid: str, servers: dict[str, MCPConfig]) -> None:
        # 关闭该工作区中已不在配置里的 server
        for key in list(self._connections):
            if key[0] == workspace_uuid and key[1] not in servers:
                await self._close_server(key)

        for name, cfg in servers.items():
            key = (workspace_uuid, name)
            cfg_sig = cfg.to_dict()
            if key in self._connections and self._server_sigs.get(key) != cfg_sig:
                await self._close_server(key)
            if key in self._connections:
                continue
            # 上一次连接超时后可能仍有残留任务：先取消，避免它覆盖新任务的簿记
            old_task = self._tasks.get(key)
            if old_task is not None and not old_task.done():
                old_task.cancel()
                await asyncio.gather(old_task, return_exceptions=True)
            self._status[key] = "connecting"
            self._stop_events[key] = asyncio.Event()
            self._conn_events[key] = asyncio.Event()
            self._tasks[key] = asyncio.create_task(self._connect_and_hold(key, cfg))

        # 有界等待连接完成（成功或失败）
        pending = [
            (workspace_uuid, name) for name in servers
            if (workspace_uuid, name) not in self._connections
        ]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[self._conn_events[key].wait() for key in pending]),
                    timeout=self._connect_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[MCP] 等待连接超时: {[key[1] for key in pending]}")

    async def _connect_and_hold(self, key: tuple[str, str], cfg: MCPConfig) -> None:
        """连接一个 server，注册 session/wrappers，然后保持连接直到关闭信号。"""
        workspace_uuid, name = key
        stack = AsyncExitStack()
        try:
            session = await self._open_session(name, cfg, stack)
            tools = await session.list_tools()
            wrappers = self._build_wrappers(workspace_uuid, name, cfg, tools.tools)
            self._connections[key] = session
            self._stacks[key] = stack
            self._wrappers[key] = wrappers
            self._server_sigs[key] = cfg.to_dict()
            self._status[key] = "connected"
            logger.info(f"[MCP] 服务器 {name}（工作区 {workspace_uuid}）已连接，{len(wrappers)} 个工具")
        except asyncio.CancelledError:
            await stack.aclose()
            self._tasks.pop(key, None)
            raise
        except Exception as exc:
            await stack.aclose()
            self._status[key] = "failed"
            self._conn_events[key].set()
            self._tasks.pop(key, None)
            logger.warning(f"[MCP] 服务器 {name}（工作区 {workspace_uuid}）连接失败: {exc}")
            return
        self._conn_events[key].set()
        try:
            await self._stop_events[key].wait()
        finally:
            await stack.aclose()
            self._connections.pop(key, None)
            self._wrappers.pop(key, None)
            self._server_sigs.pop(key, None)
            self._status[key] = "offline"
            self._tasks.pop(key, None)
            logger.info(f"[MCP] 服务器 {name}（工作区 {workspace_uuid}）已断开")

    async def _close_server(self, key: tuple[str, str]) -> None:
        event = self._stop_events.pop(key, None)
        if event is not None:
            event.set()
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        self._conn_events.pop(key, None)
        self._connections.pop(key, None)
        self._stacks.pop(key, None)
        self._wrappers.pop(key, None)
        self._server_sigs.pop(key, None)
        self._status.pop(key, None)

    async def _open_session(self, name: str, cfg: MCPConfig, stack: AsyncExitStack) -> Any:
        """建立 streamableHttp 传输 + ClientSession 并 initialize，返回 session。"""
        from mcp import ClientSession
        from mcp.client.streamable_http import httpx2, streamable_http_client

        if not cfg.url:
            raise ValueError(f"MCP server '{name}': streamableHttp 需要 url")
        # mcp SDK 内置 vendored 的 httpx2；headers 认证（Bearer/API Key）须经
        # http_client 传入。外部传入的 client 由本模块 stack 管理生命周期。
        http_client = await stack.enter_async_context(
            httpx2.AsyncClient(headers=cfg.headers or None)
        )
        read, write = await stack.enter_async_context(
            streamable_http_client(cfg.url, http_client=http_client)
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    def _build_wrappers(self, workspace_uuid: str, name: str, cfg: MCPConfig,
                        tool_defs: list[Any]) -> list[MCPToolWrapper]:
        from core.tools.mcp import MCPToolWrapper

        enabled = set(cfg.enabled_tools or ["*"])
        allow_all = "*" in enabled
        wrappers: list[MCPToolWrapper] = []
        for tool_def in tool_defs:
            raw_name = tool_def.name
            wrapped_name = _sanitize_mcp_tool_name(f"mcp_{name}_{raw_name}")
            if not allow_all and raw_name not in enabled and wrapped_name not in enabled:
                continue
            # mcp 1.x 用 inputSchema，2.x 改为 input_schema（snake_case）
            input_schema = (
                getattr(tool_def, "input_schema", None)
                or getattr(tool_def, "inputSchema", None)
                or {}
            )
            parameters = _normalize_schema(input_schema)
            wrappers.append(MCPToolWrapper(
                provider=self,
                workspace_uuid=workspace_uuid,
                server_name=name,
                tool_name=raw_name,
                description=getattr(tool_def, "description", None) or raw_name,
                parameters=parameters,
                tool_timeout=cfg.tool_timeout,
            ))
        # 排序：server 的 list_tools() 返回顺序不定，tools 数组顺序影响 prompt cache
        # 前缀的字节一致性（见 workspace 设计文档 §3.2）。
        wrappers.sort(key=lambda w: w.name)
        return wrappers

    # ── 工具执行 ──────────────────────────────────────────────────────────

    async def execute_tool(
        self, workspace_uuid: str, server_name: str, tool_name: str,
        kwargs: dict, timeout: int,
    ) -> ToolResult:
        """在 loop 内执行一次 MCP 工具调用（含超时与临时错误单次重试）。"""
        key = (workspace_uuid, server_name)
        session = self._connections.get(key)
        if session is None:
            return ToolResult(f"Error: MCP server '{server_name}' 未连接", error=True)

        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments=kwargs), timeout=timeout
            )
        except asyncio.TimeoutError:
            return ToolResult(f"Error: MCP tool 调用超时（{timeout} 秒）", error=True)
        except Exception as exc:
            if _is_transient(exc):
                logger.warning(f"[MCP] tool 调用遇临时错误 ({type(exc).__name__})，重试一次")
                await asyncio.sleep(1)
                # 等待期间可能已重连并替换 session：重试前重新取用（审查 P2）
                session = self._connections.get(key)
                if session is None:
                    return ToolResult(f"Error: MCP server '{server_name}' 已断开", error=True)
                try:
                    result = await asyncio.wait_for(
                        session.call_tool(tool_name, arguments=kwargs), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    return ToolResult(f"Error: MCP tool 调用超时（{timeout} 秒）", error=True)
                except Exception as exc2:
                    return ToolResult(f"Error: MCP tool 调用失败: {exc2}", error=True)
            else:
                return ToolResult(f"Error: MCP tool 调用失败: {exc}", error=True)

        text = _render_tool_result(result)
        return ToolResult(text, error=bool(getattr(result, "isError", False)))

    # ── 查询 / reload ─────────────────────────────────────────────────────

    def get_wrappers(self, workspace_uuid: str) -> list[MCPToolWrapper]:
        """返回指定工作区已连接 server 的 wrapper（同步，读共享列表）。

        按 name 排序：``_wrappers`` 的插入序 = 各 server 连接完成时序（``_connect_all``
        并发建连），跨重启不稳定，排序保证 tools 前缀字节一致（设计文档 §3.2）。
        """
        result: list[MCPToolWrapper] = []
        for key, wrappers in list(self._wrappers.items()):
            if key[0] == workspace_uuid:
                result.extend(wrappers)
        result.sort(key=lambda w: w.name)
        return result

    def status(self, workspace_uuid: str) -> dict[str, dict]:
        return {
            key[1]: {
                "status": self._status.get(key, "offline"),
                "tool_count": len(self._wrappers.get(key, [])),
            }
            for key in sorted(self._status)
            if key[0] == workspace_uuid
        }

    def server_tools(self, workspace_uuid: str, name: str) -> list[dict]:
        """返回某 server 已枚举的工具清单（名称 + 描述 + 参数 schema，规范 input_schema 键）。"""
        return [
            {
                "name": w.name,
                "description": w.description,
                "input_schema": w.parameters,
            }
            for w in self._wrappers.get((workspace_uuid, name), [])
        ]

    def test_connect(self, cfg: MCPConfig, timeout: int = 60) -> dict:
        """测试单个 server 连接并枚举工具，测完立即断开；不改变全局连接状态。

        返回 {"status": "connected", "tool_count", "tools"} 或
        {"status": "failed", "error"}。
        """
        if self._loop is None or not self._loop.is_running():
            self.start()
        try:
            return self.run_in_loop(self._test_connect_coro(cfg), timeout=timeout + 5)
        except TimeoutError:
            return {"status": "failed", "error": f"连接超时（{timeout} 秒）"}
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}

    async def _test_connect_coro(self, cfg: MCPConfig) -> dict:
        stack = AsyncExitStack()
        try:
            session = await self._open_session("_test", cfg, stack)
            tools = await session.list_tools()
            tool_list = [
                {
                    "name": t.name,
                    "description": getattr(t, "description", None) or t.name,
                    "input_schema": _normalize_schema(
                        getattr(t, "input_schema", None)
                        or getattr(t, "inputSchema", None)
                        or {}
                    ),
                }
                for t in tools.tools
            ]
            return {
                "status": "connected",
                "tool_count": len(tool_list),
                "tools": tool_list,
            }
        finally:
            await stack.aclose()

    def reload(self, workspace_uuid: str, servers: dict[str, MCPConfig],
               force: bool = False) -> None:
        """按新配置重连该工作区。force=True 时先断开其全部现有连接。"""
        if force:
            self._workspace_sigs.pop(workspace_uuid, None)
            try:
                if self._loop is not None and self._loop.is_running():
                    self.run_in_loop(
                        self._disconnect_workspace(workspace_uuid),
                        timeout=self._connect_timeout + 5,
                    )
            except Exception as exc:
                logger.warning(f"[MCP] 断开现有连接失败（工作区 {workspace_uuid}）: {exc}")
        self.ensure_connected(workspace_uuid, servers)

    async def _disconnect_workspace(self, workspace_uuid: str) -> None:
        for key in [k for k in self._connections if k[0] == workspace_uuid]:
            await self._close_server(key)


# ── 模块级单例（仿 BrowserService.get_service 双重检查锁）─────────────────

_provider: MCPProvider | None = None
_provider_lock = threading.Lock()


def get_provider() -> MCPProvider:
    """获取全局 MCPProvider 单例；首次调用自动启动后台 loop。"""
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                provider = MCPProvider()
                try:
                    provider.start()
                except RuntimeError as exc:
                    # 启动失败不抛出：ensure_connected 会再次尝试并降级为「本次无 MCP」
                    logger.warning(f"[MCP] provider 启动失败: {exc}")
                _provider = provider
    return _provider


def stop_mcp_provider() -> None:
    global _provider
    with _provider_lock:
        if _provider is not None:
            _provider.close()
            _provider = None
