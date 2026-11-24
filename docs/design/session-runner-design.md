# Session Runner 设计

> 按当前实现重写（2026-11-25）。

## 一、分层

```
core/base_session_runner.py      BaseSessionRunner：状态与兼容转发层（消息、用量、sink）
core/session_runner.py           SessionRunner：唯一子类，按 role 分叉（interactive / autonomous）
core/session_runner_runtime/
  conversation.py                ConversationStore：消息状态、提交视图、用量记账
  executor.py                    TurnExecutor：一轮执行（压缩→LLM→工具→落盘/事件）
  loop.py                        Loop / LoopPolicy / PhaseMachine：回合循环与预算/阶段
  tool_batch.py                  工具批执行（并发安全工具并行）
core/session_registry.py         SessionRegistry：runner 池、运行权认领、Attachment
core/output_sink.py              OutputSink：9 个回调的统一输出接收端
core/turn_dispatch.py            prepare_turn / execute_turn：接入端共享的回合驱动
core/turn_hooks.py               回合后钩子（记忆提取、git 自动提交）
core/turn_queue.py               用户回合排队
core/session_control.py          快照/停止回合/停止后台
core/session_purge.py            会话删除唯一顺序
core/compression.py              消息压缩工具（microcompact/token 计数）
```

`BaseSessionRunner` 的压缩/循环/工具执行实现均在 runtime；基类保留状态与转发，
以保证测试与子类兼容。

## 二、角色分叉

`SessionRunner.__init__(config, role=, cwd=, workspace_uuid=, session=…, exec_id=…)`：

| role | mode | 会话 | 特点 |
|---|---|---|---|
| master | interactive | SessionStore（持久，可切换/分叉） | 用户通道（ask_user/审批卡），工具最全 |
| worker | autonomous | SessionIdentity（仅标识与目录） | 无用户通道，可续轮（continuation） |
| lite | autonomous | SessionIdentity | 工具最少，用于判定器/轻任务 |

- 角色定义在 `core/agents/{master,worker,lite}.json`（`core/role_config.py` 加载）。
- 模型解析：`config.model_for_role(role)`；会话级覆盖存 `metadata.model_ref`。
- 工具实例化：`kind` 由 `registry.create_tools(ToolBuildContext)` 按白名单创建；
  `_eligible_tools()` 再做接口门控与服务端工具门控。

## 三、一轮执行（TurnExecutor）

`run_round(streaming, system_prompt)`：

1. `_check_and_compress()`：三层压缩（`core/compression` + full compact 摘要）。
2. `build_projected_messages()`：按图片历史策略投影（`images.history_mode`：full / current_turn；
   current_turn 下历史图片降级为路径引用，并受请求体预算约束）。
3. 调用 LLM（`core/llm` client，流式/非流式；413 去图重试、重试清屏哨兵）。
4. 工具调用：`tool_batch` 分批（`concurrency_safe` 并行，其余串行）；`execute_tool`
   负责占位、流式输出文件、结果外置、截断与 `_meta`（含 `ask_user_completed` 占位语义）。
5. 落盘：`ConversationStore.sync_to_session()` 写 index.json commits；事件经 `OutputSink`。

`Loop`（`loop.py`）：

- `run_interactive()`：直到无工具调用、预算触顶或外部等待（ask_user/审批卡）。
- `run_autonomous()`：迭代上限、check 阶段、预算收尾、失败计数；返回结果 dict。
- 预算文案、阶段机、失败阈值由 `LoopPolicy` 提供。

## 四、回合驱动（接入端共享）

`core/turn_dispatch`：

- `prepare_turn(runner, content=…, images=…, attachments=…, interface=…)`：
  解析附件/图片、检测待答占位（resume 模式）、claim 运行权。
- `execute_turn(prepared, sink, after_turn_hooks=True)`：执行并落盘；异常转 `error_notice` 消息；
  成功后触发 `turn_hooks`（记忆提取 + git 自动提交）。
- 接入端职责：提供 sink（Web SSE / QQ 单流 / 微信 defer / 邮件回信）与出站交付。

## 五、会话池与并发

`SessionRegistry`（`core/session_registry.py`）：

- `get_or_create / register_hosted`：按 `workspace:session` 键创建或复用 runner；
  超出上限时 `evict_idle`。
- `claim(key) / release(key)`：运行权认领（防双循环改写消息）。
- `attach/detach`：`Attachment` 记录会话附着（接口无关）。
- `remove/remove_workspace/shutdown_all`：删除与停机；删除统一走 `core.session_purge`。

回合排队（`core/turn_queue.py`）：运行中发送的消息入队，可在运行边界合并插入；
`queue_update` 事件同步前端。

## 六、输出与事件

`OutputSink`（9 回调）：`on_text / on_thinking / on_tool_call / on_tool_output / on_tool_result /
on_session_start / on_session_progress / on_session_complete / on_turn_complete`。

- 请求级 sink：Web SSE（`web/sse`）、QQ 单流（`interfaces/qq/sink`）、微信、邮件。
- 全局 sink：`core/default_sink`（事件总线；后台回合、文件交付、`turn_complete`）。
- `layer_sink(base, override)`：按已设置字段覆盖；接入端手动扇出到 base。

## 七、会话持久化

`SessionStore`（`core/session.py`）目录：

```
{workspace_dir}/.cili/sessions/{short_id}/
  messages.jsonl   完整内容历史（追加式）
  index.json       模型视图 commits（seq 引用 + 块级 _meta + 摘要提交）
  meta.json        session_id/name/metadata（通道三元组/模型覆盖等）
  {tool_use_id}.txt / exec_{id}/  工具输出与子代理日志
```

- 双读路径：UI 读 jsonl 完整历史，模型读 commits 视图。
- 压缩/失效只改 commits；revert/clear 是唯一显式截断 jsonl 的例外。
- 内部 `_meta` 字段（`INTERNAL_META_FIELDS`）不进入模型上下文。
- `SessionIdentity`：autonomous 会话的最小身份（session_id/session_dir/master_session_id）。

## 八、fork 与恢复

- `SessionRunner.fork_session(...)`：克隆快照（或超限时生成 handoff 文档）到兄弟会话，
  登记注册表、绑定默认 sink、注册邮箱；`run_sync` 供工作流同步执行。
- `resume_after_ask_user()` / `resume_from_notification()`：答案提交与后台通知恢复。
- 审批卡超时由 `core/approval_watchdog` 结算；Web/CLI 可自动恢复循环
  （`auto_resume` 只覆盖有推送通道的接口）。

## 九、测试约定

- 单元：`test_base_runner_unit.py`；集成（DGX）：`test_base_runner_integration.py`。
- 角色：`test_master_runner / test_worker_runner / test_lite_runner`。
- 回合驱动：`test_ask_user`、`test_approval_runner_e2e`、`test_channel_session`。
- 伪造 runner：只实现被测表面（如 `session`/`default_sink`/`is_running`），不要依赖 MagicMock 伪属性。
