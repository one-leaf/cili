"""接入端共享斜杠指令骨架：``/h`` ``/w`` ``/s`` ``/new`` ``/status`` ``/stop``。

QQ / 微信的指令表与大部分 handler 完全一致；本模块提供通用实现，
各接入端只注入会话标题、Owner 标签和 /status 的专属行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.config import (
    find_workspace_entry,
    get_workspace_data_dir,
    load_workspaces_index,
)
from core.session import SessionStore
from core.session_registry import registry, session_key

MAX_LIST_ITEMS = 20   # 列表最多展示多少条，避免刷屏

LOCKED_WORKSPACE_REPLY = (
    "⚠️ 本账号已锁定工作空间，不支持切换。如需切换，请到设置页取消「锁定工作空间」。"
)


@dataclass
class CommandContext:
    """指令执行上下文。``state`` 是账号切片，``chat`` 是当前聊天的切片。"""

    runtime: Any
    state: Any
    chat: Any
    chat_id: str = ""
    args: str = ""


@dataclass
class Command:
    """一条指令（声明式）。``aliases`` 是等价写法，指同一个 handler。"""

    name: str
    description: str
    usage: str
    handler: Callable[[CommandContext], "str | Awaitable[str]"]
    aliases: tuple[str, ...] = ()


# ─── 工具函数 ─────────────────────────────────────────────────────────


def _workspaces() -> list[dict]:
    """列出可选工作区（排除 system）。"""
    out = []
    for entry in load_workspaces_index():
        uuid = entry.get("uuid")
        if uuid and uuid != "system":
            out.append(entry)
    return out


def _sessions(workspace_uuid: str) -> list[dict]:
    sessions_dir = get_workspace_data_dir(workspace_uuid) / "sessions"
    return SessionStore.list_sessions(sessions_dir)


def _pick(items: list[dict], token: str, key: str) -> dict | None:
    """按「序号（1 起）/ 精确 id / 名称子串」挑一项。"""
    if not token:
        return None
    if token.isdigit():
        idx = int(token) - 1
        return items[idx] if 0 <= idx < len(items) else None
    for it in items:
        if str(it.get(key) or "") == token:
            return it
    lowered = token.lower()
    for it in items:
        name = str(it.get("workspace_name") or it.get("name") or "")
        if lowered in name.lower():
            return it
    return None


def _session_label(workspace_uuid: str, session_id: str) -> str:
    """会话的展示名（与 Web 端同源；查不到就退回 id）。"""
    if not session_id:
        return "(无会话)"
    for s in _sessions(workspace_uuid):
        if str(s.get("session_id") or "") == session_id:
            return str(s.get("display_name") or s.get("name") or session_id)
    return session_id


def _current_label(chat) -> str:
    """「工作区 / 会话」的展示串。不显示任何 id。"""
    entry = find_workspace_entry(chat.workspace_uuid) or {}
    ws_name = entry.get("workspace_name") or "(未设置)"
    return f"{ws_name} / {_session_label(chat.workspace_uuid, chat.session_id)}"


# ─── 各指令 ───────────────────────────────────────────────────────────


def _cmd_workspace(ctx: CommandContext, save_state) -> str:
    if ctx.runtime.cfg.workspace_locked:
        return LOCKED_WORKSPACE_REPLY
    from core.teen_mode import get_teen_mode

    teen = get_teen_mode()
    items = _workspaces()
    if not items:
        return "没有可用工作区。"

    if not ctx.args:
        lines = ["**工作区列表**", ""]
        for i, entry in enumerate(items[:MAX_LIST_ITEMS], start=1):
            name = entry.get("workspace_name") or "(未命名)"
            uuid = str(entry.get("uuid") or "")
            restricted = teen.workspace_access(uuid) == "locked"
            directory = "(已锁定)" if restricted else (entry.get("directory") or "(未知目录)")
            mark = " ✅" if uuid == ctx.chat.workspace_uuid else ""
            if restricted:
                mark += " 🔒"
            lines.append(f"{i}. **{name}**{mark}")
            lines.append(f"    `{directory}`")
        lines += ["", f"当前：{_current_label(ctx.chat)}", "用 `/w <序号>` 切换。"]
        return "\n".join(lines)

    picked = _pick(items, ctx.args, "uuid")
    if not picked:
        return f"找不到工作区：{ctx.args}"
    if teen.workspace_access(str(picked.get("uuid") or "")) == "locked":
        return "🔒 该工作区已锁定，需家长解锁后才能使用。"
    ctx.chat.workspace_uuid = str(picked.get("uuid") or "")
    # 延迟导入避免与 state 模块形成循环（state 不依赖 commands，但保持惰性更稳）
    from interfaces.common.state import latest_session_id
    ctx.chat.session_id = latest_session_id(ctx.chat.workspace_uuid)
    save_state(ctx.runtime.account_id, ctx.state)
    return f"已切换工作区 → {_current_label(ctx.chat)}"


def _cmd_session(ctx: CommandContext, save_state) -> str:
    if not ctx.chat.workspace_uuid:
        return "尚未设置工作区，先执行 `/w`。"
    items = _sessions(ctx.chat.workspace_uuid)

    if not ctx.args:
        if not items:
            return "当前工作区还没有会话，用 `/new` 新建。"
        lines = ["**会话列表**", ""]
        for i, s in enumerate(items[:MAX_LIST_ITEMS], start=1):
            sid = str(s.get("session_id") or "")
            name = s.get("display_name") or s.get("name") or sid
            mark = " ✅" if sid == ctx.chat.session_id else ""
            lines.append(f"{i}. {name}{mark}")
        lines += ["", "用 `/s <序号>` 切换。"]
        return "\n".join(lines)

    # 只按序号切换：列表不显示 id，再让用户敲 id 是自相矛盾的
    if not ctx.args.isdigit():
        return "请用列表里的**序号**切换，例如 `/s 2`。"
    idx = int(ctx.args) - 1
    if not (0 <= idx < len(items)):
        return f"序号超出范围：{ctx.args}（当前共 {len(items)} 个会话）"
    picked = items[idx]
    ctx.chat.session_id = str(picked.get("session_id") or "")
    save_state(ctx.runtime.account_id, ctx.state)
    name = picked.get("display_name") or picked.get("name") or ctx.chat.session_id
    return f"已切换会话 → {name}"


def _cmd_new(ctx: CommandContext, session_title: str, save_state) -> str:
    if not ctx.chat.workspace_uuid:
        return "尚未设置工作区，先执行 `/w`。"
    name = ctx.args or session_title
    sessions_dir = get_workspace_data_dir(ctx.chat.workspace_uuid) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore.create_new_session(sessions_dir, name)
    ctx.chat.session_id = store.session_id
    save_state(ctx.runtime.account_id, ctx.state)
    return f"已新建并切换到会话：{name}  `{store.session_id[:8]}`"


def _cmd_status(
    ctx: CommandContext,
    *,
    owner_label: str,
    owner_lines: Callable[[CommandContext], list[str]] | None = None,
    footer_lines: Callable[[CommandContext], list[str]] | None = None,
) -> str:
    lines = ["**当前状态**", "", f"- 工作区/会话：{_current_label(ctx.chat)}"]
    if ctx.runtime.cfg.workspace_locked:
        lines.append("- 工作空间：已锁定（/w 不可用）")
    owner = ctx.state.owner or "(未绑定)"
    lines.append(f"- {owner_label}：{owner}")
    entry = find_workspace_entry(ctx.chat.workspace_uuid) or {}
    lines.append(f"- 目录：{entry.get('directory') or '(未知)'}")
    if owner_lines:
        lines.extend(owner_lines(ctx))

    runner = registry.get(session_key(ctx.chat.workspace_uuid, ctx.chat.session_id))
    if runner is None:
        lines.append("- 运行中：否（runner 未加载）")
    else:
        lines.append(f"- 运行中：{'是' if runner.is_running() else '否'}")
        try:
            usage = runner.get_usage()
            lines.append(f"- API 调用：{usage.get('api_calls', 0)}")
            lines.append(f"- 输入 tokens：{usage.get('input_tokens', 0):,}")
            lines.append(f"- 输出 tokens：{usage.get('output_tokens', 0):,}")
        except Exception as e:  # noqa: BLE001 — 状态展示不该因统计失败而中断
            lines.append(f"- 用量统计不可用：{e}")

    if footer_lines:
        lines.extend(footer_lines(ctx))
    return "\n".join(lines)


def _cmd_stop(ctx: CommandContext) -> str:
    runner = registry.get(session_key(ctx.chat.workspace_uuid, ctx.chat.session_id))
    if runner is None or not runner.is_running():
        return "当前没有正在执行的任务。"
    runner.stop()
    return "已发送停止信号，当前回合将在下一步中断。"


def _unique(table: dict[str, Command]) -> list[Command]:
    seen: list[Command] = []
    for c in table.values():
        if not any(c is x for x in seen):
            seen.append(c)
    return seen


# ─── 注册表 / 分发 ────────────────────────────────────────────────────


def build_commands(
    *,
    session_title: str,
    status_owner_label: str,
    save_state,
    status_owner_lines: Callable[[CommandContext], list[str]] | None = None,
    status_footer_lines: Callable[[CommandContext], list[str]] | None = None,
) -> dict[str, Command]:
    """构建指令表：键含别名，别名指向同一个 ``Command`` 对象。"""
    table: dict[str, Command] = {}

    def _help(ctx: CommandContext) -> str:
        lines = ["**可用指令**", ""]
        locked = ctx.runtime.cfg.workspace_locked
        for cmd in _unique(table):
            usage = cmd.usage
            if locked and cmd.name == "w":
                usage += "（本账号已锁定）"
            lines.append(f"- `{usage}` — {cmd.description}")
        lines += ["", "直接发消息即与 AI 对话。"]
        return "\n".join(lines)

    commands = [
        Command("h", "显示本帮助", "/h", _help, aliases=("help",)),
        Command("w", "查看/切换工作区", "/w [序号|名称]", lambda ctx: _cmd_workspace(ctx, save_state), aliases=("workspace",)),
        Command("s", "查看/切换会话", "/s <序号>", lambda ctx: _cmd_session(ctx, save_state), aliases=("session",)),
        Command("new", "在当前工作区新建会话", "/new [名称]",
                lambda ctx: _cmd_new(ctx, session_title, save_state)),
        Command("status", "显示当前工作区/会话与用量", "/status",
                lambda ctx: _cmd_status(
                    ctx, owner_label=status_owner_label,
                    owner_lines=status_owner_lines,
                    footer_lines=status_footer_lines,
                )),
        Command("stop", "停止当前正在执行的回合", "/stop", _cmd_stop),
    ]
    for c in commands:
        table[c.name] = c
        for alias in c.aliases:
            table[alias] = c
    return table


def unique_commands(table: dict[str, Command]) -> list[Command]:
    """按定义顺序去重的指令列表（供 /h 展示，不把别名重复列一遍）。"""
    return _unique(table)


def parse(text: str) -> tuple[str, str] | None:
    """解析 ``/cmd args``；不是指令则返回 None。"""
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    body = stripped[1:]
    name, _, args = body.partition(" ")
    return name.strip().lower(), args.strip()


async def dispatch(
    runtime,
    state,
    chat_id: str,
    text: str,
    *,
    build: Callable[[], dict[str, Command]],
    resolve_current: Callable[[Any, str, Any], Any],
) -> str | None:
    """执行一条指令，返回要回给用户的文本；不是已知指令则返回 None。"""
    parsed = parse(text)
    if parsed is None:
        return None
    name, args = parsed
    cmd = build().get(name)
    if cmd is None:
        return f"未知指令：`/{name}`。用 `/h` 查看可用指令。"

    chat = resolve_current(state, chat_id, runtime.cfg)
    ctx = CommandContext(runtime=runtime, state=state, chat=chat, chat_id=chat_id, args=args)
    result: Any = cmd.handler(ctx)
    if hasattr(result, "__await__"):
        result = await result
    return str(result)
