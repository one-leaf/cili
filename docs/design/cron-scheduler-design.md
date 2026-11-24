# 定时任务（Cron v2）设计

> 按当前实现重写（2026-11-25）。代码：`core/cron/`、`core/cron_seeds/`、`core/tools/cron.py`、
> `web/routes_cron.py`。

## 一、存储布局

```
{workspace}/.cili/cron/
  {job_id}/
    job.json      任务定义（Job.to_dict）
    state.json    运行状态（下次运行时间、历史、cron 会话绑定）
```

- 全局任务在 System 工作区：`data/.cili/cron/{id}/`（cron 种子安装于此，`managed=true` 只读）。
- 任务 ID 限定 `[A-Za-z0-9_-]{1,64}`。

## 二、任务模型（`core/cron/model.py`）

`Job` 字段：

| 字段 | 含义 |
|---|---|
| `id` / `name` / `description` / `enabled` | 标识与开关 |
| `scope` | `workspace` / `global` |
| `workspace_uuid` | 所属工作区（global 为空） |
| `schedule` | `{type: at|interval|cron, ...}` |
| `misfire` | `{policy: once|skip|none, grace_minutes}`（默认 once / 120 分钟） |
| `content` | 执行内容（提示词或动作定义） |
| `execution` | `timeout_seconds`(900) / `max_executions` / `max_runs_kept`(20) / `context`(fresh) |
| `delivery` | `{mode: none|origin|session, ...}` |
| `origin` | 来源会话信息（delivery=origin 时回投） |
| `one_time` | 一次性任务（执行后失效） |
| `managed` / `seed_revision` / `seed_checksum` | 种子任务托管字段 |

`JobState`：`next_ts`、`last_run`、`history`（保留 max_runs_kept 条）、`cron_session_id` 等。
`validate_schedule(job)` 校验类型与参数；`compute_next_run` 计算下次时间（支持时区）。

## 三、调度类型

| 类型 | 参数 | 说明 |
|---|---|---|
| `at` | `at`（ISO 时间） | 单次执行（通常配 `one_time`） |
| `interval` | `every`（分钟等） | 固定间隔 |
| `cron` | `expr`（5 段） | 标准 cron 表达式（支持 `*/n`、范围、列表） |

`schedule_summary(job)` 生成人读描述。

## 四、引擎（`engine.py`）

- 单调度线程 + `Event` 唤醒；`start/stop/reload` 由 `core.cron.bootstrap` 在启动时调用。
- `_load_all(recover_running=True)`：启动时恢复 `running` 状态（进程中断视为失败并记录）。
- 到期任务交给执行器；全局并发受 `BoundedSemaphore` 限制；同一任务串行。
- misfire：超过宽限期的错过执行按策略处理（once=补一次/skip=跳过/none=不补）。

## 五、执行（`executor.py`）

- 每个任务拥有独立会话：`ensure_cron_session`（`[Cron] {name}`，状态保存在 state.json）；
  `context=fresh` 时每轮新建上下文。
- 运行：`runner.run(prompt)`（worker/lite 自主模式），带超时；超时停止 runner，
  无法停止时把 runner 标记为 poisoned 并移出池。
- 结果：状态/摘要/耗时写入 `JobState.history`；`RunResult` 交给投递层。

## 六、投递（`delivery.py`）

| 模式 | 行为 |
|---|---|
| `none` | 仅本地记录（不发送） |
| `origin` | 回投到创建会话（按其通道发送） |
| `session` | 投递到指定工作区/会话 |

失败时回退为本地记录；跨接口投递复用 `core.outbox`/接口 sink。

## 七、种子任务（`core/cron_seeds/`）

- `memory-consolidation`：定期整合各工作区记忆。
- `office-pdf-sync`：同步 Office→PDF 缓存。
- 启动时 `bootstrap.start()` 安装到 System 工作区；`managed=true` 只读（界面不可改），
  种子版本变化按 `seed_revision/checksum` 更新。

## 八、入口

- 工具 `cron`（master）：create/list/get/update/delete/run/enable/disable/history。
- Web：`/api/workspaces/{uuid}/cron/jobs…`（列表/增改删/运行/启停/历史）。
- 迁移：`core/cron/migration.py`（v1 数据目录 → v2，失败打包保留，不丢数据）。

## 九、测试

`test_cron.py`（解析/校验/引擎/执行/投递）、`test_web_*`（路由）、`test_cron_seeds`（种子安装）。
