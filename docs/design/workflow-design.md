# 工作流（Workflow）设计

> 按当前实现整理（2026-11-28）。代码：`core/workflows/`、`core/tools/workflow.py`、
> `core/tools/workflow_agent.py`、`web/routes_workflows.py`、`web/static/workflow-pane.js`、
> `core/session_registry.py`、`core/outbox.py`。

## 一、定位

一个工作流由若干**严格顺序**的步骤组成。执行时由一个业务无关的监督者会话逐步派发，
每一步由全新 worker/lite 会话执行；步骤之间不 fork、不共享会话，只通过**文件产物**传递数据。

- 监督者角色 `workflow`：只能看到步骤序号与名称，不接触业务需求；
- 执行器角色 `worker` / `lite`：由步骤定义决定，执行器内部不能再派发步骤（一层委派）；
- 产物契约：每一步必须写出 `steps/{n}/result.md`（或步骤声明的 `outputs`），缺失即判失败。

## 二、数据模型（`core/workflows/model.py`）

### Workflow

| 字段 | 约束 |
|---|---|
| `id` | 8 位小写十六进制（`secrets.token_hex(4)`，与 workspace/会话同算法） |
| `name` | 必填，≤50 字 |
| `description` | ≤2000 字 |
| `inputs` | ≤20 项，`key` 匹配 `[A-Za-z_][A-Za-z0-9_]{0,39}` 且唯一 |
| `steps` | 1–20 步 |
| `hidden` | 侧栏隐藏，与会话 `hidden` 同语义 |

### WorkflowStep

| 字段 | 说明 |
|---|---|
| `id` | 8 位十六进制；缺省生成，更新时带上以保留同一步骤 |
| `name` / `requirement` | 名称 ≤60 字；要求 ≤8000 字 |
| `sources` | 上游步骤：0-based 下标或步骤 id；落盘统一存 id；必须排在当前步骤之前且不重复 |
| `executor` | `worker` 或 `lite`，缺省 `worker` |
| `reads` | 显式读取的产物路径；缺省读上游 `result.md` |
| `outputs` | 显式声明的产物路径；与默认 `steps/{n}/result.md` 一起做存在性检查 |

### WorkflowInput

`key / label / type(text|textarea) / required / default / description`。运行前由面板表单或普通会话收集，
`validate_run_inputs` 校验必填、默认值、长度（单值 ≤4000），缺失时抛 `WorkflowInputRequiredError`。

### WorkflowState / StepState

- `WorkflowState.status`：`idle | running | completed | failed | stopped | interrupted`；
  记录 `run_id`、起止时间、`current_index`、`panel_session_id`、`results_session_id`、
  `origin_session_id`、`run_inputs`、`items`（按步骤 id）、`history`（最近 20 次）。
- `StepState`：`status(pending|running|done|failed|stopped|interrupted)`、`session_id`、`executor`、
  `artifact`、起止/耗时、`summary/error`、`truncated`、`completion_declared/declared_status`、
  `phase/current_tool/iterations/stats`。

## 三、存储布局

运行时（工作区数据，remote-wins 共享）：

```
{workspace}/.cili/workflows/{workflow_id}/
  workflow.json            # 定义
  state.json               # 当前状态 + 历史
  runs/{run_id}/run.json   # 单次运行快照（version=2）
  runs/{run_id}/events.jsonl
```

用户可见结果（普通工作区目录）：

```
{workspace}/workflows/{name}-{id}/{YYYYMMDD_HHmmSS}_{run_id}/
  inputs.md                # 运行参数（有输入时）
  steps/{n}/result.md      # 每步产物（默认契约）
  summary.md               # 运行汇总（状态 + 步骤产物 + 最终总结）
```

- 所有写盘走 `atomic_write_json` / 原子文本；进程内分三级锁：全局、工作流、运行；
- 每次运行结束 `prune_runs(keep=20)`，只保留最近 20 次运行目录；
- 结果目录名里的时间戳取本次运行开始时间。

## 四、运行模型（`core/workflows/runner.py`）

### 启动

`service.run_workflow`：

1. 读取定义与状态，`is_workflow_running` 忙检查；
2. `validate_run_inputs` 校验输入（缺参抛 `WorkflowInputRequiredError`，API 返回 `missing_inputs`，**不启动**）；
3. 写入 `origin_session_id`，启动 `WorkflowRunner` 后台线程；同一工作流同时只允许一个运行。

### 主循环 `_loop`

```
new run_id → reset_run → ensure_run_dir → inputs.md → run.json(initial)
→ ensure_results_session(fresh=True) → 创建监督者 SessionRunner
→ 对每个步骤：
    build_supervisor_prompt(i) → supervisor.run(prompt)
    → 期望监督者调用 workflow_agent(step=i) → 步骤状态离开 pending
    → pending 未执行 / failed / stopped 分别终止
→ 全部完成：state=completed → 监督者最终总结
→ 写 history、run_finished 事件、run.json 终态、summary.md、维护通知
```

- 监督者提示词只含步骤序号与名称，不传业务要求；
- `WorkflowAgentTool` 只在监督者会话动态注入，`executor` 完成实际步骤，执行器内没有该工具。

### 单步执行 `_execute_step`

1. 建 `steps/{n}/` 与步骤 `SessionStore`，写 `step_started` 事件；
2. `SessionRunner(config, role=executor, cwd=工作区, initial_session=步骤会话,
   master_session_id=结果会话)`，注册进 `SessionRegistry`；
3. 步骤会话输出通过 `OutputSink` 同时写自己的 sink 与父会话事件（text/thinking/tool_use/tool_result/tool_output）；
4. `STEP_TIMEOUT_SECONDS = 3600` 定时器，超时置 `_step_timed_out` 并停止 runner；
5. 提取本轮 assistant 最终文本，进入评估。

### 步骤判定 `_assess_step`

按顺序：

1. 超时 → `failed`（“步骤超时”）；
2. 用户停止 → `stopped`；
3. 有 pending 审批/提问 → **无人值守结算**：审批一律 `deny`，`ask_user` 自动回复“请基于现有信息自行决策”，
   然后判 `failed`（“步骤在等待用户输入，工作流无人值守”）；
4. 达到最大迭代次数 → `failed`；
5. 缺少 `outputs` 或默认 `result.md` → `failed`（“缺少输出产物：…”）；
6. 文本匹配 `状态: 受阻: …` → `failed`；匹配 `状态: 完成` → `done`；
7. 其余按 `done` 处理（记录 `declared_status`）。

## 五、停止与单步重跑

- **停止**：`stop_workflow` → `request_stop()` 置停止事件并停止当前步骤 runner 与监督者；
  状态落成 `stopped`，事件 `run_finished`。
- **单步重跑**：`run_step`（面板/结果会话）复用上次 `run_id` 的结果目录、运行参数与上游产物，
  只重跑指定步骤；**不重算下游步骤，也不重新生成总结**。要求先有一次完整运行。

## 六、触发面

| 场景 | 可用 action |
|---|---|
| 工作流面板会话（`metadata.workflow_panel`） | `list/get/run/run_step/stop/status/create/update/update_inputs/delete/runs/get_run/send_to_user` |
| 普通 master 会话 | `list/get/run/stop/status`（`run` 必须带 `workflow_id`） |
| 结果会话（监督者） | 动态注入 `workflow_agent(step)`；`workflow` 工具另可 `run_step/send_to_user` |

`run` 的 `inputs` 必须是 `{key: value}`；缺必填项返回 `{"error":"missing_inputs","missing":[...]}`，
提示用户补充后再重试，绝不自行编造输入。`create/update/update_inputs/delete` 仅面板可用。

## 七、HTTP 接口（`web/routes_workflows.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/workspaces/{ws}/workflows` | 列出工作流 |
| POST | `/api/workspaces/{ws}/workflows` | 创建（面板） |
| GET/PUT | `/api/workspaces/{ws}/workflows/{id}` | 读取/更新定义 |
| POST | `.../hidden` | 侧栏隐藏 |
| DELETE | `.../{id}` | 删除（运行中拒绝） |
| POST | `.../{id}/run` | `{inputs, origin_session_id}`，缺参返回 `missing_inputs` |
| POST | `.../{id}/stop` | 停止 |
| GET | `.../{id}/state` | 当前状态 |
| POST | `.../panel-sessions` | 新建或复用面板会话 |
| GET | `.../panel-sessions/by-session/{sid}` | 会话 → 工作流绑定 |
| POST | `.../{id}/results-session` | 确保/新建结果会话 |
| POST | `.../{id}/send-results` | 把本次运行产物发到指定会话（file card） |
| GET | `.../{id}/runs`、`.../runs/{run_id}`、`.../events` | 运行列表/详情/事件流 |

## 八、事件与前端

- 全局事件：`workflow_changed`（action：created/updated/deleted/run_started/step_started/run_finished/…）、
  `files_changed`；
- 运行事件（`events.jsonl` 同名）：`run_started`、`step_started`、`text`、`thinking`、`tool_use`、
  `tool_result`、`tool_output`、`step_finished`、`run_finished`；
- 前端侧栏「工作流」分区 + 面板视图（双标签：定义/运行、Agents 卡片），只读运行清单，不直接改定义。

## 九、守卫与约束

- **禁止直接写定义**：`core/workflows/guards.py` 拦截对 `{cwd}/.cili/workflows/` 的 write/edit，
  要求走 `workflow` 工具，避免绕过校验与面板绑定；
- 运行中拒绝 update/run/delete；删除需用户明确确认；
- 步骤 id 与 workspace 同算法；来源只能用前面的步骤；单步重跑不改变下游与总结；
- 结果目录、运行目录均由代码生成，不信任模型传入的路径。

## 十、测试

`test/test_workflow_model.py`、`test_workflow_store.py`、`test_workflow_runner.py`、
`test_workflow_role.py`、`test_workflow_tool.py`、`test_workflow_panel_layer.py`、
`test_web_workflows.py`。

## 十一、已知边界

- 步骤不共享会话，跨步骤只能靠文件；模型需在产物里写清结论；
- 单步重跑不会自动重算下游产物，也不重新生成总结；
- 运行历史仅保留最近 20 次；步骤超时固定 1 小时（常量，不对外开放参数）；
- 监督者与执行器都在本进程线程内运行，无跨进程调度。
