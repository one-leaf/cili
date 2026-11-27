# Web API 设计

> 按当前实现重写（2026-11-25）。以 `web/` 下代码为准；本文只描述**已经存在**的端点与行为。

## 一、装配与生命周期

`web/web_api.py` 是唯一装配入口：

| 顺序 | 动作 |
|---|---|
| 1 | `FastAPI(lifespan=lifespan)` |
| 2 | lifespan：`import web.profile`（注册 Web 能力档案）→ `init_web_deps()` → `app_context.startup()` → `register_hooks()` |
| 3 | CORS 中间件（仅 localhost/127.0.0.1 任意端口，`allow_credentials=True`） |
| 4 | 访问控制中间件（**最后注册因此最外层**，见下节） |
| 5 | `TeenModeDenied → 403`、`TeenLockedOut → 429` 异常处理器 |
| 6 | 静态文件（`NoCacheStaticFiles`，`Cache-Control: no-store`）+ 20 个 `routes_*.py` 挂载 |

lifespan 退出：教材任务 `shutdown_all()` → `app_context.shutdown()`。

共享依赖集中在 `web/deps.py`：workspace 解析（`require_workspace_data_dir` / `require_workspace_root`）、
会话认领封装、CSRF、青少年守卫 `teen_guard(action, hard=)`、接入端启停 helper、默认 sink 绑定。
公共校验：`validate_session_id / validate_workspace_uuid / validate_exec_id / csrf_protect`。

## 二、认证与访问控制

`web/access_control.py`：

- **令牌优先**：`setting.json` 的 `system.access_token` 非空时，所有请求（含 localhost）必须携带
  `X-Access-Token` 头、`?token=` 查询参数或 HttpOnly cookie `cili_access_token`。
  首次 `?token=` 鉴权成功即下发 cookie（7 天），前端从 URL 移除令牌。
- **未配置令牌**：localhost 直接放行；其余按 `system.allowed_ips`（精确 IP 或 CIDR）判断。
- 配置尚未就绪（如无 API key）：只放行 localhost。
- 只信任 `request.client.host`（真实 TCP IP），不校验可伪造的 Host 头。
- **反代部署**：代理 IP 会被视为 localhost，`/api/fs/browse`、`/api/open-in-explorer` 这类
  本机端点在反代层必须单独封禁。

CSRF：写操作端点使用 `csrf_protect`（校验 Origin/Referer 为本站）。

## 三、通用约定

- 资源复数、按归属嵌套：`/api/workspaces/{uuid}/…`；动作是单段 snake_case 子路径
  （`queue/resume`、`mailbox/resume`、`ask-user/answers`）。
- 请求体字段：`profile`（驱动客户端，web/cli/…；缺省 web）、`protocol`（模型协议）。
- 占位语义：ask_user/审批卡的 block `_meta` 用 `ask_user_completed`（旧会话回退 `completed`）。
- 错误：400 参数/CSRF、403 权限/青少年拒绝、404 不存在、413 负载过大、429 青少年锁定、
  500 服务端异常。青少年拒绝统一转 403/429。
- 静态资源不缓存；`/api/events` 与两个 SSE 端点为 `text/event-stream`。

## 四、端点总表

### 4.1 工作区（`routes_workspace.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/workspaces` | 列表（含默认/系统标记） |
| POST | `/api/workspaces` | 新建（名称/目录校验） |
| PUT | `/api/workspaces/{uuid}` | 改名/改目录 |
| DELETE | `/api/workspaces/{uuid}` | 从索引注销（保留数据目录与文件；青少年 `workspace_unregister` hard） |
| GET/PUT | `/api/workspaces/{uuid}/git/settings` | Git 开关与远端 |
| GET | `/api/workspaces/{uuid}/git/status` | 状态/提交历史 |
| POST | `/api/workspaces/{uuid}/git/pull`、`/git/push` | 拉取/推送 |
| GET | `/api/workspaces/{uuid}/instructions` | 读取 AGENTS.md/CLAUDE.md |
| PUT | `/api/workspaces/{uuid}/instructions` | 保存（文件名守卫 + 路径越界校验） |
| POST | `/api/workspaces/{uuid}/instructions/generate` | 后台 worker 扫描生成（返回 task_id） |
| GET | `/api/workspaces/{uuid}/instructions/generate/{task_id}` | 生成任务状态 |
| GET | `/api/workspaces/{uuid}/instructions/templates`、`/templates/{name}` | 模板清单/内容 |

### 4.2 会话（`routes_workspace.py` + `routes_chat.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/workspaces/{uuid}/sessions` | 列表（metadata 摊平、按更新时间倒序） |
| POST | `/api/workspaces/{uuid}/sessions` | 新建（`channel` 默认 web；cli 显式传 cli） |
| GET | `/api/workspaces/{uuid}/sessions/{sid}` | 详情 |
| DELETE | `/api/workspaces/{uuid}/sessions/{sid}` | 删除（`core.session_purge` 唯一顺序；青少年 hard） |
| POST | `…/sessions/{sid}/rename`、`/hidden` | 重命名/隐藏 |
| POST | `/api/workspaces/{uuid}/sessions/batch` | 批量隐藏/删除 |
| GET | `…/sessions/{sid}/export`；POST `/api/workspaces/{uuid}/sessions/import` | 导出/导入 |
| GET | `…/sessions/{sid}/executions`、`/executions/{exec_id}` | 子代理执行日志 |
| GET/POST | `…/sessions/{sid}/model` | 会话级模型覆盖（空串清除） |
| POST | `…/sessions/{sid}/messages` | **发送消息，SSE 流**（`content/images/attachments/profile/client_id`） |
| POST | `…/sessions/{sid}/stop` | 停止（`scope=turn|background|session`） |
| POST | `…/sessions/{sid}/mailbox/resume` | 排空后台通知并恢复循环（与 queue/resume 不同） |
| POST | `…/sessions/{sid}/fork` | 克隆上下文到兄弟会话（`next_steps/label`） |
| POST | `…/sessions/{sid}/revert` | 撤销到 `msg_id`（清空队列） |
| GET | `…/sessions/{sid}/status` | 运行状态 + 队列摘要 |
| GET | `…/sessions/{sid}/context` | 上下文占用报告（消息体 + 系统提示词/工具估算；只读） |
| POST | `…/sessions/{sid}/ask-user/answers` | 提问卡/审批卡答案（`tool_use_id` + `answer/decision/answers`），SSE |
| POST | `…/sessions/{sid}/suggest` | 推荐输入（`refresh/exclude/input`） |
| GET | `…/sessions/{sid}/timeline` | 会话时间线 |
| GET | `…/sessions/{sid}/stream/{tool_use_id}` | 工具输出增量轮询（`offset` 字节） |
| POST | `…/sessions/{sid}/queue` | 入队（运行中发送走队列） |
| GET | `…/sessions/{sid}/queue` | 队列快照 |
| DELETE | `…/sessions/{sid}/queue/{item_id}` | 删除排队项 |
| POST | `…/sessions/{sid}/queue/clear`、`/queue/resume` | 清空/解暂停并启动 |

### 4.3 全局事件流

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/events?workspace_uuid&session_id&types&token` | SSE 全局流；`types` 逗号分隔的事件类型不受 session 过滤 |

帧类型（实际发出）：

- 正文/思考：`text`、`thinking`、`retry_clear`（413 去图重试清屏）
- 工具：`tool_call`、`tool_output`（流式增量）、`tool_result`
- 回合：`user_message`（投递回显）、`turn_complete`、`queue_update`、`todo_update`
- 子代理：`session_start`、`session_progress`、`session_complete`、`run_started`、`run_finished`、
  `background_runner_resume`
- 工作区级：`workflow_changed`、`session_changed`、`session_fork_created`、`files_changed`、`inbox_changed`（纯信号，前端收到后重拉 `/api/inbox/count`）
- 交付：`file`（Web 下载卡片事件）

### 4.4 文件（`routes_files.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/workspaces/{uuid}/files/{path}` | 规范文件路由（图片/附件/下载） |
| GET | `/api/fs/browse` | 目录浏览（**仅本机**；青少年限制在允许工作区内） |
| GET | `/api/files?workspace_uuid&path` | 目录内容（FileTree） |
| GET | `/api/files/search?workspace_uuid&q` | 名称/内容搜索 |
| GET/POST/PUT/DELETE | `/api/files…` | 读/建/改/删（`workspace_uuid` 必填） |
| GET | `/api/file-preview?…` | 文本预览（mtime_ns 以字符串传输） |
| GET | `/api/office-preview/status`、`/pdf` | Office→PDF 预览 |
| POST | `/api/files/upload` | 上传到 `uploads/` |
| POST | `/api/open-in-explorer` | 本机打开资源管理器（仅本机） |

`/api/workspace/*` 两代旧兼容路由已删除；历史消息中的旧 URL 由启动迁移 `legacy_file_urls` 改写。

### 4.5 配置 / MCP / ChatGPT OAuth

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/PUT | `/api/config` | 读取（api_key 掩码）/更新（providers/agents/system/qq/wechat/email 全量） |
| POST | `/api/config/test` | 测试 provider+模型组合（OAuth 走预览期整形） |
| POST | `/api/config/detect-models` | 拉取模型 ID 列表（anthropic 分页 / openai 单页） |
| POST | `/api/config/test-email` | 邮箱连接测试 |
| GET/PUT | `/api/mcp/servers?workspace=` | 工作区 MCP 配置与工具清单 / 全量保存 |
| POST | `/api/mcp/reload?workspace=`、`/api/mcp/test` | 重连工作区服务器 / 单服务测试 |
| POST | `/api/config/chatgpt-oauth/login` | 开始登录（`provider/credential/provider_config`） |
| GET | `/api/config/chatgpt-oauth/login/{attempt_id}` | 轮询状态 |
| POST | `/api/config/chatgpt-oauth/login/{attempt_id}/complete`、`/cancel` | 手动完成/取消 |
| POST | `/api/config/chatgpt-oauth/logout` | 登出并清 provider 凭据 |
| GET | `/api/config/chatgpt-oauth/status` | 登录态 |

### 4.6 接入端

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/qq/status` | QQ 账号列表（无凭据） |
| POST | `/api/qq/accounts/{id}/login/start`、`/login/cancel`、`/logout` | 扫码登录/取消/登出 |
| GET | `/api/qq/accounts/{id}/login/status` | 扫码状态 |
| GET | `/api/wechat/status` | 微信账号列表 |
| POST | `/api/wechat/accounts/{id}/login/start`、`/login/cancel`、`/logout` | 扫码登录/取消/登出 |
| GET | `/api/wechat/accounts/{id}/login/status` | 扫码状态（含验证码流程） |
| GET | `/api/email/status` | 邮件账号运行态与 24h 拦截统计 |

### 4.7 记忆 / 权限 / 青少年

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/workspaces/{uuid}/memory` | 记忆概览（开关、条目、待整合） |
| GET/PUT | `…/memory/entries/{name}` | 读取/编辑条目 |
| POST | `…/memory/entries/{name}/archive|restore|delete` | 归档/恢复/删除 |
| POST | `…/memory/consolidate` | 手动整合 |
| PUT | `…/memory/settings` | 工作区记忆开关 |
| GET | `/api/permissions` | 默认策略与判定配置 |
| PUT | `/api/permissions` | 更新策略 |
| GET | `/api/permissions/rules` | 规则列表 |
| POST | `/api/permissions/rules`、`PATCH/DELETE /rules/{id}`、`POST /rules/revoke-ai` | 规则增改删/AI 规则撤销 |
| GET | `/api/teen/status` | 公开状态（前端灰显/倒计时） |
| POST | `/api/teen/enable`、`/unlock`、`/lock`、`/disable` | 开关与解锁 |
| PUT | `/api/teen/policy` | 策略（白名单目录不得嵌套） |
| POST | `/api/teen/password` | 修改家长密码 |
| GET | `/api/teen/audit`、`/report` | 审计与使用报告 |

### 4.8 工作流 / 定时 / 教材 / 课件 / 升级

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/workspaces/{uuid}/workflows` | 列表/新建 |
| GET/PUT/DELETE | `…/workflows/{wid}` | 读取/保存/删除 |
| POST | `…/workflows/{wid}/hidden` | 隐藏 |
| POST | `…/workflows/{wid}/run`、`/stop` | 运行/停止 |
| GET | `…/workflows/{wid}/state` | 运行状态 |
| POST | `…/workflows/panel-sessions`、`GET …/by-session/{sid}` | 面板会话创建/查找 |
| POST | `…/workflows/{wid}/results-session`、`/send-results` | 结果会话/发送结果 |
| GET | `…/workflows/{wid}/runs`、`/runs/{run_id}`、`/runs/{run_id}/events` | 运行记录与事件 |
| GET/POST | `/api/workspaces/{uuid}/cron/jobs` | 任务列表/新建 |
| PUT/DELETE | `…/cron/jobs/{id}` | 更新/删除 |
| POST | `…/cron/jobs/{id}/run|enable|disable` | 立即运行/启停 |
| GET | `…/cron/jobs/{id}/history` | 执行历史 |
| GET | `/api/textbooks/options?stage&subject&edition&grade&semester&edition_grade` | 可选教材 |
| POST | `/api/textbooks/download` | 创建下载任务 |
| GET/POST | `/api/textbooks/download/{task_id}`、`/retry`、`/cancel` | 状态/重试/取消 |
| POST | `/api/courseware/quiz-result` | 课件答题回传（CORS 白名单 + 体积上限） |
| GET | `/api/update/check|status`；POST `/api/update/download|restart` | 升级检查/下载/重启 |

### 4.9 工作区向导

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/wizard/options` | 分类/学段/学科/版本/位置 |
| POST | `/api/wizard/preview` | 预览目录与 AGENTS.md 渲染 |
| POST | `/api/wizard/create` | 创建工作区（可触发教材下载） |

### 4.10 收件箱（`routes_inbox.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/inbox?status&workspace_uuid&limit&offset` | 列表；列表项只带 `excerpt`，`status` 默认 `active` |
| GET | `/api/inbox/count` | 未读数/总数（青少年模式按工作区白名单过滤） |
| GET | `/api/inbox/{item_id}` | 详情（完整 `body`，不改变已读状态） |
| POST | `/api/inbox/{item_id}/read`、`/unread` | 标记已读/未读 |
| POST | `/api/inbox/read-all` | 全部标为已读（body 可选 `workspace_uuid`；只作用于可见条目） |
| DELETE | `/api/inbox/{item_id}` | 软删除；`?permanent=true` 彻底删除（仅限回收站条目） |
| POST | `/api/inbox/{item_id}/restore` | 从回收站恢复 |

写端点带 `csrf_protect`；`/count` 必须声明在 `/{item_id}` 之前。

## 五、SSE 细节

### 5.1 请求级流（POST /messages、POST /ask-user/answers）

`web/sse.py`：runner 同步回调 → 队列 → `StreamingResponse`。

- 正文/思考/工具事件同时扇出到 `runner.default_sink`（全局流）；前端用 `isSending` 去重。
- `retry_clear` 帧：413 去图重试时要求前端清空已流式文本。
- 回合末先推 outbox 的 `file` 帧（`web.delivery`），再推结束哨兵。
- 客户端断开：`/ask-user/answers` 取消恢复循环；`/messages` 默认不中断后台执行，
  仅终止 SSE 生成器。

### 5.2 全局流（GET /api/events）

- 按 `workspace_uuid`/`session_id` 过滤；`types` 中列出的类型不受 session 过滤。
- 回合落盘后必发 `turn_complete`（页面刷新后界面唯一感知回合结束的途径）。
- 无请求级 sink 的回合（后台恢复/看门狗）由 `default_sink` 补齐 Web 文件交付。

## 六、前端结构（`web/static/`）

| 文件 | 职责 |
|---|---|
| `index.html` / `session.html` | 主界面 / 只读会话分享页 |
| `app.js` | 装配、工作区切换、主题、事件绑定 |
| `sidebar.js` | 会话/工作流侧栏、多选与批量操作 |
| `chat.js` | 消息渲染与发送 |
| `chat-empty-state.js` | 空状态示例与欢迎区 |
| `chat-scroll.js` | 聊天贴底状态 |
| `interaction-cards.js` | 提问卡/审批卡 |
| `queue-control.js` | 排队条、停止/状态 |
| `composer.js` | 输入区按钮、推荐输入、模型下拉、附件上传 |
| `settings.js` + `provider-settings.js` / `channel-settings.js` / `teen-mode.js` / `permission-settings.js` / `about-settings.js` | 全局设置各页签 |
| `workspace-settings.js` / `workspace-wizard.js` / `textbook-settings.js` | 工作区设置/向导/教材页签 |
| `file-tree.js` / `file-browser.js` / `file-api.js` | 文件树、目录浏览弹窗、REST 封装 |
| `workflow-pane.js` / `memory.js` / `cron-settings.js` / `session-about.js` / `context-gauge.js` | 工作流面板/记忆/定时/时间线/上下文仪表 |
| `agent-cards.js` / `inbox.js` / `sse-client.js` | 子代理卡片 / 收件箱面板 / SSE 客户端 |
| `utils.js` / `markdown-core.js` / `voice-input.js` / `tts.js` | 工具函数/Markdown 核心/语音 |

脚本顺序：`utils → chat-scroll → voice/tts → file-tree → settings… → app → 各面板 → context-gauge → chat → interaction-cards → queue-control → agent-cards → inbox → sse-client`。
所有事件绑定在 `DOMContentLoaded` 后执行。

## 七、契约与迁移

- 请求字段 `profile`（原 `interface`）；配置键 `protocol`（原 `interface_type`）。
- 工具注册键与工具名统一（`todo_write`）；`send_user_file` 的接口门控由 `Tool.interface_scoped` 声明。
- ChatGPT OAuth 命名族：包 `core/llm/chatgpt_oauth`、路由 `/api/config/chatgpt-oauth/*`、
  provider 键 `chatgpt-oauth`、凭证前缀 `chatgpt-oauth:`。
- 启动迁移（`core/migrations`）：`provider_protocol_key`、`chatgpt_oauth_naming`、
  `session_placeholder_meta`、`tui_channel_to_cli`、`legacy_file_urls`；配置在 `data/cili/backup/migrations/` 备份。
