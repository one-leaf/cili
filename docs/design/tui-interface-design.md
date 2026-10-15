# 终端（TUI）接入端设计

> 目标：**终端客户端（cli.cmd）**——**在哪个目录执行 cli.cmd，该目录就是当前 workspace**。
>
> 形态：**客户端 / 服务端分离**——TUI 连接常驻服务端（web server），支持多个 TUI
> 同时连接；workspace 锁定启动目录，不支持切换。
>
> 版本：v1.0 ｜ 日期：2026-10-15

---

## 一、定位与分层

会话层接口无关（`core/session_registry.py` 持有 runner 池与运行权认领），TUI 补
「接入端」这一层——但它**不驻留服务端进程**：TUI 是 **web API 的第二个前端**
（HTTP + SSE），web 前端用什么 API 它就用什么 API。服务端零新增核心机制，
`turn_dispatch` / `outbox` / `default_sink` / `interface_profile` 全部复用。

```
cli.cmd                    Windows 启动器（任意目录 → python -m interfaces.tui）
interfaces/tui/            终端客户端（瘦：不 import 任何 core/* 模块）
  __init__.py              包声明 + 落地清单注释
  __main__.py              python -m interfaces.tui → cli.main()
  cli.py                   main()：argparse（-c/--session/--server/--no-color）
                           + 非 TTY 检测 + 装配
  config.py                服务端发现与鉴权解析链
  client.py                httpx API 客户端 + SSE 帧解析
  render.py                渲染原语（纯函数为主：工具行摘要、ask_user 卡、答案映射）
  sink.py                  SseFrameRouter：SSE 帧 → 渲染回调（缓冲 + 提交点）
  commands.py              /h /s /new /stop /quit（/status 透传服务端）
  app.py                   TuiApp：输入循环 + 回合驱动 + Ctrl+C 语义
  profile.py               能力档案（服务端 import 注册，客户端自身不用）

main.py                    服务端启动时落盘 data/cili/server.json（发现锚点）
core/app_context.py        startup() 注册 tui 能力档案
web/routes_chat.py         POST messages 的 interface 字段（唯一的路由层改动）
```

与服务端入口**物理分离**：`main.py` / `start.cmd` 只服务 Web 服务端，一律不掺 TUI。
不做 pip 打包、无全局命令——启动方式就是执行 cli.cmd。

## 二、服务端改动（共三处）

1. **`interface` 字段**（web/routes_chat.py:379）：`POST messages` 新增可选字段，
   `run_runner` 按它 `set_turn_interface(iface)`——能力档案（Response Surface）与
   接口专属工具随回合切换，缺省 `"web"`，旧前端零影响。两个实现要点：
   - **取值必须在返回 SSE 响应之前**（`iface = getattr(...)` 在路由函数体里）——
     若留到 executor 线程内取，异常会吞掉结束哨兵把 SSE 流挂死
   - **getattr duck-typing**：web 路由测试广泛用 SimpleNamespace 伪造 request，
     缺属性按缺省处理（与 `set_iface` 本身的 duck-typing 同一约定）
2. **`server.json` 落盘**（main.py:1345）：服务端启动时写
   `{host, port, pid, started_at}`，退出（含 Ctrl+C）时删除——TUI 据此发现同机
   服务端，自定义 `--port` 自然覆盖
3. **tui 档案注册**（core/app_context.py startup）：TUI 没有 start 函数可走
   （QQ/微信是启动接口时顺带注册的），在此用 import 副作用注册
   `interfaces.tui.profile`——否则 `set_turn_interface("tui")` 的回合告警
   「未知接口」并按未声明处理（提示词缺 Response Surface 段）

## 三、服务端发现与鉴权（interfaces/tui/config.py）

解析链（优先级从高到低）：

| 项 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| 服务端地址 | `--server` 参数 | 环境变量 `CILI_SERVER` | `~/.cili/config.json` | `data/cili/server.json` | 默认 `http://127.0.0.1:8000` |
| token | 环境变量 `CILI_TOKEN` | `~/.cili/config.json` | 服务端 setting.json 的 `system.access_token` | | |

项目根由 `config.py.__file__` 自定位（客户端不依赖 cwd）——同机场景直接读服务端
的 server.json / setting.json；`~/.cili` 与环境变量覆盖远程/脚本场景。未配置
access_token 时服务端走 IP 白名单，客户端无需 token。连接失败提示先启动服务端，
退出码 2；不做自动拉起（spawn daemon 的孤儿进程管理复杂度高，明确排除）。

## 四、cwd → workspace（client.py `resolve_workspace`）

1. `GET /api/workspaces` 取列表
2. 按 `os.path.normcase(os.path.abspath(cwd))` 匹配各 workspace 的 `directory`
   （Windows 盘符大小写与正反斜杠归一化；列表只拉一次，匹配与查重名共用）
3. 命中 → workspace_uuid 就绪；未命中 → `POST /api/workspaces`
   `{name: 目录名, directory: cwd}` 自动注册；重名追加后缀（`tmp-2`、`tmp-3`…），
   目录匹配按 path 不受影响

服务端无 by-path 端点，客户端本地匹配即可——**零新增端点**。

## 五、会话模型（多 TUI 并发）

启动时三选一（app.py `_setup_session`）：

| 方式 | 行为 |
|---|---|
| `cli.cmd`（默认） | 该 workspace 下新建会话 |
| `cli.cmd -c` | 附加最近活跃会话（`list_sessions` 按 mtime 倒序取第一个） |
| `cli.cmd --session X` | 指定会话 id |

- `/new` 新建、`/s` 列出最近 10 条、`/s <序号>` 切换
- 多 TUI 各自新会话互不干扰；同一会话并发驱动由**服务端 registry.claim 互斥**，
  客户端收到 `{"type": "error"}` 帧（「当前会话正在执行中」）显示后保持可输入
- 出站文件：回合末服务端 `drain_outbox_events` 推 `file` 帧（send_file 产物路径），
  TUI 只展示不上传——与 tui 能力档案 `can_send_file=False` 一致

## 六、回合流与超时（client.py `chat_stream`）

`POST .../messages`（body 含 `interface: "tui"`）→ SSE 流。**超时策略是本模块
最重要的细节**：`httpx.Timeout(10.0, read=None)`——connect/write/pool 保持 10 秒
（快速发现服务端未启动），但 **read 无限制**：工具执行（bash/python 可达数分钟）
期间没有 SSE 帧产出，client 级读超时会把正常回合掐断（服务端继续跑完落盘，
客户端却收不到结果——现象是 `timed out` + 服务端日志 `Client disconnected,
runner continues running in background`）。与 web 前端 EventSource 同语义。

帧解析（`_iter_sse_frames`）：收集 `data:` 行、空行定帧边界、非法 JSON 跳过不中断。

## 七、SSE 帧 → 渲染（sink.py `SseFrameRouter`）

| SSE 帧 | 渲染行为 |
|---|---|
| `text` | 累积进正文缓冲 |
| `thinking` | 累积进思考缓冲 |
| `retry_clear` | 清空两个缓冲重流（413 重试信号；终端缓冲可清空） |
| `tool_use` | **提交点**：正文/思考缓冲先提交（markdown 精渲染）；打工具行 `🔧 name 摘要`；ask_user 另渲染问题卡 |
| `tool_result` | 摘要行：成功 `✔ name (N 行)`；失败 `✘ name` + 前 10 行；ask_user 的 tool_result 是应答确认，打状态行 |
| `file` | `📄 name path —— note` |
| `error` | 提交点 + 红字错误行 |
| `session_start` / `session_complete` | dim 状态行 |
| `done` | 由 `finish()` 兜底提交缓冲 |
| 未知帧（todo_update 等） | 静默忽略 |

**提交点** = 语义边界（tool_use / file / error / done / retry_clear）：正文在回合
结束或工具块边界一次性 markdown 精渲染进 scrollback（rich Console.print），
已完成内容不再改动。

工具行摘要（render.py `summarize_tool_input`）：按 `_SUMMARY_KEYS`
（action/path/file_path/command/pattern/glob/query/url/name/skill/task/description）
取第一个非空字段、空白折叠、80 字符截断；整卡大入参（ask_user）不展示。

**ask_user 交互**（TUI 相对 QQ 的优势——答案天然同步）：
`tool_use(ask_user)` 渲染编号问题卡并记下 label 表（`build_option_labels`）；
用户输入 `1` / `1 3`（多选）经 `resolve_answer` 映射回 label，自由文本原样；
答案作为普通消息 POST，服务端 `prepare_turn` 检测到待答占位符自动判为 resume
注入（turn_dispatch.py），**客户端无需任何 resume 专用逻辑**。发送后 label 表清空。

## 八、命令与中断（app.py / commands.py）

| 命令 | 行为 |
|---|---|
| `/h` `/help` | 本地帮助文本（markdown 渲染） |
| `/s` `/session` | 列出最近 10 条；`/s <序号>` 切换 |
| `/new` | 新建会话 |
| `/status` | **透传服务端**（服务端把 "/status" 识别为文本命令返回 usage 报告），走回合流渲染 |
| `/stop` | `POST .../stop`（服务端 `runner.stop()`：当前回合在下一步中断） |
| `/quit` `/q` `/exit` | 退出（Ctrl+D 等价） |
| 未知命令 | 报错提示 /h |

**Ctrl+C 语义**（run_turn 的 next() 循环内处理，生成器引用不能丢——丢了对端
HTTP 连接就断，剩余帧收不到）：

- 回合中首次：`POST /stop` + 继续消费**同一个流**直到收尾（「3 秒内再按强制断开」）
- 3 秒窗口内二次：`frames.close()` 强制断开（服务端照常收尾落盘）
- 空闲时：退出循环（Ctrl+D 等价）

## 九、入口链（cli.cmd → python -m interfaces.tui → cli.main()）

```
cli.cmd（~40 行批处理，自包含）
  ├─ %~dp0 定位项目根；chcp 65001
  ├─ Python 发现（不自动下载——缺失时提示先跑 start.cmd）：
  │    1. data\deps\python\python.exe（portable；项目根在 ._pth 里，任意 cwd 可 import）
  │    2. PATH 里的 python（此时 set PYTHONPATH=%~dp0）
  ├─ winpty 包装：TERM_PROGRAM==mintty 且 winpty 可用（mintty 下 python 的
  │  stdin/stdout 非 console；Windows Terminal/cmd 直接跑）
  └─ "%PYTHON%" -m interfaces.tui %*
     ⚠️ 绝不 cd——cwd 保持用户目录，这是「cwd 即 workspace」的前提
```

- 用 `-m` 而非文件路径：保证包上下文（任意 cwd 下包内相对导入可用）
- cli.py 启动即检 `sys.stdout.isatty()`，非 TTY（管道/重定向）拒绝启动并提示
- `cli.py` 只做参数解析与装配；`TuiApp` 分时模型（等待输入时无渲染，回合期间
  只消费 SSE 帧渲染），prompt_toolkit 常驻输入框是后续演进

## 十、测试

```
test/test_tui_render.py     渲染原语纯函数（工具摘要/ask_user 卡/答案映射）+
                            SseFrameRouter 帧序列 → 调用序列（FakeRenderer 收集，
                            覆盖 retry_clear/提交点/ask_user label 表/error 兜底）
test/test_tui_client.py     httpx.MockTransport 注入（client.py 构造器可传 transport）：
                            cwd 归一化匹配/自动注册/重名后缀、SSE 解析（非法帧跳过）、
                            interface="tui" 断言、并发 error 帧、连接失败提示、stop
test/test_web_api.py        TestSendMessageInterface：interface 字段 → set_turn_interface
                            调用序列 ["tui", None]；缺省/缺属性 → ["web", None]
```

渲染与 client 均为纯函数/可注入，无需真实终端；mintty/winpty 分支手动验证
（CI 无法覆盖终端模拟器）。

## 十一、当前边界（明确未做）

- **流式渲染**：正文在提交点/回合结束一次性渲染（无 rich Live tail），长正文
  无打字机效果
- **输入**：`input()` 循环——无多行、无历史持久化、无 / 命令补全、无大粘贴折叠
- **断线**：无自动重连与增量回拉（超时断开后用 `-c` 或 web 看落盘结果）
- **全局事件流**：未接 `GET /api/events`（多客户端同会话实时旁观）
- **工具流式输出**：未接 `GET .../stream/{tool_use_id}`（结果摘要已够）
- **图片**：能力档案 `image_protocol="none"`，上传/内联未做
- **非 TTY / 自动拉起服务端 / pip 打包 / 多会话分屏**：明确排除
