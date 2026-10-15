"""TUI 客户端本地命令（方案 §九）：/h /s /new /status /stop /quit。

与 QQ 接入端的 commands.py 同一组织方式：每条指令一个 handler，声明式注册。
差异：workspace 锁定 cwd，**无 /w**；/status 不本地实现——作为消息发给服务端
（服务端把 "/status" 识别为文本命令并返回 usage 报告的 SSE 流），渲染路径与
普通回合完全一致。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

HELP_TEXT = """**命令：**

- `/h` — 显示本帮助
- `/s [序号]` — 列出/切换本 workspace 的最近会话
- `/new` — 新建会话
- `/status` — 当前会话状态（上下文与用量，服务端返回）
- `/stop` — 停止当前正在执行的回合（回合中也可按 Ctrl+C）
- `/quit` — 退出（Ctrl+D 等价）

**回合中：** Ctrl+C 请求停止；Markdown 与工具轨迹自动渲染。
workspace 锁定启动目录，切换工作区请到目标目录重新运行 cli.cmd。"""


@dataclass
class Command:
    name: str
    description: str
    handler: Callable[[], str | None]
    aliases: tuple[str, ...] = ()


def build_commands(app) -> dict[str, Command]:
    """构建指令表（键含别名）。handler 闭包绑定 TuiApp 实例。"""

    def cmd_help() -> str | None:
        app.renderer.submit_markdown(HELP_TEXT)
        return None

    def cmd_session() -> str | None:
        sessions = app.client.list_sessions(app.workspace_uuid)
        if not sessions:
            app.renderer.status_line("该 workspace 还没有会话。")
            return None
        for idx, s in enumerate(sessions[:10], start=1):
            name = s.get("name") or "Unnamed"
            preview = (s.get("preview") or "").replace("\n", " ")[:40]
            app.renderer.console.print(f"  {idx}. {name}  {preview}", style="dim")
        app.renderer.status_line("/s <序号> 切换会话")
        return None

    def cmd_new() -> str | None:
        session = app.client.create_session(app.workspace_uuid)
        app.set_session(session)
        return None

    def cmd_stop() -> str | None:
        app.stop_turn()
        return None

    def cmd_quit() -> str | None:
        app.request_quit()
        return None

    commands = [
        Command("h", "显示本帮助", cmd_help, aliases=("help",)),
        Command("s", "查看/切换会话", cmd_session, aliases=("session",)),
        Command("new", "新建会话", cmd_new),
        Command("stop", "停止当前回合", cmd_stop),
        Command("quit", "退出", cmd_quit, aliases=("q", "exit")),
    ]
    table: dict[str, Command] = {}
    for c in commands:
        table[c.name] = c
        for alias in c.aliases:
            table[alias] = c
    return table


def build_completer_words() -> list[str]:
    """/ 前缀补全词表（M2 的 completer 用，先收敛在一处）。"""
    return ["/h", "/help", "/s", "/new", "/status", "/stop", "/quit"]
