"""邮件斜杠指令（共享骨架见 ``interfaces/common/commands.py``）。

与 QQ/微信同一套：``/h /w /s /new /mode /status /stop``。指令在**来信正文首行**
以 ``/`` 开头时触发，回信即指令回复。
"""

from __future__ import annotations

from interfaces.common.commands import Command, CommandContext, parse
from interfaces.common.commands import build_commands as _build_commands
from interfaces.common.commands import dispatch as _dispatch
from interfaces.common.commands import unique_commands as _unique_commands
from interfaces.email.runtime import EmailRuntime
from interfaces.email.state import (
    EmailAccountState,
    ignored_summary,
    resolve_current,
    save_account_state,
)

__all__ = [
    "Command",
    "CommandContext",
    "build_commands",
    "dispatch",
    "is_command",
    "parse",
    "unique_commands",
]


def is_command(text: str) -> bool:
    """首行以 ``/`` 开头才算指令（回信里的引用内容不参与指令解析）。"""
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return first.startswith("/")


def _status_owner_lines(ctx: CommandContext) -> list[str]:
    email = getattr(ctx.runtime.cfg, "email_address", "")
    return [f"- 邮箱：{email}"] if email else []


def _status_footer(ctx: CommandContext) -> list[str]:
    account_id = getattr(ctx.runtime, "account_id", "")
    if not account_id:
        return []
    summary = ignored_summary(account_id)
    if not summary.get("total"):
        return ["- 最近 24h 拦截：0"]
    return [f"- 最近 24h 拦截：{summary['total']}（用设置页查看明细）"]


def build_commands() -> dict[str, Command]:
    return _build_commands(
        channel="email",
        session_title="邮件会话",
        status_owner_label="最近来信",
        status_owner_lines=_status_owner_lines,
        status_footer_lines=_status_footer,
        save_state=save_account_state,
    )


def unique_commands() -> list[Command]:
    return _unique_commands(build_commands())


async def dispatch(
    runtime: EmailRuntime, state: EmailAccountState, chat_id: str, text: str,
) -> str | None:
    """执行一条指令，返回回信正文；不是已知指令则返回 None。"""
    return await _dispatch(
        runtime, state, chat_id, text,
        channel="email",
        build=build_commands, resolve_current=resolve_current,
    )
