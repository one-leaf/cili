# 会话存储与消息管理设计

> 按当前实现重写（2026-11-25）。代码：`core/session.py`。

## 一、目录布局

```
{workspace_dir}/.cili/sessions/
  {short_id}/                 8 位十六进制会话目录
    messages.jsonl            完整内容历史（追加式；UI 直接读取）
    index.json                模型视图：{schema_version, next_seq, commits[]}
    meta.json                 会话属性：session_id / name / metadata
    {tool_use_id}.txt         主代理工具输出（流式写入，前端轮询）
    exec_{id}/                子代理目录：index.json 执行日志 + 工具输出
```

工作区数据目录由 `core.config.get_workspace_data_dir` 决定；sessions 子目录由
`get_workspace_sessions_dir` 提供。

## 二、双读路径

| 读者 | 数据源 | 说明 |
|---|---|---|
| UI | `messages.jsonl` + `index.json` commits | 合并块级 `_meta` 渲染完整历史 |
| 模型 | `index.json` commits | seq 引用 jsonl 内容；摘要提交内嵌文本 |

- 块级 `_meta` **只存 index.json 的 `commits[].blocks`**（键为 tool_use_id/id）；
  jsonl 行只保留内容（`_strip_block_meta`）。
- 消息级 `_meta` 在 commits 的 `msg_meta`；`INTERNAL_META_FIELDS`（如 `valid`、
  `tool_name`、`ask_user_completed`、`exec_id`）不进入模型上下文。
- 压缩/失效只改 commits；`revert`/`clear` 是唯一显式截断 jsonl 的例外。

## 三、写入与提交

- `SessionStore.add_message(role, content, meta=None, flush=False)`：写内存；
  `flush=True` 落盘（回合收尾路径必须 flush）。
- `save()` / `save(force=True)`：把内存消息同步为 jsonl 行 + commits（`_commit_for_message`）。
- 摘要提交：`_summary_commit`（`summary` 内嵌，不进 jsonl）。
- 追加式 jsonl：`append_jsonl`；损坏行（断尾）丢弃该行及之后内容（`read_jsonl`）。
- 并发：每会话目录一把写锁（`_SESSION_LOCKS`），删除时释放。

## 四、内部字段与过滤

`INTERNAL_META_FIELDS`（`is_message_in_context` 判定）：`tool_name`、`multimodal`、
`completed`、`ask_user_completed`、`answered`、`exec_id`、`valid` 等。

- `valid=False` 的消息不进入模型上下文（但保留在 jsonl）。
- 图片历史策略：`image_history_mode`（full / current_turn）；current_turn 下历史图片
  写入引用文本（`_jsonl_line`）。
- 已回答的 tool_result 在模型视图重建时清空内联内容（`_apply_model_content_rules`），
  大结果由 `output_path` 引用。

## 五、执行日志（ExecLogStore）

`session.agent_logs`：

- `_generate_exec_id()`：`exec_` + 短 ID。
- `load_agent_log(exec_id)`：读取子代理执行日志（优先 `run.json` 元数据，回退 `index.json`）。
- `list_agent_logs()`、`delete_agent_log()`：列表与删除（删除走青少年守卫）。
- 运行元数据的写入在 `core/agent_run.AgentRunStore/Manager`（`run.json` + 轮次）。

## 六、用量记账

- 运行时：`ConversationStore.update_usage`（token/缓存/API 次数 + 每模型分桶 + 速度）。
- 汇总：`usage_total_for_session_dir`（cwd 扫描 commits 派生）。
- `SessionStore.update_usage` 仅保留给测试与历史数据（生产路径不调用）。

## 七、会话操作

| 操作 | 入口 |
|---|---|
| 新建 | `SessionStore.create_new_session(sessions_dir, name, metadata=)` |
| 列表 | `SessionStore.list_sessions` / `list_sessions_meta_only` |
| 切换/附着 | runner `attach_session` / `switch_session` |
| 分支名 | `default_branch_name`（fork 命名） |
| 删除 | `core.session_purge.purge_session` → `delete_session_dir`（青少年 hard 守卫） |
| 导出/导入 | `GET …/export`、`POST …/import`（web 路由） |
| 时间线 | `core/session_timeline.build_timeline` |
| 会话键 | `session_key(ws, sid)` / `split_session_key(key)` |

## 八、格式兼容

- 旧消息格式由 `core/migration.migrate_sessions_dir` 在启动时迁移（`ensure_new_format`）。
- 旧块级 `_meta.completed` 由启动迁移重写为 `ask_user_completed`（保留旧键双读）。
