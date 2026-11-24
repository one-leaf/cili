# TodoWrite 任务规划工具设计

> 按当前实现重写（2026-11-25）。代码：`core/tools/todo.py`、`core/todo_store.py`。

## 一、设计要点

1. **整表替换**：每次调用提交完整列表，不做增量更新、不做单条编辑。
2. **三态**：`pending / in_progress / completed`（不允许其他值）。
3. **单一所有者**：列表属于创建它的会话。
4. **独立存储**：`{workspace}/.cili/tools/todo/{session_id}.json`（按工作区、按会话）。

工具名与注册键一致：`todo_write`（历史 `todo` 注册键已废弃并迁移）。

## 二、参数与校验

```json
{ "todos": [ {"content": "修复登录 bug", "status": "in_progress"} ] }
```

- `todos` 必须为数组；每项为对象，`content` 非空，`status` 必须是三态之一。
- 重复内容拒绝（同表内去重）。
- 空数组合法：表示清空列表（并会发出一次 `todo_update` 事件）。

## 三、存储与读取

- `core/todo_store`：
  - `get_todo_file_path(session_id, workspace_uuid)`；
  - `read_todos` / `write_todos`（原子写）；
  - `_load_todos_file` 损坏文件返回 `None`（区别于合法的空列表 `[]`）。
- 兼容旧格式：早期存于 session metadata 的 todos 在读取时自动迁移到独立文件。

## 四、事件

`todo_update_event(tool_name, is_error, session, workspace_uuid)`：

- 仅当工具名 `todo_write` 且执行成功时产生；
- 内容 `{type: "todo_update", session_id, todos}`；
- 与接口无关：Web SSE、QQ、微信等传输层直接序列化下发，不需要知道工具名与数据来源。

## 五、前端渲染

- 聊天内渲染任务进度（`x/y 完成` 进度条 + 状态图标）。
- 历史加载时从会话消息/接口读取当前列表，保证刷新后仍显示。
- 工具在 master 的 `deferred_tools` 中（由 `tool_search` 激活）；worker 同理，lite/workflow 无此工具
  （workflow 有，用于监督者追踪）。

## 六、测试

`test_todo_tool.py` 覆盖整表替换、状态校验、重复内容、空列表、存储与事件；
`test_web_session_api`/`test_web_sse` 覆盖事件下发。
