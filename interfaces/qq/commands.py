"""QQ 斜杠指令（共享骨架见 ``interfaces/common/commands.py``）。

QQ 侧只差 /new 的默认会话名与 /status 的「被动回复配额」行；
其余指令、别名和解析逻辑全部复用共享实现。
"""

from __future__ import annotations

from interfaces.common.commands import (
    LOCKED_WORKSPACE_REPLY,
    MAX_LIST_ITEMS,
    Command,
    CommandContext,
    parse,
)
from interfaces.common.commands import (
    build_commands as _build_commands,
)
from interfaces.common.commands import (
    dispatch as _dispatch,
)
from interfaces.common.commands import (
    unique_commands as _unique_commands,
)
from interfaces.qq.runtime import QQRuntime
from interfaces.qq.state import QQAccountState, resolve_current, save_account_state

__all__ = [
    "Command",
    "CommandContext",
    "LOCKED_WORKSPACE_REPLY",
    "MAX_LIST_ITEMS",
    "build_commands",
    "dispatch",
    "parse",
    "unique_commands",
]


def _status_footer(ctx: CommandContext) -> list[str]:
    return [f"- 被动回复配额：每条消息 {ctx.runtime.limiter.limit} 次"]


def build_commands() -> dict[str, Command]:
    """构建指令表：键含别名，别名指向同一个 ``Command`` 对象。"""
    return _build_commands(
        session_title="QQ 会话",
        status_owner_label="Owner OpenID",
        status_footer_lines=_status_footer,
        save_state=save_account_state,
    )


def unique_commands() -> list[Command]:
    """按定义顺序去重的指令列表（供 /h 展示）。"""
    return _unique_commands(build_commands())


async def dispatch(
    runtime: QQRuntime, state: QQAccountState, chat_id: str, text: str,
) -> str | None:
    """执行一条指令，返回要回给用户的文本；不是已知指令则返回 None。"""
    return await _dispatch(
        runtime, state, chat_id, text,
        build=build_commands, resolve_current=resolve_current,
    )
