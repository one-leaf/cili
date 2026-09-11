# Agent 架构设计文档

本文档描述 Cili Agent 的核心 Agent 架构设计，包括 BaseAgent 基础设施、统一 Agent 类（`core/agent.py`）以及角色 JSON 配置驱动的行为分叉。

> **LLM API 接口细节**请参考 [LLM API 参考文档](llm-api-reference.md)
>
> **会话管理**请参考 [会话管理设计文档](session-management-design.md)
>
> **工具系统**请参考 [工具系统设计文档](tool-system-design.md)

---

## 目录

- [一、整体架构](#一整体架构)
- [二、BaseAgent 设计](#二baseagent-设计)
- [三、统一 Agent 与角色配置](#三统一-agent-与角色配置)
- [四、interactive 模式（master）](#四interactive-模式master)
- [五、autonomous 模式（worker/lite）](#五autonomous-模式workerlite)
- [六、委派会话消息结构](#六委派会话消息结构)
- [七、Thinking 处理](#七thinking-处理)
- [八、关键设计决策](#八关键设计决策)

---

## 一、整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        用户界面 (Web UI)                         │
│                    web/web_api.py + static/                      │
└──────────────────────────────┬──────────────────────────────────┘
                               │ SSE / REST API
┌──────────────────────────────▼──────────────────────────────────┐
│                         BaseAgent                               │
│                     core/base_agent.py                          │
│                                                                  │
│   统一基础设施:                                                   │
│   - self.messages (消息管理)                                     │
│   - 工具执行 (外部文件存储)                                       │
│   - 3 层自动压缩                                                 │
│   - LLM 调用 (流式/非流式)                                       │
│   - usage 追踪                                                   │
└──────────────────────────┬───────────────────────────────────────┘
                           │ 继承
┌──────────────────────────▼───────────────────────────────────────┐
│                       Agent（统一类）                            │
│                      core/agent.py                               │
│   run() 按 role_cfg.mode 分叉：                                   │
│   - interactive → _run_interactive（master，Web 聊天入口）         │
│   - autonomous → _run_autonomous（worker/lite，自主执行）         │
└──────────────────────────┬───────────────────────────────────────┘
                           │ load_agent_role() 加载
┌──────────────────────────▼───────────────────────────────────────┐
│                      角色 JSON 配置                               │
│              core/agents/master.json / worker.json / lite.json   │
│   字段：mode / tools 白名单 / skills / 行为开关 / max_iterations / │
│         system_prompt.blocks / user_layers                       │
│   prompt 拼装：core/prompt_builder.py（块生成器 + user 层注入）     │
│   模型：config.{role}_model or config.model                       │
└──────────────────────────────────────────────────────────────────┘
```

核心变化：**不再有 RootAgent / Agent 两个子类**。二者合并为一个统一 `Agent` 类，行为差异完全由角色 JSON（`mode` 字段）驱动：

- `mode="interactive"`（master）→ 原 RootAgent 行为：Web 聊天入口、会话持久化、流式回调、ask_user/审批、可恢复循环
- `mode="autonomous"`（worker/lite）→ 原子代理行为：pinned 任务消息 → 自主执行循环 → 可选检查阶段 → 兜底总结，`run()` 返回结构化 dict

---

## 二、BaseAgent 设计

### 2.1 职责

BaseAgent 是统一 Agent 类的基类，提供所有角色共用的执行基础设施：
- 消息管理（`self.messages` 列表）
- 工具执行（外部文件存储）
- 3 层自动压缩
- LLM 调用（流式/非流式）
- usage 追踪

子类（统一 `Agent`）通过角色配置定制：工具白名单、系统提示拼装、行为开关。

### 2.2 核心方法

```python
class BaseAgent:
    def __init__(
        self,
        config: Config,
        workspace_uuid: str = "",
        cwd: str = "",
        session_dir: Path | None = None,  # 持久化路径
        stop_check: Callable[[], bool] | None = None,
        max_iterations: int = 50,  # 由 Agent 传入角色配置的 max_iterations
    ):
        self.messages: list[dict] = []       # 消息列表（权威源）
        self.session_dir = session_dir       # 保存路径
        self.max_iterations = max_iterations

    # 消息管理
    def add_message(role, content, meta=None)     # 添加消息（meta 如 {"pinned": True}）
    def save_messages(metadata)              # 保存到 session_dir/index.json
    def load_messages() -> bool              # 从文件加载
    def get_valid_messages(strip_meta=True) -> list[dict]  # 过滤无效消息

    # 工具执行
    def _execute_tool(name, input, tool_use_id) -> dict
    def _resolve_tool_results(messages) -> list[dict]
    def _pad_dangling_tool_results()          # 为悬挂 tool_use 补占位 tool_result

    # 压缩
    def _check_and_compress()                # 3 层压缩

    # LLM 调用
    def _call_llm(streaming, system_prompt) -> LLMResponse
    # 失败语义：内部重试耗尽后抛 RuntimeError（format_llm_error 文本），
    # 由调用方捕获处理；用户停止返回 stop_reason="stopped"，不算错误

    # 生命周期
    def stop()                               # 设置 _stopped = True（可随时中断）
    def is_running() -> bool
    def close()                              # 关闭工具与 LLM 客户端
```

### 2.3 消息压缩

BaseAgent 在每次 LLM 调用前自动执行三层压缩，详见 [`docs/design/compression-design.md`](./compression-design.md)。`pinned` 标记的消息（任务、检查提示等核心锚点）在完整压缩中永不标记失效。

### 2.4 执行循环

```
用户输入 → 添加消息 → 自动压缩检查 → 调用 LLM → 解析工具调用
    ↑                                              ↓
    └──── 工具结果 ← 执行工具 ← 有工具调用？←──────┘
                                                    ↓ (无工具调用)
                                              返回最终响应
```

---

## 三、统一 Agent 与角色配置

### 3.1 职责与 mode 分叉

统一 `Agent` 类（`core/agent.py`）继承 BaseAgent，构造时加载角色配置，`role_cfg.mode` 决定运行分叉：

- `interactive`（master）→ `run(user_input, on_text=..., ...)` 返回 `None`，Web 聊天入口，保留 `resume_after_ask_user` / `switch_session` / `reload_config` / `compact` 等接口
- `autonomous`（worker/lite）→ `run()` 返回 dict（`status` / `summary` / `iterations` / `usage` 等），pinned 任务消息 → 循环 → 可选 check 阶段 → 兜底总结

```python
class Agent(BaseAgent):
    def __init__(
        self,
        config: Config,
        role: str = "master",       # 角色名（master/worker/lite）
        cwd: str | None = None,
        workspace_uuid: str = "",
        task: str = "",             # autonomous 模式的任务描述
        plan: list[str] | None = None,  # autonomous 模式的可选执行计划
        session_dir: Path | None = None,  # autonomous 模式的消息持久化目录
        stop_check: Callable[[], bool] | None = None,
        exec_id: str = "",          # autonomous 模式的执行 ID（作为 session 标识）
        temperature: float | None = None,  # 可选 LLM temperature 覆盖
        approval_store=None,        # autonomous 模式共享的会话级审批存储
        max_consecutive_failures: int | None = None,  # None 时取角色配置
    ):
        self.role = role
        self.role_cfg = load_agent_role(role, config)
        self.model = getattr(config, f"{role}_model", None) or config.model
        self._mode = self.role_cfg.mode

        if self._mode == "interactive":
            self._init_interactive(workspace_uuid)
        else:
            self._init_autonomous(task, plan, exec_id, session_dir, stop_check)

        super().__init__(
            config=config, workspace_uuid=workspace_uuid, cwd=self._cwd_init,
            session_dir=init_session_dir, stop_check=stop_check,
            max_iterations=self.role_cfg.max_iterations,
        )
        # ...
        self._streaming = self.role_cfg.streaming
        self._rebuild_tools()
        self._system_prompt = self._build_system_prompt()  # autonomous 用缓存
```

`run()` 按 mode 分派：

```python
def run(self, *args, **kwargs):
    """统一入口：按角色 mode 分派。

    interactive → run(user_input, on_text=..., ...)（Web 聊天入口）
    autonomous → run() → dict（pinned 任务 → 循环 → 检查 → 兜底总结）
    """
    if self._mode == "interactive":
        return self._run_interactive(*args, **kwargs)
    return self._run_autonomous(*args, **kwargs)
```

### 3.2 角色 JSON 配置

角色定义位于 `core/agents/{role}.json`（系统级、随代码库提交），由 `core/agent_config.py` 的 `load_agent_role(role, config)` 加载为 `AgentRoleConfig`。任何 JSON 缺失的字段使用 `_DEFAULTS` 兜底。

`AgentRoleConfig` 字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | str | 角色名（master/worker/lite） |
| `label` | str | 显示名 |
| `mode` | str | `interactive` 或 `autonomous`（仅这两值合法） |
| `tools` | list[str] | 工具白名单，`create_tools(role_cfg, ...)` 按此实例化 |
| `skills` | list[str] | 可见技能，`[]` 或 `["*"]`（全部可见） |
| `streaming` | bool | 是否流式输出（所有角色默认均流式） |
| `ask_user` | bool | 是否允许 ask_user 工具 |
| `approval` | bool | 是否启用高风险命令审批 |
| `session_persistence` | bool | 是否会话持久化 |
| `check_phase` | bool | autonomous 是否启用检查阶段 |
| `budget_notice` | bool | autonomous 是否注入迭代额度预警 |
| `progress_persistence` | bool | autonomous 是否实时保存进度 |
| `max_iterations` | int \| None | None → 取 `config.system.max_iterations`（默认 200） |
| `max_consecutive_failures` | int | 连续失败次数上限（默认 5） |
| `max_tokens` | int \| None | 角色级输出上限；None → 继承角色模型的 `max_tokens`；设置后取 `min(角色值, 模型上限)` |
| `system_prompt` | dict | `{"blocks": [...]}` 系统提示块声明 |
| `user_layers` | list[dict] | 需注入的 user 消息层声明 |

### 3.3 三个角色概览

| | master | worker | lite |
|---|--------|--------|------|
| **mode** | `interactive` | `autonomous` | `autonomous` |
| **工具白名单** | 23 个（15 core 常驻 + 8 deferred 延迟加载；含 agent、ask_user、tool_search） | 13 个（执行型：read/write/edit/bash/pwsh/grep/find/web_search/memory/python/read_tool_result/temp/skill） | 4 个（read/write/edit/bash） |
| **skills** | `["*"]` | `["*"]` | `[]` |
| **streaming** | ✓ | ✓ | ✓ |
| **ask_user** | ✓ | ✗ | ✗ |
| **approval** | ✓ | ✗ | ✗ |
| **session_persistence** | ✓ | ✗ | ✗ |
| **check_phase** | ✗ | ✓ | ✗ |
| **budget_notice** | ✗ | ✓ | ✗ |
| **progress_persistence** | ✗ | ✓ | ✓ |
| **max_iterations** | null → system（200） | null → system（200） | 20 |
| **max_tokens** | 16384 | 16384 | 8192 |
| **system_prompt.blocks** | text(role) + tools + skills | text(role) + tools + skills | text(role) + tools |
| **user_layers** | claude_md + context | task + context + runtime | task |

### 3.4 模型选择

- 角色 JSON **不含 model 字段**。`Agent` 构造时取 `getattr(config, f"{role}_model", None) or config.model`。
- `Config.model` 为 master 主模型；`worker_model` / `lite_model` 可选，未配置（或只填了部分字段）时通过 `ModelConfig.merged_with(override)` **继承 master 模型的其他字段**（例如只填 `name` 则其余字段全部沿用 master）。
- `reload_config()` 从磁盘重载配置、重建 LLM 客户端与工具集（先创建新客户端，成功后再关闭旧客户端，避免失败后 `self.client` 指向已关闭的实例）。

### 3.5 配置化 prompt（core/prompt_builder.py）

系统提示词不再由硬编码模板常量拼接，而是按角色 JSON 的 `system_prompt.blocks` 顺序拼装：

- `SYSTEM_BLOCK_GENERATORS`：块类型 → 生成器
  - `text`：返回固定文案（`block.content`，字符串或字符串数组按行拼装）
  - `tools`：从实际加载的工具实例生成工具列表段
  - `skills`：从角色可见技能生成技能列表段
- `build_system_prompt(agent)`：按 blocks 顺序拼接「启用且非空」的块。`Agent._build_system_prompt()` 委托给它。

注入型 user 层（`USER_LAYER_GENERATORS`）：

- `claude_md`：每次从磁盘重读项目指令（agent.md/CLAUDE.md），**不持久化**到消息历史
- `context`：动态环境上下文（日期/workspace/内存等），每次调用重新生成，**不持久化**
- `task` / `runtime`：由 `Agent` 按 `role_cfg` 布尔开关运行时写入历史（pinned 任务消息、预算/检查/超时提示），不在生成器表内

`Agent._get_messages_with_header()` 在 BaseAgent 版基础上：遍历 `role_cfg.user_layers`，把启用且存在的生成器产出追加为注入消息，再调用 `assemble_context(messages, inject)`：

- 注入消息恒排在最前；若历史第一条也是 user（如 pinned 任务消息），二者合并为一条
- 合并后**连续 user 消息自动合成一条**，保证角色交替（满足 OpenAI/Bedrock 约束）
- 返回新列表，不修改入参；合并保留第一条的 `_meta`（id/pinned 等）

---

## 四、interactive 模式（master）

### 4.1 职责

`mode="interactive"` 即 master 角色（原 RootAgent 行为），用于 Web 聊天交互：
- 流式输出（`streaming=True`，可通过 `run(streaming=...)` 关闭）
- SessionManager 会话持久化
- 最多 `max_iterations` 次迭代（取自角色配置，master 为 `config.system.max_iterations`）
- 回调支持（on_text, on_thinking, on_tool_call, on_tool_result, on_agent_start, on_agent_complete）

### 4.2 初始化与接口

```python
def _init_interactive(self, workspace_uuid: str) -> None:
    # 工作区 sessions 目录
    self.sessions_dir = get_workspace_data_dir(workspace_uuid) / "sessions"
    self.sessions_dir.mkdir(parents=True, exist_ok=True)
    # 会话管理器：加载已有会话或创建默认会话
    self.session_manager = SessionManager("", self.sessions_dir)
    ...
    # 共享消息列表（同一引用，非拷贝）
    self.messages = self.session_manager.messages
    self._usage = self.session_manager.get_usage()
    # 会话级高风险命令审批存储（内存，不持久化），根/子代理共享
    self.approval_store = ApprovalStore()

# 其他接口
def reload_config()          # 重载配置并重建 LLM 客户端/工具集
def resume_after_ask_user()  # ask_user 工具返回后恢复循环
def resume_loop()            # 外部占位完成后的恢复接口（保留兼容）
def switch_session()         # 切换会话
def reset()                  # 清空对话历史
def get_usage()              # 返回 usage 统计（与会话同步）
def compact()                # 手动压缩（_perform_full_compact(3)）
def cleanup()                # 清理资源并保存会话
```

### 4.3 核心循环

```python
def _run_interactive(
    self,
    user_input: str | list[dict],
    on_text: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_call: Callable[[str, dict, str], None] | None = None,
    on_tool_result: Callable[[str, str, bool, str], None] | None = None,
    on_agent_start: Callable[[str, str], None] | None = None,
    on_agent_complete: Callable[[str], None] | None = None,
    streaming: bool = True,
) -> None:
    """执行一轮 agent 循环"""
    self._stopped = False
    self._running = True
    self._on_text = on_text
    # ... 保存所有回调 ...

    try:
        # 保存上一轮
        self._sync_to_session_manager()
        self.session_manager.save()
        # 添加用户消息（项目指令不在此注入，而在 _get_messages_with_header()
        # 中每次 LLM 调用时从磁盘重读并动态注入，不持久化到消息历史）
        self.add_message("user", user_input)
        # 进入 agent 循环
        self._agent_loop()
    finally:
        self._running = False

def _agent_loop(self) -> None:
    """共享循环体（run/resume_after_ask_user/resume_loop 均调用此方法）"""
    iteration = 0
    while iteration < self.max_iterations:
        if self._stopped:
            break
        iteration += 1
        # 自动压缩检查
        self._check_and_compress()
        # 调用 LLM（每轮重建 system prompt，注入型 user 层动态生成）
        system_prompt = self._build_system_prompt()
        response = self._call_llm(streaming=self._streaming, system_prompt=system_prompt)
        # 添加 assistant 响应
        self.add_message("assistant", response.content_as_dicts())
        # 解析工具调用
        tool_call_blocks = response.get_tool_calls()
        if not tool_call_blocks:
            break  # 没有工具调用，结束本轮
        # 执行工具，每个工具执行后都同步到 session
        wait_for_external = False
        for block in tool_call_blocks:
            input_data = block.parse_arguments()
            result = self._execute_tool(block.name, input_data, block.id)
            # 检查是否需要暂停等待外部输入（ask_user/agent 占位）
            if result.get("_meta", {}).get("completed") is False:
                wait_for_external = True
            self.add_message("user", [result])
            self._sync_to_session_manager()
            self.session_manager.save()
        if wait_for_external:
            break  # 退出循环等待用户输入或 agent 完成
```

### 4.4 审批（approval）

master 启用会话级高风险命令审批（`ApprovalStore`，内存存储，根/子代理共享）：

- 工具执行返回 `_meta[META_KEY]` 表示「需用户批准」时，该结果被降级为错误提示
- 批处理完所有工具结果后，`_handle_approval_required(approval)` 合成一条 `ask_user` 卡片询问用户是否批准，随后暂停循环等待回答（保证消息配对正确：`[assistant tool_use...] → [tool_result...] → [assistant ask_user tool_use] → [user ask_user 占位]`）
- 批准后本会话内执行相同命令（含委派给子代理）不再询问；同批多个需批准命令只询问第一条，其余保持拒绝

---

## 五、autonomous 模式（worker/lite）

### 5.1 职责

`mode="autonomous"` 即 worker/lite 角色（原子代理行为），用于 agent 工具委派：
- `run()` 返回结构化 dict：`{"status", "summary", "iterations", "usage", ...}`
- 隔离执行环境，避免污染主会话上下文
- 支持并行处理多个独立任务（后台线程）

### 5.2 执行流程：目标→计划→执行→检查

```python
def _run_autonomous(self) -> dict[str, Any]:
    """Execute autonomous loop with check phase, return structured result.

    Flow: 目标→计划→执行→检查
    """
    # 构建 pinned 任务消息（任务 + 计划，免疫压缩）
    self.add_message("user", self._build_task_message(), meta={"pinned": True})

    # 主循环 → 无工具调用时：
    #   - 未进入检查阶段 → 注入 _CHECK_PROMPT（pinned）进入检查阶段
    #   - 已在检查阶段   → 任务真正完成，返回 {"status": "completed", ...}
    for i in range(self.max_iterations):
        ...
```

各阶段要点：

1. **目标+计划**：`_build_task_message()` 作为第一条 user 消息注入，带 `_meta.pinned=True` 标记，压缩时始终保留。内容含 Objective、可选的 Execution Plan、迭代额度说明，以及下放的「主代理会话已批准命令」（`build_approved_commands_section`）。
2. **执行**：主循环，LLM 自主调用工具完成任务。全部角色流式输出（`streaming=True`），可通过 `stop()` 随时中断（`_stopped`）。
3. **检查**（仅 worker，`check_phase=True`）：主阶段结束后注入 `_CHECK_PROMPT`（pinned），要求 LLM 重新阅读任务目标与执行计划、逐项检查执行结果、发现遗漏立即修复、全部确认后输出总结。检查阶段最多允许 `max(main_iterations, _MIN_CHECK_ITERATIONS=10)` 次额外迭代。
4. **兜底总结**：迭代额度耗尽（timeout）时，`_wrapup_timeout_summary()` 直接调用 `client.chat`（**不传 tools**）生成一次执行总结，杜绝兜底调用再次触发工具循环。

### 5.3 返回状态

`run()` 返回 dict，`status` 取值为：

| status | 含义 |
|--------|------|
| `completed` | 正常完成（含检查阶段确认完成） |
| `stopped` | 用户/父代理主动停止 |
| `error` | LLM 调用失败（内部重试耗尽） |
| `failed` | 连续失败次数达到 `max_consecutive_failures` |
| `timeout` | 迭代额度耗尽（`wrapped_up=True` 表示兜底总结成功） |

返回字段：`status` / `summary`（或 `message`）/ `iterations` / `usage`，检查完成时含 `check_iterations`，预算兜底下含 `budget_wrapup=True`。

### 5.4 行为开关

- **budget_notice**（worker）：`_inject_budget_notice(i)` 在迭代进行到 `max_iterations * 0.8`（额度预警）与 `* 0.95`（额度即将耗尽）时各注入一次 user 预警消息。用 **list content** 注入（而非 string），避免 `_find_split_by_user_messages` 的 string 计数被推过阈值、导致 full compact 挤掉 pinned 任务消息。预算兜底（final 已触发）时跳过检查阶段，直接交付总结。
- **progress_persistence**（worker/lite）：`_save_progress()` 每轮实时写 `{exec_dir}/index.json`，保证文件始终有 `exec_id` 和 `task`（`SessionManager.load_agent_log()` 可直接读取，前端可实时查看进度）。
- **无 ask_user/审批**：worker/lite 无 ask_user 工具，`_downgrade_approval_result()` 把「需用户批准」的结果降级为普通错误（提示换用非拦截命令），不挂起不询问。主代理批准过的命令通过共享 `ApprovalStore` 直接下放执行。

### 5.5 worker 与 lite 的差异

| 维度 | worker | lite |
|------|--------|------|
| 工具集 | 13 个（read/write/edit/bash/pwsh/grep/find/web_search/memory/python/read_tool_result/temp/skill） | 只读 read/write/edit/bash 四工具 |
| 检查阶段 | ✓（`check_phase=True`） | ✗ |
| 预算预警 | ✓（`budget_notice=True`） | ✗ |
| 迭代上限 | null → system（默认 200） | 20 |
| 适用场景 | 复杂/耗时任务 | 简单文件读写改造 |

---

## 六、委派会话消息结构

主会话通过 tool_use + tool_result 消息对存储 `agent` 调用，子代理自己的完整执行日志存储在独立目录。

`agent` 工具（`core/tools/agent_tool.py`）委派逻辑：

- 生成 `exec_id`（`exec_{8位hex}`，`SessionManager._generate_exec_id()`）
- 创建 `{主会话 session_dir}/{exec_id}/` 目录
- 构造统一 Agent：`Agent(config, role=agent_type, task, plan, workspace_uuid, cwd, stop_check, session_dir=exec_dir, exec_id=exec_id, temperature, approval_store=共享)`，`agent_type` 为 `worker`（默认）或 `lite`
- **同步模式**（默认）：后台线程运行 `agent.run()` 并阻塞等待结果，主循环无需外部恢复；`run_in_background=True` 则立即返回 `task_id`，用 `read_task`/`kill_task`/`list_tasks` 管理
- 子代理 usage 转发到主会话；完成后保存执行日志并触发 `on_agent_start`/`on_agent_complete` 回调（推送 SSE 事件）

```
主会话 index.json:
  messages: [
    {role: "user", content: "处理这个大文件"},
    {role: "assistant", content: [{type: "tool_use", id: "toolu_001", name: "agent", input: {task: "处理文件...", agent_type: "worker"}}]},
    {role: "user", content: [{type: "tool_result", tool_use_id: "toolu_001",
     _meta: {exec_id: "exec_abc123", completed: true, iterations: 15, message_count: 42}}]},
    {role: "assistant", content: "处理完成！"}
  ]

子代理 {session_dir}/exec_{id}/index.json:
  {
    "exec_id": "exec_abc123",
    "session_id": "exec_abc123",
    "task": "处理文件...",
    "messages": [...完整执行历史...],
    "summary": "处理完成，共处理 42 个文件",
    "metadata": {
      "parent_session_id": "",
      "session_id": "exec_abc123",
      "started_at": "2026-08-25 10:00:00",
      "ended_at": "2026-08-25 10:05:00",
      "duration_seconds": 300,
      "status": "completed",
      "iterations": 15,
      "max_iterations": 200,
      "message_count": 42,
      "summary": "处理完成，共处理 42 个文件"
    }
  }
```

`metadata` 还可能在执行期间包含 `current_tool`（`_save_progress` 写入），供前端显示当前正在执行的工具。

---

## 七、Thinking 处理

### 7.1 设计原则

Cili 对 LLM 返回的 thinking 内容**不做过滤**，直接作为回复的一部分：

- **流式模式**：thinking 内容通过 `on_thinking` 回调实时推送到前端
- **消息存储**：thinking blocks 保留在消息历史中，Anthropic API 要求后续请求包含之前的 thinking blocks
- **启用条件**：配置了 `reasoning_effort` 时启用 extended thinking（流式/非流式均生效；`budget_tokens` 映射：low→1024, medium→4096, high→10000）

### 7.2 LLM 配置

**Anthropic API**（`budget_tokens` 由 `reasoning_effort` 配置决定，未配置时不启用 thinking）：
```json
{
  "thinking": {
    "type": "enabled",
    "budget_tokens": 4096  // low=1024, medium=4096, high=10000
  }
}
```

**OpenAI API**（推理模型，`reasoning_effort` 来自配置；未配置时不传该参数，使用 API 默认值）：
```json
{
  "reasoning_effort": "medium"
}
```

> **注意**：流式与非流式均支持 Anthropic 的 extended thinking；流式模式下 thinking 增量通过 SSE `thinking_delta` 事件翻译为 `reasoning_delta`，经 `on_thinking` 回调实时推送。

---

## 八、关键设计决策

### 8.1 为什么用「统一 Agent + 角色 JSON」取代三层类？

- 原 RootAgent/Agent 共享 90% 的执行基础设施（BaseAgent），差异仅在于模式开关；硬编码成两个类导致重复与分支蔓延
- 行为差异收敛到一份 JSON：工具白名单、行为开关、prompt、迭代上限一目了然，新增角色只需加一个 JSON 文件
- `mode` 分叉（interactive/autonomous）是唯一的关键分支点，其余行为全部由开关控制

### 8.2 为什么任务信息放在首条 user 消息（pinned）？

- 任务/计划作为第一条 user 消息注入，带 `_meta.pinned=True` 标记
- pinned 消息不会被上下文压缩影响
- 即使对话历史被压缩，任务信息仍然可见，子代理始终清楚自己的任务目标
- 系统提示按角色 JSON blocks 拼装，与任务内容解耦

### 8.3 为什么模型按角色配置？

- `config.{role}_model or config.model`：worker/lite 可在 `setting.json` 单独配置模型，未配置继承 master 模型
- `ModelConfig.merged_with(override)`：只填 `name` 时其余字段（接口类型、温度、上下文窗口等）自动继承 master，避免重复配置
- 支持并行执行多个不同模型的子代理

### 8.4 为什么 Agent 持有消息而不是 SessionManager？

- Agent 自己管理消息生命周期，保存时机由 Agent 控制
- BaseAgent 层仅依赖 `session_dir` 持久化；interactive 模式中 SessionManager 只负责会话元数据与磁盘保存（messages 共享同一引用）
- autonomous 模式可以传入 `session_dir` 直接持久化（`exec_{id}/index.json`），无需 SessionManager

### 8.5 为什么注入型 user 层不持久化 + 防连续合并？

- `claude_md`/`context` 层每次从磁盘/环境动态生成，不污染 `self.messages`，会话文件保持干净
- 注入层与历史中的 pinned 任务消息都是 user 角色，`assemble_context()` 自动合并连续 user 消息，保证角色交替（满足 OpenAI/Bedrock 约束）

### 8.6 为什么 worker/lite 无 ask_user/审批？

- 子代理自主执行，不应在运行中停下来向用户提问
- 需要批准的命令被降级为普通错误（提示换用非拦截命令或告知主代理）
- 主代理会话批准过的命令通过共享 `ApprovalStore` 下放，子代理可直接执行，无需重复审批

---

*文档版本: v3.0*
*最后更新: 2026-09-11（worker 精简为 13 个执行型工具；master 增加 tool_search，8 个低频工具延迟加载；三角色新增角色级 max_tokens）*
