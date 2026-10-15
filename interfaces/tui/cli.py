"""TUI 客户端命令行入口（cli.cmd 的实现体）。

入口链：``cli.cmd`` → ``python -m interfaces.tui``（``__main__.py``）→ :func:`main`。
客户端与服务端入口**物理分离**：``main.py`` / ``start.cmd`` 只服务 Web 服务端，
本包不 import 任何 ``core/*`` 模块（``profile.py`` 除外，且仅服务端 import）。

启动语义：**cwd 即 workspace**——在哪个目录执行 ``cli.cmd``，该目录就是当前工作区
（按路径匹配服务端已注册 workspace，未命中自动注册）；不支持切换 workspace。
"""

from __future__ import annotations

import argparse
import sys

from interfaces.tui.app import TuiApp
from interfaces.tui.client import CiliClient
from interfaces.tui.config import discover_config
from interfaces.tui.render import Renderer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.cmd",
        description="Cili terminal client - connect to a running Cili server",
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="attach to the most recent session of the resolved workspace",
    )
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
    client = CiliClient(config.base_url, config.token)
    renderer = Renderer(no_color=args.no_color)
    app = TuiApp(
        client,
        renderer,
        continue_last=args.continue_session,
        session_id=args.session,
    )
    try:
        return app.run()
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
