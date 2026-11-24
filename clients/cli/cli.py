"""TUI 客户端命令行入口（cli.cmd 的实现体）。

入口链：``cli.cmd`` → ``python -m clients.cli``（``__main__.py``）→ :func:`main`。
客户端与服务端入口**物理分离**：``main.py`` / ``start.cmd`` 只服务 Web 服务端，
本包不 import 任何 ``core/*`` 模块（``profile.py`` 除外，且仅服务端 import）。

启动语义：**cwd 即 workspace**——在哪个目录执行 ``cli.cmd``，该目录就是当前工作区
（按路径匹配服务端已注册 workspace，未命中自动注册）；不支持切换 workspace。
"""

from __future__ import annotations

import argparse
import sys

from clients.cli.app import TuiApp
from clients.cli.client import CiliHttpClient
from clients.cli.discovery import discover_config
from clients.cli.render import Renderer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.cmd",
        description="Cili terminal client - connect to a running Cili server",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "-c",
        "--continue",
        dest="mode",
        action="store_const",
        const="latest",
        help="attach to the most recent session of the resolved workspace",
    )
    mode_group.add_argument(
        "--new",
        dest="mode",
        action="store_const",
        const="new",
        help="always start a new CLI session",
    )
    parser.set_defaults(mode="dedicated")  # 默认：续本通道自己的独立会话
    parser.add_argument("--session", metavar="ID", help="attach to a specific session id")
    parser.add_argument("--server", metavar="URL", help="server base url (default: auto-discover)")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not sys.stdout.isatty():
        print("cili TUI 需要交互终端（stdout 不是 TTY）。请直接在终端运行 cli.cmd。", file=sys.stderr)
        return 2

    config = discover_config(args.server)
    client = CiliHttpClient(config.base_url, config.token)
    renderer = Renderer(no_color=args.no_color)
    app = TuiApp(
        client,
        renderer,
        mode=args.mode,
        session_id=args.session,
    )
    try:
        return app.run()
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
