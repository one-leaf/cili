# 青少年模式设计

> 按当前实现整理（2026-11-28）。代码：`core/teen_mode.py`、`core/security/teen_paths.py`、
> `core/security/teen_primitives.py`、`core/security/path_policy.py`、`core/approval_watchdog.py`、
> `core/prompt_builder.py`、`core/tools/base.py`、`core/turn_dispatch.py`、
> `web/routes_teen.py`、`web/deps.py`、`web/static/teen-mode.js`。

## 一、定位

青少年模式是一套**家长可控的使用约束**：开关与密码、工作区白名单、硬禁路径、审批 strict、
时长/时段限制、审计与家长面板。模式关闭时所有守卫立即放行（零行为差异）；开启后分两层：

| 层级 | 含义 | 解锁窗口是否放行 |
|---|---|---|
| **硬禁（hard）** | 模式语义本身：受保护路径写/删、会话删除/隐藏/分享、工作区删除/注销、记忆删除等 | 否 |
| **降级（soft）** | 需要家长授权的操作：MCP、浏览器上传、cron/workflow 修改、越界写/删、审批卡 | 是（全局或工作区解锁窗口内） |

## 二、状态与存储

- 状态文件：`{DATA_DIR}/teen_mode.json`（单文件、原子写、按 `(path, mtime_ns, size)` 指纹缓存，
  外部修改自动重读；损坏走 `.corrupt-*` 备份回退）；
- 审计：`{DATA_DIR}/teen/audit.jsonl`（追加式 JSONL，进程内互斥防交错）；
- 用量：`{DATA_DIR}/teen/usage.json`（只保留当天，跨日自动重置）。

默认状态：

```json
{
  "schema_version": 1,
  "enabled": false,
  "password": {},
  "policy": {...DEFAULT_POLICY},
  "limits": {...DEFAULT_LIMITS},
  "unlock": {"window_minutes": 15, "scopes": {}, "failed_attempts": 0, "locked_until": ""}
}
```

## 三、密码与解锁

- 密码 ≥6 位；`scrypt(n=16384, r=8, p=1)` 加盐哈希，不存明文；
- 连续失败 `FAIL_LIMIT=5` 次锁 `BASE_LOCK_MINUTES=5`，之后按次数指数退避，上限 60 分钟；
- 解锁作用域：`*`（全局）或 `ws:<uuid>`（单工作区）；窗口默认 15 分钟，可选 5/15/30/60；
- 未解锁时访问受限工作区返回 `locked`；解锁任意作用域后 `workspace_access` 放行；
- 关闭模式、修改密码、修改策略都必须提供密码或处于全局解锁窗口。

## 四、默认策略

`DEFAULT_POLICY`（全部可在家长面板调整）：

| 键 | 默认 | 含义 |
|---|---|---|
| `deny_hide` | true | 禁止隐藏会话 |
| `deny_share` | true | 禁止分享会话链接 |
| `deny_memory_delete` | true | 禁止记忆删除/归档 |
| `deny_file_delete` | false | 工作区内文件删除默认放开（写代码需要） |
| `outside_workspace` | `deny` | 越界写/删：拒绝（而非走审批） |
| `allowed_workspaces` | `[]` | 工作区白名单；空 = 全部受限 |
| `deny_read_outside` | true | 工作区外读取拒绝（read/grep/glob） |
| `approval_strict` | true | 审批 strict：未解锁不出卡、不落盘规则、高风险直接拒绝 |
| `mcp_tools` | `deny` | 外部 MCP 工具 |
| `browser_upload` | `deny` | 浏览器上传 |
| `cron` | `deny` | 定时任务增删改 |
| `workflows` | `deny` | 工作流增删改与运行 |
| `prompt_block` | true | 注入青少年模式 system prompt 块 |

`DEFAULT_LIMITS`：`daily_minutes=0`（不限）、`allowed_windows=[]`（不限，格式 `["08:00-21:00"]`）。

被拒动作有统一中文文案（`_DENY_MESSAGES`），路由 403 detail 与接入端提示共用。

## 五、硬禁路径（`core/security/teen_paths.py`）

模式开启时，以下目录的**写/删**为硬 deny（解锁也不放行；`tmp/` 子目录除外）：

- `data/cili/**`：`setting.json`、`workspaces.json`、`teen_mode.json`、cron、工具状态等；
- `data/.cili/**`：System 工作区数据；
- 各工作区 `{directory}/.cili/**`：会话、记忆、审批规则、执行日志（`tmp/` 除外）。

保护清单由 `protected_roots()` 动态汇总（含所有已登记工作区）。模式关闭时返回 `None`，
与普通路径策略完全一致。

## 六、运行时守卫

| 位置 | 行为 |
|---|---|
| `core/tools/base.py` | 工具执行前 `guard_tool_command`（shell/python 目标）与 `guard_tool_read`（工作区外读取） |
| `core/tools/browser.py` | 上传按 `browser_upload` 拒绝 |
| `core/tools/cron.py` / `workflow.py` | 增删改/运行按 `cron` / `workflows` 拒绝（读取放行） |
| `core/security/path_policy.py` | 审批 strict：未解锁时越界/高风险操作直接拒或不出卡 |
| `core/session_runner_runtime/loop.py` | 注入 `prompt_block`；strict 下审批占位不落盘规则 |
| `core/approval_watchdog.py` | 后台自动恢复前 `check_send_allowed()` |
| `core/turn_dispatch.py` | 回合结束累计 `record_turn_seconds` |
| `web/routes_chat.py`、`interfaces/common/*` | 发消息前 `check_send_allowed()`（时长/时段） |
| `web/routes_workspace.py` | 会话删除/隐藏/工作区删除/注销等硬禁 |
| `web/routes_memory.py`、`routes_files.py`、`routes_config.py` | 记忆删除、文件删除、设置修改按策略拒绝 |
| `web/routes_inbox.py` | 按允许工作区集合过滤通知（空工作区条目沿用“allow”） |

模式关闭时以上守卫全部短路。

## 七、时长与时段

- `usage.json` 记录当天 `minutes/turns/denied`，跨日自动重置；
- 每个回合结束把运行时间累计进去（跨会话合并）；
- `check_send_allowed()` 在**发送消息前**检查：`daily_minutes` 用尽或不在 `allowed_windows`
  内直接拒绝（进行中的回合不受影响，避免中途截断）；
- 家长面板的 `usage_report(days)` 汇总今日用量、限额与近 N 天拒绝次数（历史逐日数据只靠审计粗算）。

## 八、审计

- `audit.jsonl` 记录：`enable/disable/policy_update/password_change/unlock/lock/deny/limit_exceeded/
  startup_state_change` 等；
- `deny` 事件含 `action/target/source/ip/detail`，并累计到 `usage.denied`；
- 启动时 `check_startup_state()` 检测“上次开启、本次关闭/文件丢失”，写
  `startup_state_change` 审计（识别外部删除文件的后门路径）；
- `read_audit(limit)` 返回最近记录（旧→新）供家长面板展示。

## 九、HTTP API（`web/routes_teen.py`）

| 方法 | 路径 | 权限 |
|---|---|---|
| GET | `/api/teen/status` | 公开（前端灰显，不含密码哈希） |
| POST | `/api/teen/enable` | 仅未开启时公开（要密码设置） |
| POST | `/api/teen/unlock` / `/api/teen/lock` | 公开（解锁本身要密码） |
| POST | `/api/teen/disable` | 需密码 / 全局解锁 |
| PUT | `/api/teen/policy` | 需全局解锁（`teen_admin`） |
| POST | `/api/teen/password` | 需旧密码 / 全局解锁 |
| GET | `/api/teen/audit`、`/api/teen/report` | 需全局解锁 |

错误：密码错误 401；连续错误 `TeenLockedOut → 429`；硬禁/受限工作区 `TeenModeDenied → 403`
（由 web_api 全局异常处理器统一转换）。写操作均带 CSRF。

## 十、前端与测试

- `web/static/teen-mode.js`：家长设置入口、开关/解锁/策略编辑、审计与用量报表；
- 测试：`test_teen_mode.py`、`test_teen_guard_api.py`、`test_teen_guard_routes.py`、
  `test_teen_path_policy.py`、`test_inbox_teen.py`。

## 十一、已知边界

- 密码与状态都在本机文件；能直接改文件/换机器的人可绕过（启动审计只记录不阻止）；
- 用量历史只保留当天，近 N 天报表只能按审计里的拒绝事件粗算；
- 硬禁路径清单是白名单式目录；新增运行时数据目录需同步加入 `protected_roots()`；
- 时段判断按本机时区与分钟粒度，不处理跨零点窗口。
