# 终端客户端（CLI）设计

> 按当前实现重写（2026-11-25）。代码：`clients/cli/`、`cli.cmd`。

## 一、定位

- **进程外客户端**（不是服务端接入端）：`cli.cmd` → `python -m clients.cli` → HTTP + SSE 访问
  正在运行的 cili Web 服务（本机或远程）。
- **cwd 即工作区**：启动时按当前目录匹配工作区（必要时创建/指定），会话归属通道为 `cli`。
- 能力档案名 `cli`（与通道 id 一致；旧的 `tui` 已由迁移改名为 `cli`）。

## 二、模块

| 文件 | 职责 |
|---|---|
| `cli.py` | argparse 入口（server/workspace/session 等参数） |
| `discovery.py` | 服务端发现：`data/cili/server.json` > `setting.json` 端口 > 默认；用户配置 `~/.cili/config.json` |
| `client.py` | `CiliHttpClient`：REST + `chat_stream`（SSE 帧解析） |
| `app.py` | 交互式 REPL（分时读取 stdin 与流式输出） |
| `frames.py` | `SseFrameRouter`：SSE 帧 → 渲染器提交 |
| `render.py` | 终端渲染（正文/思考/工具/提问卡编号与多选） |
| `commands.py` | 本地指令（/help /session /new /stop /quit） |

## 三、发现与鉴权

`discover_config(server_override)` 优先级：

1. `--server` 参数；
2. 环境变量/用户配置 `~/.cili/config.json`；
3. `data/cili/server.json`（服务端启动时落盘，含端口/绑定）；
4. `setting.json` 的 `system` 端口；
5. 默认端口。

令牌：服务端配置了 `access_token` 时，从配置读取并以 `X-Access-Token` 头携带。

## 四、回合与流

- 发送：`POST /api/workspaces/{uuid}/sessions/{sid}/messages`，body
  `{content, profile: "cli", client_id}`；响应为 SSE 请求流。
- `_iter_sse_frames` 解析 `data: {...}`；`SseFrameRouter` 按帧类型分发：
  正文/思考增量、工具调用/结果、提问卡（编号提示）、`turn_complete` 收尾。
- 停止：`POST …/stop`（scope=turn/background）。
- 提问卡回映射：`interfaces.common.ask_user.resolve_answer`（与 QQ/微信一致）。

## 五、会话

- 启动时选择/创建 `cli` 通道会话；`/session` 切换、`/new` 新建。
- 会话归属标记三元组：`channel=cli`，账号/聊天为空。

## 六、入口脚本

`cli.cmd`：

- 定位项目根，设置 `PYTHONPATH`；
- `mintty` 下优先 `winpty python -m clients.cli`；
- 其余终端直接 `python -m clients.cli %*`。

## 七、测试

`test_cli_client.py`（客户端与 SSE 解析）、`test_cli_render.py`（帧路由/渲染/提问卡编号）、
`test_channel_clients.py`（通道选择）。
