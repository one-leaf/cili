# TodoWrite 任务规划工具设计文档

本文档描述 Cili Agent 的任务规划工具（TodoWrite）设计。

---

## 一、设计背景

任务规划工具为 Agent 提供结构化的任务清单能力，用于：
- 规划多步骤复杂任务
- 展示当前进度
- 追踪已完成/进行中/待办任务

### 1.1 Cili 设计方案

- **整表替换**：每次调用发送完整列表
- **三态状态**：`pending` / `in_progress` / `completed`
- **双形式描述**：`content`（命令式）+ `activeForm`（进行时，UI 显示）
- **并行控制**：可配置 `allow_parallel_in_progress`
- **验证提醒**：完成多任务时提醒验证
- **存储方式**：独立文件（`data/cili/tools/todo/{session_id}.json`，按 session 隔离）

---

## 二、数据结构

### 2.1 TodoItem

```python
{
    "content": "修复认证 bug",           # 命令式：描述要做什么
    "activeForm": "正在修复认证 bug",     # 进行时：UI 显示当前正在做什么
    "status": "pending" | "in_progress" | "completed"
}
```

**字段说明**：
- `content`：任务的命令式描述，如 "Run tests"、"Fix auth bug"
- `activeForm`：任务的进行时描述，用于 UI 显示当前进行中的任务
- `status`：任务状态
  - `pending`：未开始
  - `in_progress`：正在进行（默认只允许一个）
  - `completed`：已完成

### 2.2 存储格式

Todos 存储在独立文件中（按 session 隔离），路径为 `data/cili/tools/todo/{session_id}.json`：

```json
{
    "session_id": "abc12345",
    "updated_at": "2026-08-29 10:30:00",
    "todos": [
        {"content": "分析代码结构", "activeForm": "正在分析代码结构", "status": "completed"},
        {"content": "重构工具系统", "activeForm": "正在重构工具系统", "status": "in_progress"},
        {"content": "编写测试用例", "activeForm": "正在编写测试用例", "status": "pending"}
    ]
}
```

`updated_at` 记录最近一次更新的时间戳（格式 `yyyy-MM-dd HH:mm:ss`）。旧版存储在 session metadata 中的 `todos` 数据会在工具执行时自动迁移到独立文件，并从 metadata 中移除（向后兼容）。

---

## 三、工具 Schema

### 3.1 工具定义

```python
class TodoWriteTool(Tool):
    name = "todo_write"
    description = """
    Use this tool to create and manage a structured task list for your current
    coding session. This helps track progress, organize complex tasks, and
    demonstrate thoroughness.

    ## When to Use
    1. Complex multi-step tasks (3+ distinct steps)
    2. User provides multiple tasks (numbered or comma-separated)
    3. After receiving new instructions - capture requirements as todos
    4. Mark task as in_progress BEFORE starting work
    5. Mark task completed IMMEDIATELY after finishing

    ## When NOT to Use
    - Single straightforward task
    - Trivial tasks (< 3 steps)
    - Purely conversational requests

    ## Task States
    - pending: Not yet started
    - in_progress: Currently working on (limit to ONE at a time)
    - completed: Finished successfully

    ## Task Format
    Each task must have TWO forms:
    - content: Imperative form (e.g., 'Run tests')
    - activeForm: Present continuous form (e.g., 'Running tests')

    IMPORTANT: Send the ENTIRE list on every call - it REPLACES the previous list.
    """
```

### 3.2 参数 Schema

```json
{
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "description": "The COMPLETE task list, replacing any previous list.",
            "items": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "What the task is - imperative form"
                    },
                    "activeForm": {
                        "type": "string",
                        "description": "Present continuous form shown in UI"
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                        "description": "Task state"
                    }
                },
                "required": ["content", "activeForm", "status"]
            }
        }
    },
    "required": ["todos"]
}
```

### 3.3 返回格式

**成功返回**：
```
Todos have been updated successfully.
Progress: 2/5 completed
```

**结构化 meta**（用于 UI）：
```json
{
    "todos": [...],
    "counts": {
        "total": 5,
        "pending": 2,
        "in_progress": 1,
        "completed": 2
    },
    "verification_nudge": false
}
```

**错误返回**：
- `Error: todos must be an array`
- `Error: todo item 0 'content' must be a non-empty string`
- `Error: duplicate todo content "Fix bug"`
- `Error: at most one task may be in_progress at a time (got 2)`

---

## 四、验证规则

### 4.1 输入验证

1. `todos` 必须是数组
2. 每个 item 必须是对象
3. `content` 必须是非空字符串
4. `activeForm` 必须是非空字符串
5. `status` 必须是 `pending`/`in_progress`/`completed` 之一
6. `content` 不能重复

### 4.2 并行控制

当 `allow_parallel_in_progress = false`（默认）时：
- 最多只能有一个 `in_progress` 任务
- 多个 `in_progress` 会返回错误

当 `allow_parallel_in_progress = true` 时：
- 允许多个 `in_progress` 任务（适合并行执行）

### 4.3 验证提醒

当以下条件全部满足时，返回验证提醒：
1. 所有任务都已完成
2. 任务数量 ≥ 3
3. 没有任何任务包含 "verify"、"test"、"check"、"validate"、"confirm" 等关键词（关键词列表为 `["verify", "test", "check", "validate", "confirm"]`）
4. 之前的状态有未完成的任务

---

## 五、前端渲染

### 5.1 SSE 事件推送

当 `todo_write` 工具执行成功后，服务端推送 SSE 事件：

```json
{"type": "todo_update", "todos": [...]}
```

### 5.2 渲染组件

Todo 列表显示在聊天区域顶部，包含：
- **标题**：📋 任务清单
- **进度**：X/Y 完成
- **进度条**：绿色填充条
- **任务列表**：
  - ○ pending（灰色）
  - ◉ in_progress（黄色高亮，脉冲动画）
  - ✓ completed（绿色，删除线）

### 5.3 CSS 样式

```css
.todo-list-container { ... }
.todo-header { ... }
.todo-progress-bar { ... }
.todo-item.todo-pending { ... }
.todo-item.todo-in-progress { ... }
.todo-item.todo-completed { ... }
```

---

## 六、使用场景示例

### 6.1 何时使用

**示例 1：多步骤功能开发**
```
用户：我想在应用设置中添加暗黑模式切换。完成后运行测试和构建。

Agent 创建任务列表：
1. [ ] 在设置页面创建暗黑模式切换组件
2. [ ] 添加暗黑模式状态管理（context/store）
3. [ ] 实现 CSS-in-JS 暗黑主题样式
4. [ ] 更新现有组件支持主题切换
5. [ ] 运行测试和构建，修复失败问题
```

**示例 2：批量重构**
```
用户：帮我把项目中所有的 getCwd 函数重命名为 getCurrentWorkingDirectory

Agent 搜索发现 15 处引用，创建任务列表：
1. [ ] 更新 src/utils/path.ts
2. [ ] 更新 src/config/loader.ts
3. [ ] 更新 test/path.test.ts
...
```

### 6.2 何时不使用

**示例 1：简单任务**
```
用户：Python 怎么打印 "Hello World"？
Agent：直接回答，不需要任务列表
```

**示例 2：单步操作**
```
用户：帮我给 calculateTotal 函数加个注释
Agent：直接执行，不需要任务列表
```

---

## 七、实现细节

### 7.1 源文件

| 文件 | 描述 |
|------|------|
| `core/tools/shared/todo.py` | TodoWrite 工具实现 |
| `core/tools/shared/__init__.py` | 工具注册 |
| `web/web_api.py` | SSE 事件推送 |
| `web/static/app.js` | 前端渲染 |
| `web/static/style.css` | 样式定义 |

### 7.2 工具注册

```python
# core/tools/shared/__init__.py

from core.tools.shared.todo import TodoWriteTool

def create_shared_tools(...):
    tools = [
        ...
        TodoWriteTool(cwd=cwd, workspace_uuid=workspace_uuid, session_manager=session_manager),
    ]
    return tools
```

### 7.3 SSE 推送

```python
# web/web_api.py

def on_tool_result(tool_name: str, output: str, is_error: bool, tool_use_id: str) -> None:
    event = json.dumps({"type": "tool_result", ...})
    event_queue.put(f"data: {event}\n\n")

    # Check for todo_write tool and push todo update event
    if tool_name == "todo_write" and not is_error and agent.session_manager:
        todos = get_todos_from_session(agent.session_manager)
        if todos is not None:
            todo_event = json.dumps({"type": "todo_update", "todos": todos}, ensure_ascii=False)
            event_queue.put(f"data: {todo_event}\n\n")
```

`get_todos_from_session` 是 `core/tools/shared/todo.py` 提供的辅助函数：通过 session_manager 的 session_id 读取独立文件 `data/cili/tools/todo/{session_id}.json` 中的 todos（兼容旧 metadata 格式并自动迁移）。

### 7.4 前端渲染

```javascript
// web/static/app.js

// SSE 事件处理
} else if (event.type === 'todo_update') {
    renderTodoList(event.todos);
}

// 渲染函数
function renderTodoList(todos) {
    // 计算统计
    const total = todos.length;
    const completed = todos.filter(t => t.status === 'completed').length;

    // 构建 HTML
    // ...
}
```

---

## 八、设计决策

### Q: 为什么用整表替换而不是增量更新？

**A**: 整表替换有以下优点：
1. **简单性**：无需处理部分更新、冲突合并
2. **一致性**：每次调用后状态完全确定
3. **与 Harness 一致**：参考成熟设计
4. **LLM 友好**：模型更容易理解和操作完整列表

### Q: 为什么需要 `activeForm` 字段？

**A**: `activeForm` 用于 UI 显示当前正在进行的任务：
- `content`: "Fix auth bug"（命令式）
- `activeForm`: "Fixing auth bug"（进行时）

这样用户可以看到 "正在修复认证 bug..." 而不是 "修复认证 bug"，更符合中文表达习惯。

### Q: 为什么存储在独立文件而不是 session metadata 或事件日志？

**A**: 三种方案各有优劣：
- **独立文件**（Cili 选择）：按 session 隔离（每个 session 一个文件 `data/cili/tools/todo/{session_id}.json`），与消息数据分离
- **Session metadata**（旧方案）：简单、随 session 持久化，但 todos 与大量消息数据混在一起
- **事件日志**（Harness 选择）：支持回放、历史追踪

Cili 选择独立文件方案是因为：
1. **数据隔离**：每个 session 一个文件（`{session_id}.json`），互不影响
2. **更新独立**：`updated_at` 字段在每次写入时刷新，记录最近更新时间，不依赖 session 的保存时机
3. **向后兼容**：旧 metadata 中的 `todos` 自动迁移到独立文件，`get_todos_from_session` 同时支持两种格式
4. 不需要复杂的事件溯源

### Q: 并行控制为什么是配置项？

**A**: 不同场景需要不同的并行策略：
- **单线程执行**：限制一个 `in_progress`，防止混乱
- **并行执行**（SubAgent）：允许多个 `in_progress`

通过 `allow_parallel_in_progress` 配置项，可以灵活适配不同场景。

---

## 九、未来扩展

1. **跨 Session 共享**：支持多个 agent 共享同一个 todo 列表
2. **优先级**：添加 `priority` 字段支持任务优先级排序
3. **子任务**：支持嵌套任务结构
4. **历史记录**：追踪 todo 列表的变化历史
5. **导出功能**：将 todo 列表导出为 Markdown/JSON
