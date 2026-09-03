# Cron 定时任务调度器设计文档

本文档描述 Cili Agent 的轻量级后台任务调度系统（Cron Scheduler）。

---

## 一、功能概述

Cron 调度器为 Cili Agent 提供周期性的后台任务执行能力，用于自动化处理定期任务（如从会话记录中提取用户信息、系统维护）。

**核心特性**：
- 基于 JSON 配置的任务定义，支持内联或动态 Python 函数
- 支持两种调度类型：`interval`（分钟间隔）和 `cron`（标准 cron 表达式）
- **通过 SubAgent 执行**：Cron 直接在 cron session 中创建 SubAgent 执行任务（采用 context-bounded-processing 策略）
- **System workspace**：专用于系统维护任务（UUID: `system`，cwd: `data/`）
- 后台线程每 60 秒检查任务到期时间，per-workspace lock 串行化
- **last_run 持久化**：任务执行后自动保存到状态文件，重启后恢复
- **remaining 计数器**：通用执行次数限制，配合 loop 工具实现自循环任务自动终止
- 全局单例，随应用启动/关闭

---

## 二、架构设计

### 2.1 执行链路

```
Cron 触发
  ↓
CronTask.execute()
  ├─ 解析目标 workspace（System 或用户指定）
  ├─ 用 state 中的 session_id 定位 session（不存在则新建 "[Cron] 任务描述"）
  ├─ 添加 user message（任务描述）
  ├─ 添加 assistant message（tool_use: subagent，模拟 LLM 调用）
  ├─ 创建 SubAgent 直接执行任务
  ├─ 添加 user message（tool_result: 含 exec_id，UI 渲染卡片）
  └─ 添加 assistant message（结果摘要）
  ↓
SubAgent.run()                       ← cron 直接执行（非流式）
  ├─ 采用 context-bounded-processing 技能策略
  └─ 自主执行工具完成任务
```

**关键点**：
- Cron **直接创建 SubAgent**，无需经过 RootAgent
- SubAgent 使用 `context-bounded-processing` 技能，适合后台自主执行
- Session 消息格式与主 Agent 调用 SubAgent 完全一致（tool_use + tool_result），UI 渲染 SubAgent 卡片，用户可在 session 中继续对话

### 2.2 组件关系

```
┌─────────────────────────────────────────────────────────────┐
│                     Cron Scheduler                          │
│                    core/cron.py                             │
│                                                             │
│  ┌──────────────────┐     ┌──────────────────────────────┐  │
│  │ CronScheduler    │────▶│ CronTask (N个)               │  │
│  │ (全局单例)       │     │ - config from JSON           │  │
│  │ - 后台线程       │     │ - task_fn (optional)         │  │
│  │ - 每60秒检查     │     │ - workspace_uuid             │  │
│  │ - per-ws lock    │     │ - 状态追踪                   │  │
│  └──────────────────┘     └──────────────┬───────────────┘  │
│                                          │                   │
│                               ┌──────────▼───────────┐       │
│                               │ SubAgent           │       │
│                               │ → session (via state) │      │
│                               │ → 非流式执行        │      │
│                               │ → 结果写入 session  │      │
│                               └──────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

### 2.3 Workspace 结构

```
System workspace (UUID: "system", cwd: "data/")
├── sessions/
│   └── {cron_session_id}/         # "[Cron]" session
│       └── index.json             # cron 执行结果
└── setting.json                   # workspace_name: "System", system: true

用户 workspace (UUID: "abc123", cwd: "workspace/xxx")
├── sessions/
│   ├── {user_session_1}/          # 用户主对话
│   └── {cron_session_id}/         # "[Cron]" session
└── setting.json
```

### 2.4 生命周期

```
应用启动流程：
main.py
  └─ _setup_directories()
       └─ 创建 data/agents/system/sessions/ + setting.json
  └─ start_scheduler()           # 创建 CronScheduler 单例
       └─ scheduler.start()
            ├─ load_tasks()      # 从 core/cron.d/*.json + user_tasks.json 加载
            └─ 启动后台线程      # 每 60 秒检查任务

应用关闭流程：
web/web_api.py (lifespan shutdown)
  └─ stop_scheduler()
       └─ scheduler.stop()
            └─ 等待线程结束 (timeout=5s)
```

---

## 三、System Workspace

System workspace 是一个特殊的 workspace，用于系统维护和定时任务。

### 3.1 基本属性

| 属性 | 值 |
|------|-----|
| UUID | `system` |
| 目录 | `data/agents/system/` |
| cwd | `data/`（项目数据根目录） |
| 用途 | 系统维护、定时清理等 |

### 3.2 初始化

`main.py` 启动时自动创建：

```python
system_ws_dir = data/agents/system
system_ws_dir/sessions/         # session 存储
system_ws_dir/setting.json      # workspace 配置（含 "system": true 标志）
```

### 3.3 管理规则

| 操作 | 是否允许 | 说明 |
|------|----------|------|
| UI 可见 | ✅ | 在下拉列表中显示，带 ⚙ 标记 |
| 查看 session | ✅ | 可以查看 cron 执行历史 |
| 删除 session | ✅ | 可以清理 cron 历史 |
| 删除 workspace | ❌ | API 返回 403，UI 隐藏按钮 |
| 修改 workspace | ❌ | API 返回 403，UI 禁用输入 |

### 3.4 任务路由

- 系统级任务（`core/cron.d/`）：默认走 System workspace
- 用户级任务（`cron` 工具）：默认走当前 workspace
- 可在 `cron` 工具或 task 返回中显式指定 `workspace_uuid`

---

## 四、任务配置

### 4.1 JSON 配置文件

每个系统级任务对应一个 `core/cron.d/*.json` 文件：

```json
{
  "name": "extract-user-info",
  "description": "每天提取用户画像",
  "enabled": true,
  "workspace_uuid": "system",
  "schedule": {
    "type": "cron",
    "expr": "0 2 * * *"
  },
  "config": {
    "max_executions": 9999
  },
  "content": {
    "task": "扫描工作区，提取用户信息到 user-profile.json",
    "plan": ["列出工作区目录", "逐个提取并写入 profile"]
  }
}
```

**字段说明**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 任务唯一标识 |
| `description` | string | 否 | 任务描述 |
| `enabled` | bool | 否 | 是否启用（默认 true） |
| `workspace_uuid` | string | 否 | 目标 workspace（空=System） |
| `content` | string/dict/list | 是 | 任务来源（见下文） |
| `schedule` | object | 是 | 调度配置 |
| `schedule.type` | string | 是 | `interval` 或 `cron` |
| `schedule.minutes` | int | interval | 执行间隔（分钟） |
| `schedule.expr` | string | cron | 标准 5 字段 cron 表达式 |
| `config` | object | 否 | 额外配置 |
| `config.max_executions` | int | 否 | 最大执行次数（1-9999，默认 9999） |

### 4.2 调度类型

**interval**：按分钟间隔
```json
{"type": "interval", "minutes": 60}
```

**cron**：标准 5 字段 cron 表达式（分 时 日 月 周）
```json
{"type": "cron", "expr": "0 2 * * *"}
```

支持的语法：`*`（任意）、`*/N`（每 N）、`M-N`（范围）、`N,M`（列表）

周几取值：0=Sunday, 1=Monday, ..., 6=Saturday

### 4.3 `content` 字段的三种格式

**格式 1：内联单任务（推荐）**

```json
{
  "content": {
    "task": "执行某个定期任务",
    "plan": ["步骤一", "步骤二", "步骤三"]
  }
}
```

**格式 2：Python 文件路径（动态任务）**

```json
{
  "content": "my_task.py"
}
```

Python 文件必须导出 `get_tasks() -> list[dict]`：

```python
def get_tasks() -> list[dict]:
    return [
        {"task": "任务描述", "plan": ["步骤1"], "workspace_uuid": "abc123"},
    ]
```

**格式 3：内联多任务**

```json
{
  "content": [
    {"task": "任务1", "plan": ["步骤A"]},
    {"task": "任务2", "plan": ["步骤B"]}
  ]
}
```

### 4.4 任务函数返回值

`get_tasks()` 返回的任务列表格式：

```python
[
    {
        "task": "任务目标描述",
        "plan": ["有序步骤列表"],
        "workspace_uuid": "abc123"   # 可选，空=System
    },
    ...
]
```

**执行规则**：
- 返回空列表 `[]` → 跳过本次执行
- 返回包含任务的列表 → 为每个任务在目标 workspace 执行
- 抛出异常 → 跳过本次执行，记录错误日志

### 4.5 用户级任务

用户通过 `cron` 工具创建的任务保存在 `data/cili/cron.d/user_tasks.json`：

```json
[
  {
    "name": "深圳天气查询",
    "workspace_uuid": "abc123",
    "description": "查询深圳天气",
    "enabled": true,
    "schedule": {"type": "interval", "minutes": 10},
    "content": {"task": "...", "plan": []}
  }
]
```

`workspace_uuid` 默认为创建时的当前 workspace。

---

## 五、数据存储结构

### 5.1 目录结构

```
data/cili/cron.d/
├── user_tasks.json              # 用户级定时任务配置
└── state/                       # 运行时状态（每个任务一个文件）
    ├── extract-user-info.json   # {"last_run": "...", "run_count": 5, "session_id": "431ea12b"}
    └── daily-report.json

data/agents/system/
├── setting.json                 # System workspace 配置
└── sessions/
    └── {session_id}/            # "[Cron] 任务描述" session
        └── index.json           # cron 执行记录
```

> **注意**：不再有独立的 `log/` 目录。Cron 执行结果保存在 workspace 的 session 中，与其他对话统一由 SessionManager 管理。

### 5.2 状态文件格式

**文件位置**：`data/cili/cron.d/state/{task_id}.json`

每个任务一个独立的状态文件，用于重启后恢复调度：

```json
{
  "last_run": "2026-08-25T22:40:32.128806",
  "run_count": 5,
  "session_id": "431ea12b",
  "remaining": 9994
}
```

- `last_run`、`run_count`：重启后恢复调度（计算 next_run）
- `session_id`：关联的 session UUID，避免每次按名称查找
- `remaining`：剩余执行次数（配合 loop 工具使用，详见"自循环任务"章节）

**启动流程**：
1. 加载任务配置（`core/cron.d/*.json` + `user_tasks.json`）
2. 读取 `state/{task_id}.json` 恢复 `last_run` 和 `session_id`
3. 根据 `last_run` 计算 `next_run`
4. 如果 `next_run <= now`，立即执行；否则等待到期

### 5.3 重启后恢复

| 场景 | 行为 |
|------|------|
| 首次运行（无状态文件） | `next_run=None` → 立即执行 |
| 重启后未到时间 | `next_run > now` → 等待到期 |
| 重启后已过时间 | `next_run <= now` → 立即执行 |

---

## 六、执行流程

### 6.1 调度循环

```
后台线程 (每60秒):
│
├─ 获取当前时间
│
├─ 遍历所有 CronTask
│   ├─ task.should_run(now)?
│   │   ├─ 首次运行 → true
│   │   ├─ 未启用 → false
│   │   └─ 当前时间 >= _next_run → true
│   │
│   └─ 如果 should_run:
│       ├─ 立即设置 _next_run = None（防止重复触发）
│       └─ 启动新线程 → _execute_task(task, now)
│           └─ 获取 per-workspace lock
│               ├─ 已锁定 → 跳过（另一个 cron 在同 workspace 执行）
│               └─ 获取成功 → task.execute() → mark_executed()
│
└─ 等待 60 秒（Event.wait，可被 stop() 提前唤醒）
```

### 6.2 任务执行

```
CronTask.execute():
│
├─ 调用 task_fn() 获取任务列表
│   └─ 空列表 → 返回 {"status": "skipped"}
│
├─ 遍历任务列表
│   ├─ 解析目标 workspace（task_item.workspace_uuid > self.workspace_uuid > "system"）
│   ├─ 调用 _execute_in_session(workspace_uuid, task_item)
│   │   ├─ _resolve_workspace_dir() — "system" → data/, 其他 → data/agents/{uuid}/
│   │   ├─ _resolve_cron_session() — 用 state 中的 session_id 直接定位（不存在则新建）
│   │   │   └─ 新 session 名字为 "[Cron] 任务描述"
│   │   ├─ 加载 SessionManager，添加 user message + assistant message（tool_use: subagent）
│   │   ├─ 生成 exec_id，创建 SubAgent 日志目录
│   │   ├─ 创建 SubAgent(task, plan, workspace_uuid, cwd, session_dir, exec_id)
│   │   ├─ subagent.run() — 非流式自主执行
│   │   ├─ 添加 user message（tool_result: 含 exec_id）+ assistant message（摘要）
│   │   ├─ 保存 SubAgent 执行日志
│   │   └─ subagent.close()
│   └─ 收集结果
│
└─ 汇总所有结果
    ├─ 全部成功 → {"status": "completed"}
    └─ 部分失败 → {"status": "partial"}
```

### 6.3 自循环任务（remaining 计数器）

CronScheduler 维护通用的 `remaining` 计数器，每次执行递减，到 0 自动 disable。配合 `loop` 工具可实现跨调度周期的自循环任务。

**执行流程**：

```
CronScheduler._execute_task(task):
│
├─ 读取 remaining（从 state 或 config.max_executions 初始化）
│   └─ remaining = state.get("remaining", config.get("max_executions", 9999))
│
├─ 递减 remaining
│   └─ remaining -= 1
│
├─ 检查终止条件
│   ├─ remaining <= 0 → 自动 disable → 不执行
│   └─ remaining > 0 → 继续执行 SubAgent
│
├─ task.execute()
│
└─ mark_executed() → 保存状态
```

**与 loop 工具配合**：

loop 工具用于跟踪批量任务进度（如处理大量文件）。SubAgent 通过 `loop(action="next", source_file="file_list.txt")` 自动加载项列表并获取下一个待处理项，用 `loop(action="done/fail")` 跟踪进度。

```
示例：导入 10005 个文件

准备工作：
  1. 扫描文件列表，写入 file_list.txt（每行一个文件路径）
  2. 创建 cron 任务，设置 max_executions 足够大（如 9999）

每次执行：
  1. loop(action="next", source_file="file_list.txt") → 获取下一个待处理文件（自动加载）
  2. 处理文件
  3. loop(action="done", source_file="file_list.txt", item=当前文件) → 标记完成
  4. loop(action="status", source_file="file_list.txt") → 查看进度

任务终止：
  - 所有文件处理完毕 → pending=0 → SubAgent 自行结束
  - 或 cron.remaining 递减到 0 → 自动 disable
```

**重新激活**：

用户手动 `cron(action="enable")` 时，remaining 重置为 `config.max_executions`。如果源目录新增文件，更新 file_list.txt 后 loop(next) 会发现并继续处理。

### 6.3 Cron Message 格式

注入到 session 的 user message：

```
[Cron 定时任务触发]

## 任务描述
扫描工作区，提取用户信息到 user-profile.json

## 执行计划
1. 列出工作区目录
2. 逐个提取并写入 profile

请根据任务描述执行。
```

### 6.4 并发控制

- `per-workspace lock` → 同一 workspace 的多个 cron 任务串行化执行
- 不同 workspace 的 cron 任务可以并行

---

## 七、核心类

### 7.1 CronScheduler

```python
class CronScheduler:
    tasks: list[CronTask]              # 所有任务
    _running: bool                     # 运行标志
    _thread: Thread                    # 后台线程
    _lock: Lock                        # 线程安全锁
    _wake_event: Event                 # 提前唤醒
    _workspace_locks: dict[str, Lock]  # per-workspace 执行锁
```

**主要方法**：

| 方法 | 说明 |
|------|------|
| `start()` | 加载任务并启动后台线程 |
| `stop()` | 停止调度器，等待线程结束 |
| `load_tasks() -> int` | 从 `core/cron.d/*.json` + `user_tasks.json` 加载 |
| `get_task(name) -> CronTask` | 按名称获取任务 |
| `list_tasks() -> list[dict]` | 列出所有任务及状态 |
| `run_task_now(name) -> dict` | 立即触发指定任务 |
| `_get_workspace_lock(ws_uuid) -> Lock` | 获取 workspace 级锁 |

### 7.2 CronTask

```python
class CronTask:
    name: str                     # 任务名称
    description: str              # 任务描述
    enabled: bool                 # 是否启用
    workspace_uuid: str           # 目标 workspace（空=System）
    schedule: dict                # 调度配置
    config: dict                  # 额外配置（含 max_executions）
    task_id: str                  # 任务 ID
    _next_run: datetime           # 下次运行时间
    _last_run: datetime           # 上次运行时间
    _run_count: int               # 运行计数
    _remaining: int | None        # 剩余执行次数（配合 loop 工具）
    _task_fn: Callable            # 任务函数（可选）
```

**主要方法**：

| 方法 | 说明 |
|------|------|
| `should_run(now) -> bool` | 检查是否应该运行 |
| `get_tasks() -> list[dict]` | 调用 task_fn 获取任务列表 |
| `execute() -> dict` | 通过 SubAgent 执行所有任务 |
| `mark_executed(now, result)` | 更新状态并持久化 |
| `_execute_in_session(ws, item) -> dict` | 在 workspace session 中通过 SubAgent 执行 |
| `_resolve_workspace_dir(uuid) -> str` | 解析 workspace 目录 |
| `_resolve_cron_session(dir) -> str` | 用 state 中的 session_id 定位，不存在则新建（名字用任务描述） |
| `_build_cron_message(item) -> str` | 构造 user message |
| `_calculate_next_run(from_time)` | 支持 interval 和 cron 表达式 |
| `to_dict() -> dict` | 序列化为字典（含 workspace_uuid） |

---

## 八、公共 API

### 8.1 模块级函数

```python
from core.cron import start_scheduler, stop_scheduler, get_scheduler

scheduler = start_scheduler() -> CronScheduler  # 启动
stop_scheduler()                                # 停止
scheduler = get_scheduler() -> CronScheduler    # 获取单例
```

### 8.2 调度器方法

```python
scheduler = get_scheduler()

tasks = scheduler.list_tasks() -> list[dict]
# [{"name": "...", "workspace_uuid": "...", "enabled": true, 
#   "schedule": {...}, "last_run": "...", "next_run": "...", "run_count": 5}]

task = scheduler.get_task("extract-user-info") -> CronTask | None
result = scheduler.run_task_now("extract-user-info") -> dict
# {"status": "triggered", "message": "Task extract-user-info triggered"}
```

---

## 九、设计决策

### 9.1 为什么直接用 SubAgent 而不是 RootAgent？

- **后台任务特性**：Cron 是后台任务，无需流式输出和用户交互能力
- **资源效率**：SubAgent 非流式执行，比 RootAgent 更轻量
- **技能匹配**：SubAgent 使用 `context-bounded-processing` 技能，适合自主执行
- **UI 可见**：session 消息格式与主 Agent 一致（tool_use + tool_result），SubAgent 卡片正常渲染

### 9.2 为什么引入 System workspace？

- **职责分离**：系统维护任务与用户工作区隔离
- **统一模型**：所有 SubAgent 必然属于某个主 Agent，System workspace 为系统 cron 提供"宿主"
- **安全保护**：不能删除/修改，防止误操作

### 9.3 为什么 "[Cron]" 独立 session？

- **不污染主对话**：cron 注入的消息不会出现在用户的正常对话中
- **集中管理**：所有 cron 历史在一个 session 中，便于查看和清理
- **可恢复**：session 持久化，重启后保留

### 9.4 为什么支持 cron 表达式？

- **精确调度**：支持"每天 2 点"、"每周一 9 点"等精确时间
- **行业标准**：5 字段 cron 表达式广泛使用，用户容易理解
- **interval 补充**：interval 适合简单间隔，cron 适合日历时间

---

## 十、相关文件

| 文件 | 职责 |
|------|------|
| `core/cron.py` | CronScheduler + CronTask + cron 表达式解析 + remaining 计数器 |
| `core/cron.d/*.json` | 系统级任务配置 |
| `core/tools/shared/cron_tool.py` | 用户级 cron 管理工具（含 max_executions 参数） |
| `core/tools/shared/loop.py` | 循环任务进度追踪（配合 cron 实现自循环任务） |
| `data/cili/cron.d/user_tasks.json` | 用户级任务配置 |
| `data/cili/cron.d/state/` | 任务状态追踪（含 remaining 计数器） |
| `data/cili/tools/loop/` | loop 工具状态文件 |
| `data/agents/system/` | System workspace |
| `main.py` | 启动调度器，创建 System workspace |
| `web/web_api.py` | 停止调度器，System workspace 保护 |

---

**文档版本**: v2.1
**创建时间**: 2026-08-25
**更新时间**: 2026-09-01
**状态**: 已实现（SubAgent 直接执行、System workspace、cron 表达式支持、remaining 计数器、loop 工具集成）
