# 收件箱（全局通知）设计

> 按当前实现整理（2026-11-28）。代码：`core/inbox.py`、`core/tools/send_notification.py`、
> `web/routes_inbox.py`、`web/static/inbox.js`；写入方还有 `core/cron/engine.py`、
> `core/workflows/{runner,service}.py`。

## 一、定位

收件箱是「AI / 系统 → 用户」的**持久通知**：用户可见、可回看、有已读状态，**不进入 LLM 上下文**，
只在 Web 端读写。

与相邻机制的分工：

| 机制 | 方向 | 持久化 | 谁看 |
|---|---|---|---|
| `agent_mailbox` / `message_bus` | agent ↔ agent | 否（进程内） | LLM |
| `outbox` / `send_user_file` | 工具 → 接入端 | 临时队列 | 用户（会话内文件卡片） |
| **收件箱** | AI / 系统 → 用户 | 是（JSON 文件） | 用户（跨会话汇总） |

## 二、数据模型

一条通知（`InboxItem`）：

| 字段 | 说明 |
|---|---|
| `id` | `inb_{YYYYMMDD_HHMMSS}_{8hex}`，正则 `^inb_[A-Za-z0-9_]+$` |
| `created_at` | `yyyy-MM-dd HH:mm:ss` |
| `level` | `info / success / warning / error`（非法回退 `info`） |
| `title` | 必填，≤120 字 |
| `body` | Markdown；工具通知 ≤4000 字，系统通知 ≤2000 字 |
| `source` | `{type: tool|cron|workflow|system, agent, interface}` |
| `workspace_uuid/name`、`session_id/title` | 来源定位（可为空） |
| `dedupe_key` | 幂等键；命中时返回已有条目、不修改内容 |
| `payload` | 结构化附加信息（job_id/run_id/status 等） |
| `read_at` / `deleted_at` | 已读/软删除时间；空串表示未读/未删 |

状态语义：`active`（未删除）、`unread`（未读未删）、`read`（已读未删）、`deleted`（软删除）、
`all`（含已删除）。

## 三、存储与容量

- 单文件：`{DATA_DIR}/inbox/inbox.json`，`{schema_version: 1, items: [...]}`；
- 单进程单例 `get_inbox_store()` + `RLock`，整文件原子写；损坏走 `.corrupt-*` 备份回退；
- 上限 `MAX_ITEMS = 500`；清理顺序：过期已删除（30 天）→ 超限时按「已读 → 已删除 → 未读」
  淘汰最旧；`active` 读路径不写，`deleted/all` 视图会顺带 `purge_deleted()`；
- 按 `created_at` 倒序（同秒按 id）。

## 四、写入

`InboxStore.add(title, body, *, level, source_type, agent, interface, workspace_uuid,
workspace_name, session_id, session_title, dedupe_key, payload, max_body_chars)`：

- `title` 必填并截断到 120 字；`body` 按来源截断；
- `dedupe_key` 用于防止重复（cron 按 `cron:{ws}:{job}:{slot}`，workflow 按
  `workflow:{ws}:{workflow}:{run_id}`）；
- 写入后广播 `inbox_changed`（纯信号，见 §七）。

写入方：

| 来源 | 入口 | 说明 |
|---|---|---|
| 工具 | `send_notification`（`source_type=tool`） | 归属根会话；同一会话 60 秒最多 10 条，超出报错让模型合并 |
| 定时任务 | `core/cron/engine.py` | 任务终态通知；`dedupe_key` 按 slot |
| 工作流 | `core/workflows/runner.py` / `service.py` | 运行终态与单步重跑结果；payload 含 `run_id/status/results_dir` |
| 系统 | 其他内部调用 | 默认 `source_type=system` |

## 五、读取与操作

- `list(status, workspace_uuid, allowed_workspace_uuids, limit, offset)` → `(items, total)`；
- `unread_count(...)`；`count_recent(session_id, seconds, source_type)` 用于限流；
- 操作为批量：`mark_read` / `mark_unread` / `mark_all_read` / `delete`（软删） /
  `restore` / `delete_permanent`（仅回收站）/ `purge_deleted`；
- 工作区过滤：指定 `workspace_uuid` 时只看该工作区；青少年模式传 `allowed_workspace_uuids`
  （见 §六）。

## 六、HTTP 接口（`web/routes_inbox.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/inbox` | `status/workspace_uuid/limit/offset`；返回 `items/total/unread_count` |
| GET | `/api/inbox/count` | 未读数 + active 总数 |
| GET | `/api/inbox/{id}` | 详情（正文） |
| POST | `/api/inbox/{id}/read` | 标记已读（CSRF） |
| POST | `/api/inbox/{id}/unread` | 标记未读（CSRF） |
| POST | `/api/inbox/read-all` | 按工作区全部已读（CSRF） |
| DELETE | `/api/inbox/{id}` | 软删除；`?permanent=true` 仅允许回收站中的条目 |
| POST | `/api/inbox/{id}/restore` | 从回收站恢复（CSRF） |

青少年模式：`_allowed_workspace_uuids()` 返回允许的工作区集合；空 `workspace_uuid` 的全局条目
沿用 teen 的「空工作区 = allow」规则。不可见条目统一返回 404，不泄露存在性。

## 七、事件与前端

- `inbox_changed` 是**纯信号**：只带 `action`，不带标题/正文/工作区/条目 ID，
  避免青少年模式下通过广播泄露受限条目；前端收到后重新拉 `/api/inbox`。
- 前端 `web/static/inbox.js`：顶栏按钮 + 未读徽标 + 两栏弹窗（列表/详情），
  状态筛选（全部/未读/已读/回收站）、全部已读、单条已读/未读/删除/恢复。

## 八、测试

`test/test_inbox_store.py`、`test/test_inbox_sources.py`、`test/test_inbox_tool.py`、
`test/test_inbox_teen.py`、`test/test_web_inbox_api.py`。

## 九、已知边界

- 仅 Web 端展示；QQ/微信/邮件/CLI 不显示收件箱；
- 单进程内存态 + 文件锁，不支持多进程同时写；多实例部署需外部存储；
- 500 条上限与 30 天回收站保留是硬编码常量；
- 通知正文不注入 LLM 上下文，模型只能通过工具写入、不能读取收件箱。
