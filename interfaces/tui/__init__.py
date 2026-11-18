"""终端（TUI/CLI）接入端。

客户端形态：``cli.cmd`` 启动 ``interfaces.tui.cli``，cwd 即 workspace；
服务端发现与 HTTP+SSE 客户端在 ``config.py`` / ``client.py``，渲染在
``sink.py`` / ``render.py``。能力档案由 ``profile.py`` 在 import 时注册。
"""
