"""MCP 服务器支持：把 MCP 工具桥接为 Cili 的同步 Tool。

MCP SDK 是全异步的，而 Cili 工具层是纯同步（agent 跑后台线程、Tool.execute
同步）。本模块用"方案 A"桥接：一个全局单例 MCPProvider 内部跑常驻后台线程 +
asyncio event loop，同步 execute() 通过 asyncio.run_coroutine_threadsafe 提交
MCP 调用并等待结果——与 BrowserService（core/browser_service.py）把 async 的
Playwright 接进同步工具层的范式一致。

裁剪自 nanobot 的 mcp.py（reference/nanobot/.../mcp.py），去掉 resources/prompts/
OAuth/SSE/图片落盘/复杂自动重连，保留：工具名 sanitize、schema 归一化、
Windows stdio 命令包装、临时错误重试、超时。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import threading
import time
import urllib.parse
from contextlib import AsyncExitStack
from typing import Any

from core.config import MCPConfig
from core.tools.base import (
    Tool,
    ToolResult,
    UNTRUSTED_DATA_BEGIN,
    UNTRUSTED_DATA_END,
)

logger = logging.getLogger(__name__)

# 临时连接错误：单次重试。通常是 MCP server 重启或网络瞬时中断。
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

# Windows 下需要 cmd /d /c 包装的 shell 启动器（Cili 是 Windows-only）
_WINDOWS_SHELL_LAUNCHERS: frozenset[str] = frozenset(("npx", "npm", "pnpm", "yarn", "bunx"))

# 模型 API 只接受 [a-zA-Z0-9_-]，其余替换为下划线并压缩连续下划线
_SANITIZE_RE = re.compile(r"_+")
_MAX_TOOL_NAME_LENGTH = 64
_HASH_LENGTH = 8


def _sanitize_name(name: str) -> str:
    return _SANITIZE_RE.sub("_", re.sub(r"[^a-zA-Z0-9_-]", "_", name))


def _limit_tool_name(name: str, max_length: int = _MAX_TOOL_NAME_LENGTH) -> str:
    """限长工具名：超长时用 sha1 哈希后缀替代，保证唯一。"""
    if len(name) <= max_length:
        return name
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    return f"{name[:max_length - _HASH_LENGTH - 1]}_{digest}"


def _sanitize_mcp_tool_name(name: str) -> str:
    return _limit_tool_name(_sanitize_name(name))


def _is_transient(exc: BaseException) -> bool:
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


# ── Windows stdio 命令包装 ─────────────────────────────────────────────────

def _windows_command_basename(command: str) -> str:
    return command.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()


def _normalize_windows_stdio_command(
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
) -> tuple[str, list[str], dict[str, str] | None]:
    """Windows 下把 npx/npm/.cmd/.bat 等包装成 cmd /d /c 启动，保证 stdio server 可靠拉起。"""
    normalized_args = list(args or [])
    if os.name != "nt":
        return command, normalized_args, env

    basename = _windows_command_basename(command)
    if basename in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return command, normalized_args, env
    if basename.endswith((".exe", ".com")):
        return command, normalized_args, env

    resolved = shutil.which(command, path=(env or {}).get("PATH")) or command
    resolved_basename = _windows_command_basename(resolved)
    should_wrap = (
        basename in _WINDOWS_SHELL_LAUNCHERS
        or basename.endswith((".cmd", ".bat"))
        or resolved_basename.endswith((".cmd", ".bat"))
    )
    if not should_wrap:
        return command, normalized_args, env

    comspec = (env or {}).get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    return comspec, ["/d", "/c", command, *normalized_args], env


# ── MCP inputSchema 归一化（JSON Schema → 模型 API 兼容）──────────────────

def _extract_nullable_branch(options: Any) -> tuple[dict[str, Any], bool] | None:
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
        return non_null[0], True
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
                    generated_defs[name] = {}
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
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
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


def _normalize_schema_for_openai(schema: Any) -> dict[str, Any]:
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


# ── MCP 工具包装器 ─────────────────────────────────────────────────────────

class MCPToolWrapper(Tool):
    """把单个 MCP 工具包装为 Cili 的同步 Tool。

    关键设计：wrapper 只持 provider 引用、不持 session。每次 execute 经
    provider 取当前 session，因此 server 重连后 wrapper 自动使用新连接。
    """

    def __init__(
        self,
        provider: "MCPProvider",
        server_name: str,
        tool_name: str,
        description: str,
        parameters: dict,
        tool_timeout: int = 30,
    ):
        super().__init__(cwd=".")
        self._provider = provider
        self._server_name = server_name
        self._tool_name = tool_name  # 原始 MCP 工具名（call_tool 用）
        self._tool_timeout = tool_timeout
        self.name = _sanitize_mcp_tool_name(f"mcp_{server_name}_{tool_name}")
        self.description = description or tool_name
        self.parameters = parameters or {"type": "object", "properties": {}}

    def execute(self, **kwargs: Any) -> ToolResult:
        try:
            result = self._provider.run_in_loop(
                self._provider.execute_tool(
                    self._server_name, self._tool_name, kwargs, self._tool_timeout
                ),
                timeout=self._tool_timeout + 10,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                f"Error: MCP tool '{self.name}' 调用超时（{self._tool_timeout} 秒）",
                error=True,
            )
        except Exception as exc:
            return ToolResult(f"Error: MCP tool '{self.name}' 调用失败: {exc}", error=True)

        if result.is_error:
            return result
        # 提示注入防护（SEC-18）：MCP 结果视为外部不可信数据
        return ToolResult(
            output=f"{UNTRUSTED_DATA_BEGIN}{result.output}{UNTRUSTED_DATA_END}"
        )


# ── MCP 连接管理器 ─────────────────────────────────────────────────────────

class MCPProvider:
    """全局单例：管理后台 asyncio loop + 每 server 的 ClientSession 生命周期。

    后台线程跑一个常驻 asyncio event loop（与 BrowserService 的 worker 线程
    范式一致）。同步线程通过 run_in_loop 提交协程并阻塞等待。
    """

    def __init__(self, connect_timeout: int = 60):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._connect_timeout = connect_timeout

        self._configured_sig: dict[str, dict] | None = None  # 当前已连接的配置签名
        self._connections: dict[str, Any] = {}  # server_name -> ClientSession
        self._stacks: dict[str, AsyncExitStack] = {}
        self._wrappers: dict[str, list[MCPToolWrapper]] = {}
        self._status: dict[str, str] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._conn_events: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name="mcp-provider", daemon=True)
        self._thread.start()
        while self._loop is None:
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

    def ensure_connected(self, servers: dict[str, MCPConfig]) -> None:
        """确保按给定配置连接；配置签名无变化则 no-op（避免 agent 每次 rebuild 重连）。"""
        if self._loop is None or not self._loop.is_running():
            self.start()
        sig = {name: cfg.to_dict() for name, cfg in servers.items()}
        if self._configured_sig == sig:
            return
        self._configured_sig = sig
        try:
            self.run_in_loop(self._connect_all(servers), timeout=self._connect_timeout + 5)
        except Exception as exc:
            logger.warning(f"[MCP] 连接失败: {exc}")

    async def _connect_all(self, servers: dict[str, MCPConfig]) -> None:
        # 关闭已不在配置里的 server
        for name in list(self._connections):
            if name not in servers:
                await self._close_server(name)

        for name, cfg in servers.items():
            if name in self._connections:
                continue
            self._status[name] = "connecting"
            self._stop_events[name] = asyncio.Event()
            self._conn_events[name] = asyncio.Event()
            self._tasks[name] = asyncio.create_task(self._connect_and_hold(name, cfg))

        # 有界等待连接完成（成功或失败）
        pending = [name for name in servers if name not in self._connections]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[self._conn_events[name].wait() for name in pending]),
                    timeout=self._connect_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[MCP] 等待连接超时: {pending}")

    async def _connect_and_hold(self, name: str, cfg: MCPConfig) -> None:
        """连接一个 server，注册 session/wrappers，然后保持连接直到关闭信号。"""
        stack = AsyncExitStack()
        try:
            session = await self._open_session(name, cfg, stack)
            tools = await session.list_tools()
            wrappers = self._build_wrappers(name, cfg, tools.tools)
            self._connections[name] = session
            self._stacks[name] = stack
            self._wrappers[name] = wrappers
            self._status[name] = "connected"
            logger.info(f"[MCP] 服务器 {name} 已连接，{len(wrappers)} 个工具")
        except Exception as exc:
            await stack.aclose()
            self._status[name] = "failed"
            self._conn_events[name].set()
            self._tasks.pop(name, None)
            logger.warning(f"[MCP] 服务器 {name} 连接失败: {exc}")
            return
        self._conn_events[name].set()
        try:
            await self._stop_events[name].wait()
        finally:
            await stack.aclose()
            self._connections.pop(name, None)
            self._wrappers.pop(name, None)
            self._status[name] = "offline"
            self._tasks.pop(name, None)
            logger.info(f"[MCP] 服务器 {name} 已断开")

    async def _close_server(self, name: str) -> None:
        event = self._stop_events.pop(name, None)
        if event is not None:
            event.set()
        task = self._tasks.pop(name, None)
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        self._conn_events.pop(name, None)
        self._connections.pop(name, None)
        self._stacks.pop(name, None)
        self._wrappers.pop(name, None)
        self._status.pop(name, None)

    async def _open_session(self, name: str, cfg: MCPConfig, stack: AsyncExitStack) -> Any:
        """建立传输 + ClientSession 并 initialize，返回 session。"""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        transport_type = cfg.type
        if not transport_type:
            if cfg.command:
                transport_type = "stdio"
            elif cfg.url:
                transport_type = "streamableHttp"
            else:
                raise ValueError(f"MCP server '{name}': 未配置 command 或 url")

        if transport_type == "stdio":
            if not cfg.command:
                raise ValueError(f"MCP server '{name}': stdio 需要 command")
            command, args, env = _normalize_windows_stdio_command(cfg.command, cfg.args, cfg.env)
            params = StdioServerParameters(
                command=command, args=args, env=env or None, cwd=cfg.cwd or None,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        elif transport_type == "streamableHttp":
            if not cfg.url:
                raise ValueError(f"MCP server '{name}': streamableHttp 需要 url")
            # mcp SDK 内置 vendored 的 httpx2；headers 认证（Bearer/API Key）须经
            # http_client 传入。外部传入的 client 由本模块 stack 管理生命周期。
            from mcp.client.streamable_http import httpx2

            http_client = await stack.enter_async_context(
                httpx2.AsyncClient(headers=cfg.headers or None)
            )
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(cfg.url, http_client=http_client)
            )
        else:
            raise ValueError(f"MCP server '{name}': 未知传输类型 {transport_type!r}")

        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    def _build_wrappers(self, name: str, cfg: MCPConfig, tool_defs: list[Any]) -> list[MCPToolWrapper]:
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
            parameters = _normalize_schema_for_openai(input_schema)
            wrappers.append(MCPToolWrapper(
                provider=self,
                server_name=name,
                tool_name=raw_name,
                description=getattr(tool_def, "description", None) or raw_name,
                parameters=parameters,
                tool_timeout=cfg.tool_timeout,
            ))
        return wrappers

    # ── 工具执行 ──────────────────────────────────────────────────────────

    async def execute_tool(
        self, server_name: str, tool_name: str, kwargs: dict, timeout: int
    ) -> ToolResult:
        """在 loop 内执行一次 MCP 工具调用（含超时与临时错误单次重试）。"""
        session = self._connections.get(server_name)
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

    def get_wrappers(self) -> list[MCPToolWrapper]:
        """返回当前所有已连接 server 的 wrapper（同步，读共享列表）。"""
        result: list[MCPToolWrapper] = []
        for wrappers in list(self._wrappers.values()):
            result.extend(wrappers)
        return result

    def status(self) -> dict[str, dict]:
        return {
            name: {
                "status": self._status.get(name, "offline"),
                "tool_count": len(self._wrappers.get(name, [])),
            }
            for name in sorted(self._status)
        }

    def reload(self, servers: dict[str, MCPConfig], force: bool = False) -> None:
        """按新配置重连。force=True 时断开全部现有连接后重新连接。"""
        if force:
            self._configured_sig = None
            try:
                self.run_in_loop(self._disconnect_all(), timeout=self._connect_timeout + 5)
            except Exception as exc:
                logger.warning(f"[MCP] 断开现有连接失败: {exc}")
        self.ensure_connected(servers)

    async def _disconnect_all(self) -> None:
        for name in list(self._connections):
            await self._close_server(name)


# ── 模块级单例（仿 BrowserService.get_service 双重检查锁）─────────────────

_provider: MCPProvider | None = None
_provider_lock = threading.Lock()


def get_provider() -> MCPProvider:
    """获取全局 MCPProvider 单例；首次调用自动启动后台 loop。"""
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                _provider = MCPProvider()
                _provider.start()
    return _provider


def stop_mcp_provider() -> None:
    global _provider
    with _provider_lock:
        if _provider is not None:
            _provider.close()
            _provider = None
