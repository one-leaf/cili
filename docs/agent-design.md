# Agent 架构设计文档

本文档描述 Cili Agent 的核心 Agent 架构设计，包括 BaseAgent、RootAgent、SubAgent 的职责和实现。

> **LLM API 接口细节**请参考 [LLM API 参考文档](llm-api-reference.md)
>
> **会话管理**请参考 [会话管理设计文档](session-management-design.md)
>
> **工具系统**请参考 [工具系统设计文档](tool-system-design.md)

---

## 目录

- [一、整体架构](#一整体架构)
- [二、BaseAgent 设计](#二baseagent-设计)
- [三、RootAgent 设计](#三rootagent-设计)
- [四、SubAgent 设计](#四subagent-设计)
- [五、Thinking 处理](#五thinking-处理)
- [六、关键设计决策](#六关键设计决策)

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
        ┌──────────────────┼──────────────────┐
        │                  │                  │
┌───────▼────────┐  ┌──────▼────────┐  ┌──────▼────────┐
│   RootAgent    │  │   SubAgent    │  │   SubAgent    │
│  (用户交互)     │  │  (委派任务)    │  │  (cron 任务)   │
│  流式输出       │  │  非流式输出    │  │  独立持久化    │
└────────────────┘  └───────────────┘  └───────────────┘
```

---

## 二、BaseAgent 设计

### 2.1 职责

BaseAgent 是 RootAgent 和 SubAgent 的基类，提供统一的执行基础设施：
- 消息管理（`self.messages` 列表）
- 工具执行（外部文件存储）
- 3 层自动压缩
- LLM 调用（流式/非流式）
- usage 追踪

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
        max_iterations: int = 50,  # RootAgent overrides to 200
    ):
        self.messages: list[dict] = []       # 消息列表（权威源）
        self.session_dir = session_dir       # 保存路径
        self.max_iterations = max_iterations

    # 消息管理
    def add_message(role, content)           # 添加消息
    def save_messages(metadata)              # 保存到 session_dir/index.json
    def load_messages() -> bool              # 从文件加载
    def get_valid_messages() -> list[dict]   # 过滤无效消息

    # 工具执行
    def _execute_tool(name, input, tool_use_id) -> dict
    def _resolve_tool_results(messages) -> list[dict]

    # 压缩
    def _check_and_compress()                # 3 层压缩

    # LLM 调用
    def _call_llm(streaming, system_prompt) -> LLMResponse
```

### 2.3 消息压缩

BaseAgent 在每次 LLM 调用前自动执行三层压缩，详见 [`docs/compression-design.md`](./compression-design.md)。

### 2.4 执行循环

```
用户输入 → 添加消息 → 自动压缩检查 → 调用 LLM → 解析工具调用
    ↑                                              ↓
    └──── 工具结果 ← 执行工具 ← 有工具调用？←──────┘
                                                    ↓ (无工具调用)
                                              返回最终响应
```

---

## 三、RootAgent 设计

### 3.1 职责

RootAgent 继承 BaseAgent，用于用户交互：
- 流式输出（`streaming=True`）
- SessionManager 持久化
- 最多 200 次迭代
- 回调支持（on_text, on_thinking 等）

### 3.2 初始化参数

```python
class RootAgent(BaseAgent):
    def __init__(self, config: Config, cwd: str | None = None, workspace_uuid: str = ""):
        # 工作区路径
        self._cwd_init = os.path.abspath(cwd or os.getcwd())

        # Setup sessions directory
        self.sessions_dir = get_workspace_data_dir(workspace_uuid) / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        # 会话管理器：加载已有会话或创建默认会话
        self.session_manager = SessionManager("", self.sessions_dir)
        self.current_session_id: str = ""
        sessions = SessionManager.list_sessions(self.sessions_dir)
        if sessions:
            latest = max(sessions, key=lambda s: s.get("updated_at", ""))
            sid = latest["session_id"]
            loaded = SessionManager.load_session(sid, self.sessions_dir)
            if loaded:
                self.session_manager = loaded
                self.current_session_id = sid
            else:
                self._create_default_session()
        else:
            self._create_default_session()

        # Initialize base agent (max_iterations=200)
        session_dir = self.sessions_dir / self.current_session_id
        super().__init__(config=config, workspace_uuid=workspace_uuid,
                         cwd=self._cwd_init, session_dir=session_dir,
                         max_iterations=200)

        # 共享消息列表（同一引用，非拷贝）
        self.messages = self.session_manager.messages
        self._usage = self.session_manager.get_usage()

        # LLM 客户端
        self.client: LLMClient = create_llm_client(config.model)

        # 工具实例（shared + root = 21~22 个）
        self._rebuild_tools()

        # 回调钩子（6 个）
        self._on_text: Callable[[str], None] | None = None
        self._on_thinking: Callable[[str], None] | None = None
        self._on_tool_call: Callable[[str, dict, str], None] | None = None
        self._on_tool_result: Callable[[str, str, bool, str], None] | None = None
        self._on_subagent_start: Callable[[str, str], None] | None = None
        self._on_subagent_complete: Callable[[str], None] | None = None

        # 停止标志
        self._stopped = False

    # 其他方法
    def reload_config()          # 重载配置并重建 LLM 客户端
    def resume_after_ask_user()  # ask_user 工具返回后恢复循环
    def resume_loop()            # subagent 完成后恢复循环
    def switch_session()         # 切换会话
    def reset()                  # 清空对话历史
    def compact()                # 手动压缩
    def cleanup()                # 清理资源
```

### 3.3 核心循环

```python
def run(
    self,
    user_input: str | list[dict],
    on_text: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_call: Callable[[str, dict, str], None] | None = None,
    on_tool_result: Callable[[str, str, bool, str], None] | None = None,
    on_subagent_start: Callable[[str, str], None] | None = None,
    on_subagent_complete: Callable[[str], None] | None = None,
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

        # 注入项目指令（CLAUDE.md 等，幂等）
        self._inject_project_instructions()

        # 添加用户消息
        self.add_message("user", user_input)

        # 进入 agent 循环
        self._agent_loop()
    finally:
        self._running = False

def _agent_loop(self) -> None:
    """共享循环体（run/resume_after_ask_user/resume_loop 均调用此方法）"""
    self._sync_to_session_manager()
    self.session_manager.save()

    iteration = 0
    while iteration < self.max_iterations:
        if self._stopped:
            break
        iteration += 1

        # 自动压缩检查
        self._check_and_compress()

        # 调用 LLM（流式）
        system_prompt = build_root_prompt(self.workspace_uuid, self.cwd)
        response = self._call_llm(streaming=True, system_prompt=system_prompt)

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
            # 检查是否需要暂停等待外部输入（ask_user/subagent）
            if result.get("_meta", {}).get("completed") is False:
                wait_for_external = True
            self.add_message("user", [result])
            self._sync_to_session_manager()
            self.session_manager.save()

        if wait_for_external:
            break  # 退出循环等待用户输入或 subagent 完成
```

---

## 四、SubAgent 设计

### 4.1 职责

SubAgent 是独立执行的子代理，用于：
- 处理复杂/耗时任务（如大文件处理）
- 隔离执行环境，避免污染主会话上下文
- 并行处理多个独立任务

### 4.2 与 RootAgent 的区别

| 特性 | RootAgent | SubAgent |
|------|-----------|----------|
| 会话管理 | 管理用户会话，支持多轮对话 | 独立消息历史，不持久化到用户会话 |
| 工具集 | 21~22 个工具（shared + root） | 19~20 个工具（shared + sub） |
| 系统提示 | `build_root_prompt()` | `build_sub_prompt()` + 任务信息 |
| 用户配置 | 加载用户 profile | 不加载用户 profile（轻量） |
| 嵌套 | 可调用 SubAgent | 禁止嵌套调用 |
| 持久化 | 主会话 `index.json` | 独立目录 `session_dir/index.json` |
| 流式 | 流式输出 | 非流式输出 |

### 4.3 SubAgent 指令设计原则

SubAgent 的指令上下文遵循以下原则：

| 原则 | 说明 |
|------|------|
| **默认不信任继承** | 不假设 SubAgent 会自动知道项目规则（如 CLAUDE.md 内容），每次任务需要显式传递必要信息 |
| **显式优于隐式** | 通过任务描述（task）和执行计划（plan）为 SubAgent 提供明确、清晰的指令 |
| **指令原子化** | 每个 SubAgent 的指令集应该是自包含的，只包含完成其特定任务所需的信息 |

**为什么不继承项目指令？**

- SubAgent 是任务执行者，不需要了解项目全貌
- 项目级指令（CLAUDE.md）可能包含与当前任务无关的内容，造成上下文污染
- 减少不必要的 token 消耗，提高执行专注度

**任务委派时的最佳实践**：

- 任务描述应包含完成该任务所需的所有上下文
- 如果任务有特定约束（如"使用 UTF-8 编码"），应在 task 中明确说明
- 不要依赖 SubAgent "应该知道"的隐含假设

### 4.4 初始化参数

```python
class SubAgent(BaseAgent):
    def __init__(
        self,
        task: str,
        plan: list[str] | None = None,
        workspace_uuid: str = "",
        cwd: str = "",
        max_consecutive_failures: int = 5,
        session_dir: Path | None = None,  # 外部传入保存路径
        stop_check: Callable[[], bool] | None = None,
        exec_id: str = "",
    ):
        self.task = task                          # 任务目标
        self.plan = plan                          # 执行计划
        self._exec_id = exec_id

        # 加载配置
        config = load_config()

        # Initialize base agent (max_iterations=200)
        super().__init__(config=config, workspace_uuid=workspace_uuid,
                         cwd=cwd or os.getcwd(), session_dir=session_dir,
                         stop_check=stop_check, max_iterations=200)

        # session 引用，供工具获取 session_id
        self._session_ref = _SessionIdRef(exec_id)

        # 独立工具集（pass session_ref 供 temp 等工具使用）
        self.tools = create_sub_tools(cwd=self.cwd, workspace_uuid=self.workspace_uuid,
                                       session_manager=self._session_ref, config=config)
        self.tool_schemas = [t.to_schema() for t in self.tools]

        # 系统提示 = 基础提示（任务/计划放在首条 user 消息中）
        self._system_prompt = build_sub_prompt(self.workspace_uuid, self.cwd)

        # 独立 LLM 客户端
        self.client = create_llm_client(config.model)
```

### 4.5 四阶段执行流程：目标→计划→执行→检查

SubAgent 采用四阶段执行流程：

1. **目标+计划**：作为第一条 user 消息注入，带 `_meta.pinned=True` 标记，压缩时始终保留
2. **执行**：主循环，LLM 自主调用工具完成任务
3. **检查**：主循环结束后自动注入检查提示，LLM 验证结果、修复问题、确认完成

```python
# run() 中的核心逻辑
if not tool_calls:
    if not in_check_phase:
        # 主阶段结束 → 注入检查提示
        self.add_message("user", _CHECK_PROMPT, meta={"pinned": True})
        in_check_phase = True
        continue
    else:
        # 检查阶段结束 → 任务真正完成
        return {"status": "completed", "summary": summary, ...}
```

检查提示要求 LLM：
- 重新阅读任务目标和执行计划
- 逐项检查执行结果是否达标
- 发现遗漏或错误立即修复
- 全部确认无误后输出总结报告

检查阶段最多允许 `_MAX_CHECK_ITERATIONS=3` 次额外迭代。

### 4.6 会话消息结构

主会话通过 tool_use + tool_result 消息对存储 SubAgent 调用，SubAgent 自己的完整执行日志存储在独立目录：

```
主会话 index.json:
  messages: [
    {role: "user", content: "处理这个大文件"},
    {role: "assistant", content: [{type: "tool_use", id: "toolu_001", name: "subagent", input: {task: "处理文件..."}}]},
    {role: "user", content: [{type: "tool_result", tool_use_id: "toolu_001",
     _meta: {exec_id: "abc123", completed: true}}]},
    {role: "assistant", content: "处理完成！"}
  ]

SubAgent session_dir/index.json:
  {
    "exec_id": "abc123",
    "session_id": "abc123",
    "task": "处理文件...",
    "messages": [...完整执行历史...],
    "summary": "处理完成，共处理 42 个文件",
    "metadata": {
      "parent_session_id": "",
      "session_id": "abc123",
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

---

## 五、Thinking 处理

### 5.1 设计原则

Cili 对 LLM 返回的 thinking 内容**不做过滤**，直接作为回复的一部分：

- **流式模式**：thinking 内容通过 `on_thinking` 回调实时推送到前端
- **消息存储**：thinking blocks 保留在消息历史中，Anthropic API 要求后续请求包含之前的 thinking blocks
- **默认级别**：非流式请求默认启用 extended thinking（`budget_tokens` 根据 `reasoning_effort` 配置动态设置：low→1024, medium→4096, high→10000）

### 5.2 LLM 配置

**Anthropic API**（非流式，`budget_tokens` 由 `reasoning_effort` 配置决定）：
```json
{
  "thinking": {
    "type": "enabled",
    "budget_tokens": 4096  // low=1024, medium=4096, high=10000
  }
}
```

**OpenAI API**（推理模型，`reasoning_effort` 来自配置；未配置时自动检测 7 种推理模型前缀：o1, o3, o4, o5, qwen3, qwq, deepseek-r1，默认 "medium"）：
```json
{
  "reasoning_effort": "medium"
}
```

> **注意**：流式模式不支持 Anthropic 的 extended thinking（API 限制），但仍透传模型输出的 think 标签内容。

---

## 六、关键设计决策

### 6.1 为什么 SubAgent 禁止嵌套？

- 避免无限递归和复杂调试
- 每个 SubAgent 已有完整工具集，可处理大多数任务
- 简化执行日志和进度追踪

### 6.2 为什么任务信息放在系统提示末尾？

- 系统提示末尾不会被上下文压缩影响
- 确保 SubAgent 始终清楚自己的任务目标
- 即使对话历史被压缩，任务信息仍然可见

### 6.3 为什么使用独立 LLM 客户端？

- RootAgent 和 SubAgent 可以使用不同的模型配置
- 避免会话状态交叉污染
- 支持并行执行多个 SubAgent

### 6.4 为什么 Agent 持有消息而不是 SessionManager？

- Agent 自己管理消息生命周期，保存时机由 Agent 控制
- SessionManager 变成纯工具类（无状态），简化设计
- cron 任务可以传入 session_dir 直接持久化，无需 SessionManager

---

*文档版本: v2.0*
*最后更新: 2026-08-28（重构为 Agent 专注设计）*
