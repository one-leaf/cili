# Tool 工具系统设计文档

本文档描述 Cili Agent 的工具系统架构、统一注册表、角色白名单、工具基类和执行流程。

---

## 一、功能概述

工具（Tools）是 Agent 与外部世界交互的能力。每个工具封装一种操作（读文件、执行命令、搜索等），由 LLM 通过 tool_use 调用。

**核心特性**：
- **统一注册表 + 角色白名单**：所有工具在 `core/tools/` 平铺，由 `TOOL_REGISTRY` 统一注册，按角色 JSON 的 `tools` 白名单实例化（取代旧 shared/root/sub 三层目录 + 硬编码工厂）
- **统一基类**：所有工具继承 `Tool` 基类，共享路径解析、命令执行等方法
- **JSON Schema 参数**：工具参数使用标准 JSON Schema 描述
- **ToolResult 返回**：统一的结果数据结构
- **工具注册表**：按名称查找工具实例（O(1) 缓存查找）

---

## 二、架构设计

### 2.1 工具物理布局（平铺）

所有工具文件直接位于 `core/tools/`，不再有 `shared/` / `root/` / `sub/` 子目录：

```
core/tools/
├── __init__.py              # 重新导出 create_tools / TOOL_REGISTRY，提供 get_tool_by_name()
├── registry.py              # TOOL_REGISTRY 统一注册表 + create_tools() 工厂
├── base.py                  # Tool 基类 + ToolResult + BackgroundTaskManager
├── approval.py              # 会话级审批（ApprovalStore、ask/deny 常量、decision_id、文案）
├── read.py                  # 读取文件
├── write.py                 # 写入文件
├── edit.py                  # 精确替换
├── bash.py                  # Shell 命令（Git Bash，会话级审批）
├── pwsh.py                  # PowerShell 命令（会话级审批）
├── grep.py                  # 正则搜索
├── find.py                  # 文件查找
├── browser.py               # 浏览器自动化
├── web_search.py            # 网络搜索
├── memory.py                # 长期记忆
├── python_tool.py           # Python 执行
├── todo.py                  # 任务规划（TodoWriteTool）
├── latex.py                 # LaTeX 编译（tectonic/pdflatex/xelatex/lualatex）
├── message_bus_tool.py      # 跨会话消息传递
├── cron_tool.py             # 用户级定时任务管理
├── read_tool_result.py      # 检索压缩的工具结果
├── temp.py                  # 临时文件/目录管理
├── loop.py                  # 循环任务进度追踪（配合 cron 使用）
├── pdf2markdown.py          # PDF 转 Markdown（MinerU API）
├── skill.py                 # 技能工具（SkillTool，按角色 frontmatter roles 过滤）
├── agent_tool.py         # 子代理委派（AgentTool）
└── ask_user.py              # 用户交互（AskUserTool）
```

### 2.2 统一注册表 TOOL_REGISTRY

`core/tools/registry.py` 定义 `TOOL_REGISTRY: dict[str, Factory]`，把工具名映射到工厂函数：

```python
Factory = Callable[[AgentRoleConfig, str, str, Any, Config | None, Any], Tool]
# 参数依次为：(role_cfg, cwd, workspace_uuid, session_manager, config, approval_store)

TOOL_REGISTRY = {
    "read": _factory(ReadTool),
    "write": _factory(WriteTool),
    "edit": _factory(EditTool),
    "bash": _factory(BashTool, needs_approval=True),
    "pwsh": _factory(PwshTool, needs_approval=True),
    "grep": _factory(GrepTool),
    "find": _factory(FindTool),
    "browser": _factory(BrowserTool),
    "web_search": _factory(WebSearchTool),
    "memory": _factory(MemoryTool),
    "python": _factory(PythonTool, needs_config=True),
    "todo": _factory(TodoWriteTool),
    "latex": _factory(LatexTool),
    "message_bus": _factory(MessageBusTool),
    "cron": _factory(CronTool),
    "read_tool_result": _factory(ReadToolResultTool),
    "temp": _factory(TempTool),
    "loop": _factory(LoopTool),
    "pdf2markdown": _factory(PDF2MarkdownTool, needs_config=True),
    "skill": _make_skill,
    "agent": _factory(AgentTool, needs_config=True, needs_approval=True),
    "ask_user": _factory(AskUserTool),
}
```

`_factory(cls, *, needs_config, needs_approval)` 是通用工厂包装器，统一注入工具公共参数（cwd / workspace_uuid / session_manager），并按需附加**特殊参数**：

| 工具 | 特殊参数 | 用途 |
|------|---------|------|
| bash / pwsh | `approval_store` | 会话级高风险命令审批（拦截→询问→批准） |
| python / pdf2markdown | `config` | 读取全局配置（API 密钥、模型等） |
| skill | `role=role_cfg.name` | 角色名，决定可见技能集合（frontmatter roles 过滤） |
| agent | `config` + `approval_store` | config 用于构造子 Agent；approval_store 透传给子代理共享 |

### 2.3 create_tools 实例化流程

```python
def create_tools(
    role_cfg: AgentRoleConfig | None = None,
    cwd: str = ".",
    workspace_uuid: str = "",
    session_manager=None,
    config: Config | None = None,
    approval_store=None,
    role: str | None = None,
) -> list[Tool]:
```

1. `role_cfg` 缺省时回退到 `load_agent_role(role or "master", config)`，便于旧调用点（conftest / prompts）不显式传角色配置即可获得 master 全量工具
2. 遍历 `role_cfg.tools` 白名单（保持 JSON 中声明顺序），逐个查 `TOOL_REGISTRY`
3. 未注册的工具跳过并告警；实例化失败的单个工具捕获异常并告警，不中断整体流程
4. 返回按白名单顺序排列的工具列表

### 2.4 角色工具白名单

每个角色的可用工具集由其 `core/agents/{role}.json` 中的 `tools` 数组**声明式**决定：

| Agent 角色 | 模式 | 工具数量 | 工具清单 |
|-----------|------|---------|---------|
| master | interactive | 22 | read, write, edit, bash, pwsh, grep, find, browser, web_search, memory, python, todo, latex, message_bus, cron, read_tool_result, temp, loop, pdf2markdown, skill, agent, ask_user |
| worker | autonomous | 17 | master 去掉 todo、cron、message_bus、latex、ask_user |
| lite | autonomous | 4 | read, write, edit, bash |

**设计要点**：
- **ask_user 仅 master**：master 是交互式（interactive），可向用户提问；worker/lite 是自主后台模式（autonomous），不含交互工具
- **agent 委派（master/worker）**：master、worker 都含 `agent` 委派工具，但委派深度仅 1 层——只有 master(0) 可委派 worker/lite(1)，depth≥1 的子代理再委派会直接报错；lite 是纯执行角色，不含 agent 工具
- **todo/cron/message_bus/latex 仅 master**：任务规划、定时任务、跨会话消息、LaTeX 渲染属于主代理的编排职责，worker 不持有
- **lite 精简集**：只保留 read/write/edit/bash，无 skill、无 python 等，适合快速文件处理类子任务
- 修改某角色工具集只需编辑对应 JSON，无需改动注册表代码

### 2.5 工具查找

`core/tools/__init__.py` 提供按名称查找：

```python
# 创建 master（默认角色）的完整工具集
tools = create_tools(cwd="/workspace", workspace_uuid="abc123", session_manager=sm, config=cfg)

# 按名称查找工具（O(1) 缓存查找）
tool = get_tool_by_name(tools, "bash")
```

`get_tool_by_name` 通过 `_tool_map()` 构建 `{tool.name: tool}` 映射，按 `id(tools)` 缓存（列表引用在 agent 生命周期内稳定）。

---

## 三、Tool 基类

### 3.1 Tool 类定义

```python
class Tool:
    name: str = ""                    # 工具名称
    description: str = ""             # 工具描述（给 LLM 看）
    parameters: dict[str, Any] = {}   # JSON Schema 参数定义

    # 文件搜索时跳过的目录
    IGNORE_DIRS: set[str] = {".git", "node_modules", "__pycache__", ...}

    # 工具输出限制常量
    MAX_TOOL_RESULT_SIZE_CHARS: int = 50_000      # 默认单工具结果上限
    _BASH_MAX_RESULT_SIZE_CHARS: int = 30_000     # Bash 硬上限
    _BASH_MAX_OUTPUT_LINES: int = 2000            # Bash 行数上限
    BYTES_PER_TOKEN: int = 4                      # Token 估算系数

    def __init__(self, cwd: str = ".", workspace_uuid: str = "",
                 session_manager=None):
        self.cwd = os.path.abspath(cwd)
        self.workspace_uuid = workspace_uuid
        self.session_manager = session_manager
        self.output_file: str | None = None  # 输出文件路径（agent 在 execute 前设置）

    def execute(self, **kwargs: Any) -> ToolResult:
        """执行工具操作，子类必须实现。"""
        raise NotImplementedError

    def to_schema(self) -> dict[str, Any]:
        """转换为 Anthropic tool schema 格式。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def coerce_input(self, kwargs: dict[str, Any]) -> dict[str, Any] | ToolResult:
        """类型转换 + 参数校验，失败时返回错误 ToolResult。"""

    def save_output_to_file(self, result: ToolResult) -> None:
        """统一保存工具输出到外部文件（由 agent 的 _execute_tool() 在工具执行后统一调用）。"""

    # 静态工具方法
    @staticmethod
    def approx_token_count(text: str) -> int: ...
    @staticmethod
    def truncate_middle(text: str, max_tokens: int) -> str: ...
    @staticmethod
    def truncate_result(text: str, max_chars: int) -> str: ...
```

### 3.2 ToolResult 数据类

```python
class ToolResult:
    """Result of a tool execution.

    New interface (recommended):
        blocks: list[ContentBlock] - typed content blocks
        is_error: bool - whether this is an error result
        meta: dict - optional structured metadata for UI
        completed: bool | None - placeholder lifecycle

    Old interface (backward compat):
        output: str - plain text output (converted to [TextBlock(text=output)])
        error: bool - alias for is_error
        content: list[dict] - legacy multimodal content (deprecated)
        wait_for_user: bool - deprecated alias for completed=False
    """

    def __init__(
        self,
        output: str = "",
        error: bool = False,
        content: list[dict] | None = None,
        # New interface
        blocks: list | None = None,
        is_error: bool = False,
        meta: dict | None = None,
        completed: bool | None = None,
        # Deprecated alias (backward compat)
        wait_for_user: bool = False,
    ):
```

`output`、`error`、`content`、`wait_for_user` 均为向后兼容属性（property），内部统一使用 `blocks`/`is_error`/`meta`/`completed` 存储。

**多模态内容格式**：
```python
content = [
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}},
    {"type": "text", "text": "图片描述..."}
]
```

### 3.3 基类工具方法

**路径与字符串处理**：

| 方法 | 说明 |
|------|------|
| `_resolve_path(path)` | 将相对路径转为绝对路径（基于 cwd） |
| `_clean_surrogates(s)` | 移除 UTF-8 无效的代理字符（U+D800-U+DFFF） |
| `_shell_escape(s)` | Shell 单引号转义 |

**输出保存**：

| 方法 | 说明 |
|------|------|
| `save_output_to_file(result)` | 统一保存工具输出到外部文件（由 agent 的 `_execute_tool()` 在工具执行后统一调用） |

**参数类型修正**：

| 方法 | 说明 |
|------|------|
| `coerce_input(kwargs)` | 根据 parameters schema 先做类型转换（如 str→int、str→bool），跳过缺失的可选参数；再调用 `validate_input()` 校验 required/enum/类型，失败返回错误 ToolResult |

**截断工具方法**（静态方法）：

| 方法 | 说明 |
|------|------|
| `approx_token_count(text)` | 估算文本 token 数（4 字节 ≈ 1 token） |
| `truncate_middle(text, max_tokens)` | Token 预算截断：保留前 40% + 后 40%，中间用标记替代 |
| `truncate_result(text, max_chars)` | 按字符数截断（保留开头，末尾加提示） |

**命令执行**：

| 方法 | 说明 |
|------|------|
| `_run_bash(command, timeout, stdin, max_chars, output_file)` | 通过 Git Bash 执行命令，自动激活 Python 环境 |
| `_run_pwsh(command, timeout, stdin, max_chars, output_file)` | 通过 PowerShell 执行命令，自动设置 UTF-8 编码和 Python 环境 |
| `_start_background_task(command, shell_path, env_prefix)` | 启动后台 bash 任务，返回 task_id |
| `_start_pwsh_background_task(command, env_prefix)` | 启动后台 PowerShell 任务，返回 task_id |
| `_read_background_task(task_id)` | 非阻塞读取后台任务累积输出 |
| `_kill_background_task(task_id)` | 终止后台任务 |
| `_write_stdin_to_task(task_id, text)` | 向运行中的后台任务发送 stdin 输入 |
| `_list_background_tasks()` | 列出所有后台任务及状态 |

**_run_bash 特性**：
- 自动将 Python 环境目录（`_VENV_DIR` 和 `_VENV_SCRIPTS`）添加到 PATH
- 支持超时控制（BashTool/PwshTool 默认 120s、最大 600s；`_run_bash` 函数签名默认 30s）
- **合并 stderr 到 stdout**（`stderr=subprocess.STDOUT`，确保实时输出可见）
- 字符数截断（硬上限 30,000，默认 30,000；`BASH_MAX_OUTPUT_LENGTH` 环境变量可调，不突破硬上限）
- 行数截断（最多 2000 行）
- Token 预算截断（`BASH_MAX_OUTPUT_TOKENS` 环境变量，默认 10,000 tokens，使用 `truncate_middle` 保留首尾）
- 非零退出码时添加 `[exit code: N]` 前缀
- 实时流式输出：逐行写入 `output_file`（供前端轮询）

**_run_pwsh 特性**：
- 与 `_run_bash` 相同的截断和超时逻辑
- 自动设置 UTF-8 编码（`[Console]::OutputEncoding = [UTF8Encoding]::new($false)`）
- 使用 Windows 原生路径，无需路径转换
- PATH 使用 `;` 分隔符，环境变量使用 `$env:VAR` 语法
- 调用方式：`pwsh -NoLogo -NoProfile -NonInteractive -Command <command>`
- 优先使用 pwsh 7，回退到 Windows PowerShell 5.1

---

## 四、工具列表

全部工具平铺在 `core/tools/`，注册名与角色白名单一一对应。可用角色中，master 含全部 22 个；worker 为 master 去掉 todo/cron/message_bus/latex/ask_user 的 17 个（含 agent）；lite 仅 read/write/edit/bash。

| 工具 | 文件 | 说明 | 可用角色 |
|------|------|------|---------|
| read | read.py | 读取文件内容（文本 + 图片 base64 + PDF 按页读取） | master/worker/lite |
| write | write.py | 创建/覆盖文件（自动创建父目录） | master/worker/lite |
| edit | edit.py | 精确文本替换（old_text 必须唯一） | master/worker/lite |
| bash | bash.py | Shell 命令（通过 Git Bash），支持后台执行和交互式 stdin，高风险命令会话级审批 | master/worker/lite |
| pwsh | pwsh.py | PowerShell 命令，支持后台执行和交互式 stdin，高风险命令会话级审批 | master/worker |
| grep | grep.py | 正则搜索（支持 glob/type 过滤） | master/worker |
| find | find.py | 文件查找（glob 模式） | master/worker |
| browser | browser.py | Chrome 自动化（Playwright + CDP） | master/worker |
| web_search | web_search.py | 网络搜索（支持 Bing / Google，委托给 BrowserService） | master/worker |
| memory | memory.py | 长期记忆（knowledge + skill，支持 find 关键词检索） | master/worker |
| python | python_tool.py | Python 代码执行 + 脚本运行，支持后台执行 | master/worker |
| todo | todo.py | 任务规划（整表替换，三态状态） | master/worker |
| latex | latex.py | LaTeX 编译（支持 tectonic/pdflatex/xelatex/lualatex） | master/worker |
| message_bus | message_bus_tool.py | 跨会话消息传递（发送/接收/检查消息） | master/worker |
| cron | cron_tool.py | 用户级定时任务管理（创建/列出/更新/删除/执行/启用/禁用任务） | master/worker |
| read_tool_result | read_tool_result.py | 检索已压缩的工具结果（通过 tool_use_id） | master/worker |
| temp | temp.py | 临时文件和目录管理（按 session 隔离） | master/worker |
| loop | loop.py | 循环任务进度追踪（配合 cron 实现自循环任务） | master/worker |
| pdf2markdown | pdf2markdown.py | PDF/文档转 Markdown（MinerU API，Agent + Precision 双模式） | master/worker |
| skill | skill.py | 技能工具（按角色 frontmatter roles 过滤，见 8.1） | master/worker |
| agent | agent_tool.py | 委派复杂任务给子代理（AgentTool，见 8.2） | master/worker |
| ask_user | ask_user.py | 向用户提问，收集决策（交互式专属） | master |

**注意**：
- 注册表键与白名单名一致（如 `todo`）；个别工具类的 `Tool.name` 属性可能不同（如 TodoWriteTool 的 name 为 `todo_write`，LLM schema 使用类属性 name）
- `skill` 工具由注册表工厂传入 `role=role_cfg.name`，每个角色实例化独立 SkillTool，可见技能集合不同
- `agent` 工具注册名仍为 `agent`，类名为 `AgentTool`

---

## 五、执行流程

### 5.1 工具调用链路（外部优先存储）

```
LLM 响应
│
├─ 解析 tool_use blocks
│   ├─ {"type": "tool_use", "id": "toolu_123", "name": "bash", "input": {"command": "ls"}}
│   └─ ...
│
├─ 对每个 tool_use:
│   ├─ get_tool_by_name(tools, "bash") → tool
│   ├─ tool.output_file = {session_dir}/{tool_use_id}.txt  # 设置输出文件
│   ├─ input_data = tool.coerce_input(input_data)          # 类型转换 + 校验（失败直接返回错误）
│   ├─ result = tool.execute(**input_data) → ToolResult
│   ├─ tool.save_output_to_file(result)                  # bash/python 等流式工具实时写入外部文件
│   └─ 构建 tool_result（小输出内联，大输出落盘）
│       {"type": "tool_result", "tool_use_id": "toolu_123", "content": "...", "is_error": false,
│        "_meta": {"tool_name": "bash", "output_path": "toolu_123.txt",
│                  "file_size": 1234, "truncated": false}}
│       # 超过 10,000 字符或多模态时 content 为空，内容写入外部文件，元信息存 _meta
│
├─ 发送 LLM 前，调用 _resolve_tool_results()
│   ├─ 仅为 content 为空的 tool_result 从外部文件读取内容
│   ├─ 处理截断/压缩标记
│   └─ 注入到消息的 content 字段
│
└─ 发送 tool_result 消息给 LLM，继续循环
```

**关键点**：
- 小输出直接内联在 tool_result 的 `content` 中；超过 10,000 字符阈值或包含图片时才写入外部文件
- 落盘时 Session 只保存元信息（存于 `_meta`：`output_path`、`file_size`、`truncated` 等）
- 外部文件名为 `{tool_use_id}.txt`（多模态为 `.json`）
- 发送 LLM 前，`_resolve_tool_results()` 仅为 content 为空的 tool_result 从外部文件按需读取内容注入消息

### 5.2 工具 Schema 生成

Agent 启动时，将白名单实例化出的所有工具转换为 Anthropic API 格式：

```python
tool_schemas = [tool.to_schema() for tool in tools]

# 示例
[
    {
        "name": "bash",
        "description": "Execute shell commands via Git Bash...",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "..."},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default: 120, max: 600)."}
            },
            "required": ["command"]
        }
    },
    ...
]
```

---

## 六、工具输出限制

每个工具有输出上限：

| 工具 | 硬上限 | 默认值 | 约束 |
|------|--------|--------|------|
| Bash | 30,000 字符 | 30,000 字符 | `BASH_MAX_OUTPUT_LENGTH` 环境变量可调，不突破硬上限 |
| Read | 10,000 tokens | 2000 行 | 单行最长 2000 字符，offset/limit 分片读取，`CILI_FILE_READ_MAX_OUTPUT_TOKENS` 环境变量可调 |
| Grep | 20,000 字符 | 250 匹配行 | 最多 100 个文件，超过自动截断 |
| Find | 100 条路径 | 100 条 | 超过由 `head -n` 截断 |

**截断行为**：超过上限时静默截断，末尾追加提示（如 `... (truncated from N to M chars)`）。

---

## 七、后台任务执行

bash、pwsh、python 和 agent 工具支持后台执行长运行命令/任务，并通过统一的后台任务管理接口进行控制。

### 7.1 功能概述

| 功能 | 参数 | 说明 |
|------|------|------|
| 启动后台任务 | `run_in_background: true` | 立即返回 task_id，命令/Agent 在后台运行 |
| 读取输出 | `read_task: "bg-N"` | 非阻塞读取累积输出/Agent 状态 |
| 终止任务 | `kill_task: "bg-N"` | 终止后台任务 |
| 写入 stdin | `write_stdin: {task_id, text}` | 向运行中的进程发送输入（仅 shell 任务） |
| 列出任务 | `list_tasks: true` | 列出所有后台任务及状态 |

### 7.2 后台任务管理器

`BackgroundTaskManager`（定义在 `core/tools/base.py`）是类级别的单例，所有工具实例共享：

```python
class BackgroundTaskManager:
    _tasks: dict[str, BackgroundTask] = {}   # task_id → 任务对象
    _counter: int = 0                         # 自增计数器
    _lock = threading.Lock()                  # 线程安全锁
```

**BackgroundTask 数据类**：
```python
@dataclass
class BackgroundTask:
    task_id: str                    # 任务 ID（格式：bg-N 或 agent-N）
    task_type: str                  # "shell" 或 "agent"
    command: str                    # 执行的命令（shell 任务）
    process: subprocess.Popen       # 子进程对象（shell 任务）
    output_file: str | None         # 输出文件路径（shell 任务）
    output_queue: queue.Queue       # 输出行队列（供 read_task 消费）
    reader_thread: threading.Thread # 输出读取线程
    status: str                     # running/completed/killed/error
    exit_code: int | None           # 退出码
    created_at: float               # 创建时间戳
    stdin_pipe: Any                 # stdin 管道（供 write_stdin 使用）
    # Agent 专用字段
    agent: Any                   # Agent 实例
    session_manager: Any            # SessionManager 实例
    result: dict | None             # Agent 执行结果
```

### 7.3 使用示例

**bash 后台执行**：
```python
# 启动后台任务
bash(command="npm run build", run_in_background=True)
# → "Background task started. Task ID: bg-1"

# 检查进度
bash(read_task="bg-1")
# → "[Task bg-1 still running] Building..."

# 完成后读取
bash(read_task="bg-1")
# → "[Task bg-1 completed with exit code 0] Build success"
```

**交互式 stdin**：
```python
# 启动需要交互的命令
bash(command="apt-get install foo", run_in_background=True)

# 回答提示
bash(write_stdin={"task_id": "bg-1", "text": "y\n"})
# → "Sent input to task bg-1"
```

**python 后台执行**：
```python
# 后台运行 Python 脚本
python(action="execute_file", file="long_task.py", run_in_background=True)

# 后台运行 Python 代码
python(action="execute", code="import time; time.sleep(60)", run_in_background=True)
```

**agent 后台执行**：
```python
# 后台运行 Agent
agent(task="Complex task...", run_in_background=True)
# → "Agent started in background. Task ID: agent-1"

# 查询 Agent 状态
agent(read_task="agent-1")
# → "Agent agent-1 is still running (5 iterations)"
# → "Agent agent-1 completed: summary..."

# 终止后台 Agent
agent(kill_task="agent-1")
# → "Terminated Agent task agent-1"

# 列出所有后台任务
agent(list_tasks=True)
# → 2 background task(s):
#   bg-1: [shell][running] sleep 100
#   agent-1: [agent][running] Complex task...
```

### 7.4 设计要点

- **线程安全**：`BackgroundTaskManager` 使用锁保护注册表
- **实时输出**：后台任务使用独立线程逐行读取 stdout，写入 `output_queue` 和 `output_file`
- **增量消费**：`read_task` 只返回自上次读取以来的新输出
- **自动清理**：任务完成后自动从注册表移除
- **stdin 保持打开**：后台任务的 stdin pipe 保持打开，支持后续 `write_stdin`

### 7.5 MessageBus 跨会话消息传递

`MessageBus` 是一个轻量级的跨会话消息传递机制，模块级别单例（与 BrowserService/CronScheduler 同模式）。

**核心模块**：`core/message_bus.py`
**工具**：`core/tools/message_bus_tool.py`（master/worker 均可使用）

**功能**：
- `send(to_session, message)` — 发送消息到指定会话
- `receive` — 接收当前会话的所有待读消息
- `check` — 检查是否有未读消息（不消费）
- `list_sessions` — 列出所有注册会话
- `clear` — 清除当前会话的所有消息

**设计要点**：
- **轻量级**：纯内存实现，无持久化，服务器重启后消息丢失
- **线程安全**：使用 `threading.Lock` 保护消息队列
- **按需读取**：消息不自动注入 agent 循环，agent 需主动调用 `message_bus(action="receive")` 检查
- **会话注册**：`web_api.py` 在创建 agent 时自动注册到 MessageBus

### 7.6 read_tool_result — 检索压缩的工具结果

`read_tool_result` 用于检索被压缩（microcompact）的旧工具结果。当工具输出被压缩后，占位符会提示 LLM 使用此工具通过 `tool_use_id` 重新获取原始内容。

**核心模块**：`core/tools/read_tool_result.py`

**调用方式**：
```python
read_tool_result(tool_use_id="toolu_01ABC123")
```

**设计要点**：
- **自动定位文件**：通过 `self.session_manager.session_dir` 找到正确的会话目录
- **Agent 支持**：自动搜索 `exec_*` 子目录中的文件
- **无需路径知识**：LLM 只需传入 `tool_use_id`，无需知道文件存储位置

**压缩占位符格式**：
```
[Compacted: use `read_tool_result` tool with tool_use_id="toolu_01ABC123" to retrieve original content]
```

---

## 八、关键工具详解

### 8.0 loop — 循环任务进度追踪

`loop` 工具用于跟踪跨多次调度周期的迭代任务进度。每个项（文件、记录等）具有三种状态：`"pending"`、`"done"`、`"failed:{reason}"`。

**核心特性**：
- **4 个 Action**：next（取下一个待处理项）、done（标记完成）、fail（标记失败）、status（查看进度）
- **文件驱动**：`source_file` 参数指定项列表文件（每行一个项），同时作为任务标识符
- **自动加载**：`next` 自动从 source_file 读取并追加新增项，已完成项不重复处理
- **自动终止**：配合 cron 的 `remaining` 计数器，所有项完成时任务自动 disable

**调用方式**：
```python
# 获取下一个待处理项（自动从 source_file 加载，所有 action 都需要 source_file）
loop(action="next", source_file="data/files.txt")
# → "进度: 1/3 已完成, 0 失败, 2 待处理\n当前项: file1.md"
# 或所有项完成时："所有项已处理完毕 (完成: 2, 失败: 1)"

# 标记完成
loop(action="done", source_file="data/files.txt", item="file1.md")
# → {"total": 3, "done": 1, "pending": 2, "failed": 0}

# 标记失败
loop(action="fail", source_file="data/files.txt", item="file2.md", error="encoding error")
# → {"total": 3, "done": 1, "pending": 1, "failed": 1}

# 查看进度
loop(action="status", source_file="data/files.txt")
# → {"total": 3, "done": 1, "pending": 1, "failed": 1}
```

**状态文件**：`data/cili/tools/loop/{hash}.json`（hash 由 source_file 绝对路径生成）

```json
{
  "_source_file": "/abs/path/to/data/files.txt",
  "file1.md": "done",
  "file2.md": "failed:encoding error",
  "file3.md": "pending"
}
```

`_source_file` 元数据记录了来源文件路径，方便检查。

**参数**：
- `action`: next | done | fail | status
- `source_file`: 项列表文件路径（每行一个项，必填，同时作为任务标识符）
- `item`: 项标识（done/fail action 使用）
- `error`: 失败原因（fail action 使用）

**实现**：`core/tools/loop.py`

### 8.1 skill — 内置技能访问

`skill` 工具（`SkillTool`）用于列出和读取全局内置技能。技能平铺在 `core/skills/{name}/skill.md`，每个技能的 frontmatter 通过 `roles` 字段声明适用角色；**缺省 `roles` 视为对所有角色可见**。

**核心模块**：`core/tools/skill.py`（扫描目录 `core/skills/`）

**roles 过滤规则**：
- frontmatter `roles: [master]` → 仅 master 可见（如 grilling / create-skill / research / code-review / skillify / learning）
- frontmatter `roles: [worker]` → 仅 worker 可见（如 context-bounded-processing）
- frontmatter `roles: [master, worker]` → master/worker 可见（如 task-delegation）
- frontmatter `roles: [master, worker, lite]` → 三个角色均可见（如 file-processing）
- 无 `roles` 字段 → 全部角色可见

**调用方式**：
```python
# 列出当前角色可见的技能
skill(action="list")
# → "Available skills for master (N):"
#   [skill-id] skill name
#     skill description

# 读取技能全文（按目录名）
skill(action="read", skill_id="large-file-processing")
```

**设计要点**：
- **角色参数化**：`SkillTool.__init__(role=...)`，注册表工厂传入 `role=role_cfg.name`，同一实例按角色过滤可见技能
- **actions**：`list`（列出技能）、`read`（读取全文）
- **不再有 `shared/` 前缀 id**：技能 id 即目录名，统一目录平铺

### 8.2 agent 工具 — 任务委派（AgentTool）

`agent` 工具在独立的子代理（Worker/Lite）中执行复杂任务。工具注册名 `agent`，类名为 `AgentTool`（`core/tools/agent_tool.py`），master 与 worker 白名单包含（委派深度仅 1 层，见下）。

**调用方式**：
```python
# 同步模式（阻塞直到完成）
agent(
    task="Read input.txt, translate to Chinese, write to output.txt",
    plan=["Read input.txt", "Translate content", "Write result"],
    agent_type="worker",        # "worker"（默认）| "lite"
)

# 后台模式（立即返回 task_id）
agent(
    task="Long-running task...",
    run_in_background=True
)

# 查询后台任务状态
agent(read_task="agent-1")

# 终止后台任务
agent(kill_task="agent-1")

# 列出所有后台任务（shell + agent）
agent(list_tasks=True)
```

**参数**：
- `task`: 任务目标描述（必填）
- `plan`: 执行计划（有序步骤列表）
- `agent_type`: 子代理角色，`"worker"`（默认，完整工具集 + check 阶段）或 `"lite"`（最小 read/write/edit/bash，无 check 阶段）
- `run_in_background`: 后台执行模式（立即返回 task_id）
- `read_task`: 读取后台 Agent 状态
- `kill_task`: 终止后台 Agent
- `list_tasks`: 列出所有后台任务
- `temperature`: 覆盖本次 Agent 的 LLM temperature（0.0~1.0，可选）
- `label`: UI 显示标签（最长 64 字符，可选）

**返回值**：
```python
# 同步模式
{"status": "completed", "summary": "翻译完成", "iterations": 12}
# 或 {"status": "error", "message": "...", "iterations": 3}      # LLM 调用失败
# 或 {"status": "timeout", "iterations": 50}                      # 超过最大迭代次数
# 或 {"status": "stopped", "message": "<summary>", "iterations": 5}       # 用户手动停止
# 或 {"status": "failed", "message": "...", "iterations": 10}     # 连续工具调用失败

# 后台模式
"Agent started in background. Task ID: agent-1"
```

**子代理构造**：`AgentTool` 使用统一 `Agent`（`core/agent.py`），按 `agent_type` 选择角色：

```python
agent = Agent(
    config=self.config,          # 全局配置（角色模型继承）
    role=agent_type,             # "worker" | "lite"，决定工具白名单与行为开关
    task=task,
    plan=plan,
    workspace_uuid=...,
    cwd=...,
    stop_check=self.stop_check,  # master Agent 创建工具后注入
    session_dir=exec_dir,        # exec_id 独立目录
    exec_id=exec_id,
    temperature=temperature,
    approval_store=self.approval_store,  # 根代理的会话级审批存储，子代理共享
)
agent.run()
```

子代理的工具集由 `agent_type` 对应角色的 JSON 白名单决定（worker 17 个 / lite 4 个）。

**关键特性**：
- **独立工具集**：worker 17 个 / lite 4 个（取决于 agent_type）
- **结构化任务**：task + plan 拼接到 system prompt 末尾（不可压缩）
- **1 小时超时**
- **委派深度限制（仅 1 层）**：只有 master(0) 可委派 worker/lite(1)；depth≥1 的子代理再调用 `agent` 工具直接报错，应自行完成任务。子代理构造时传 `delegation_depth = parent + 1`
- **后台执行**：`run_in_background=true` 在独立线程中运行子代理
- **懒加载 UI**：Agent 结果通过 tool_result 中的 exec_id 懒加载渲染

**会话消息结构**：
- 主会话通过 tool_use(tool: agent) + tool_result(exec_id) 消息对呈现 Agent
- 完整执行日志存入独立 `exec_*.json` 文件，每轮迭代实时保存

**后台 Agent 生命周期**：
1. `run_in_background=True` → 注册 task_id（格式：`agent-N`）
2. 独立线程运行 `agent.run()`，完成后自动更新 tool_result 状态
3. `read_task` → 查询状态（running/completed）和摘要
4. `kill_task` → 设置 `agent._stopped=True` 终止 Agent

**回调链路**：
```
web_api.py 注入 on_agent_start / on_agent_complete 回调
  → Agent._on_agent_start / _on_agent_complete
    → AgentTool.on_agent_start / on_agent_complete
      → 推送 SSE 事件（agent_start / agent_complete）
        → 前端渲染卡片 / 更新状态
```

**实现**：`core/tools/agent_tool.py`（AgentTool），子代理由 `core/agent.py` 的统一 `Agent`（autonomous 模式）驱动。

### 8.3 temp — 临时文件/目录管理

`temp` 工具用于管理当前 session 的临时文件和目录。临时数据存放在 `{CILI_TMP}/{session_id}/`（`CILI_TMP` 环境变量默认为 `data/tmp/`；session_id 缺失时使用 `no-session`）。

**Actions**：

| action | 说明 |
|--------|------|
| create_file | 创建临时文件（需 `name`，可选 `content`） |
| create_dir | 创建临时目录（需 `name`） |
| list | 列出当前 session 的所有临时文件/目录 |
| cleanup | 删除当前 session 的整个临时目录 |

**示例**：
```json
{"action": "create_file", "name": "data.json", "content": "{...}"}
{"action": "create_dir", "name": "downloads"}
{"action": "list"}
{"action": "cleanup"}
```

**设计要点**：
- **Session 隔离**：每个 session 有独立的临时目录
- **自动清理**：调用 `cleanup` 可一次性删除所有临时文件
- **路径解析**：根目录由 `CILI_TMP` 环境变量决定（默认 `data/tmp/`），下按 session_id 隔离

**实现**：`core/tools/temp.py`

---

## 九、添加新工具

### 9.1 创建工具文件

在 `core/tools/` 平铺目录下创建新文件：

```python
# core/tools/my_tool.py
from core.tools.base import Tool, ToolResult

class MyTool(Tool):
    name = "my_tool"
    description = "My custom tool that does something useful."
    parameters = {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "The input to process"
            }
        },
        "required": ["input"]
    }

    def execute(self, **kwargs) -> ToolResult:
        input_text = kwargs.get("input", "")

        # 实现工具逻辑
        result = f"Processed: {input_text}"

        return ToolResult(output=result, error=False)
```

### 9.2 注册工具

在 `core/tools/registry.py` 的 `TOOL_REGISTRY` 中注册工厂（普通工具用 `_factory`；需特殊参数时按需设置 `needs_config` / `needs_approval`，或自定义工厂）：

```python
# core/tools/registry.py
from core.tools.my_tool import MyTool

TOOL_REGISTRY = {
    # ... existing tools ...
    "my_tool": _factory(MyTool),
}
```

### 9.3 角色白名单

将工具名加入希望开放角色的 `core/agents/{role}.json` 的 `tools` 数组：

```json
{
  "name": "master",
  "tools": [
    "read",
    "...",
    "my_tool"
  ]
}
```

不加入任何角色白名单的工具不会被实例化（白名单外注册名视为未注册，跳过并告警）。

### 9.4 工具命名规范

- 使用 snake_case（如 `web_search`）
- 名称应清晰表达工具功能，并与注册表键、角色白名单保持一致
- 避免与现有工具重名

---

## 十、设计决策

### 10.1 为什么用"统一注册表 + 角色白名单"？

- **单一注册点**：所有工具集中在 `TOOL_REGISTRY`，一处注册、全局可见，取代旧 shared/root/sub 三层目录与分散的 `create_shared_tools`/`create_root_tools`/`create_sub_tools` 硬编码工厂
- **声明式角色配置**：工具集由 `core/agents/{role}.json` 的 `tools` 数组声明，新增/调整角色只改 JSON，不动代码
- **职责边界清晰**：同一工具类可被多角色共享（如 read/write/edit/bash），角色差异完全由白名单体现
- **无冗余复制**：skill 等"按角色参数化"的工具通过工厂传参（`role`）复用同一实现，不再为不同角色创建独立实例目录

### 10.2 为什么 read/write/edit 用直接 I/O？

- **性能**：避免 subprocess 开销（spawn Python 进程）
- **简单**：直接 `open()` 读写，不需要解析输出
- **可靠**：无 shell 转义问题

### 10.3 为什么 bash 工具用 Git Bash？

- **Windows 兼容**：Windows 原生 cmd/PowerShell 语法不同
- **Unix 工具**：grep、sed、awk 等 Unix 工具在 Git Bash 中可用
- **一致性**：跨平台行为一致

### 10.4 为什么需要 pwsh 工具？

- **PowerShell 原生**：某些 Windows 管理任务（注册表、COM、.NET）用 PowerShell 更自然
- **路径格式**：pwsh 使用 Windows 原生路径（`C:\...`），无需像 bash 那样转换
- **环境变量**：`$env:VAR` 语法，与 Windows 生态一致
- **UTF-8 编码**：自动设置 `[Console]::OutputEncoding`，正确处理非 ASCII 输出
- **与 bash 互补**：bash 擅长 Unix 工具链，pwsh 擅长 Windows 原生操作

### 10.5 为什么需要跨工具隔离？

bash/pwsh/python 三个执行工具互相隔离，不能从一个工具调用另一个：

- **防止安全绕过**：LLM 可能通过 `bash(command="powershell -Command ...")` 绕过 pwsh 的 deny_patterns
- **工具职责清晰**：每个工具有自己的 deny_patterns 和安全检查，混用会导致安全策略失效
- **引导正确使用**：拦截时返回错误提示用户使用正确的工具，形成正向引导

**实现方式**：

| 工具 | 拦截目标 | deny_patterns 示例 |
|------|---------|-------------------|
| `bash` | powershell, pwsh, python/py, cmd, wsl, eval | `(?<![a-zA-Z0-9_-])(?:powershell\|pwsh)(?:\.exe)?(?![a-zA-Z0-9_-])` |
| `pwsh` | bash/sh, python/py, cmd, wsl, powershell 重入, iex | `(?<![a-zA-Z0-9_-])(?:python3?\|pythonw?\|py)(?:\.exe)?(?![a-zA-Z0-9_-])` |
| `python` | subprocess→bash/pwsh, eval, exec | `subprocess\.\w+\s*\(\s*[\[\(]?\s*['"](?:bash\|pwsh\|powershell)` |

**扫描前的字符串剥离**（`_strip_shell_strings`，base.py）：

deny 扫描只针对代码部分，不扫描字符串字面量，避免字符串数据误触发关键字规则（如 `Write-Output "pwsh tool works!"` 被当作 PowerShell 重入拦截）：

- 单引号/双引号字符串内容替换为空格（保留 token 边界）
- 双引号内的 `$(...)` 子表达式和 bash 的 `` `...` `` 替换会**执行为代码**，原样保留参与扫描
- 未闭合的引号剥到行尾（该命令本身会是解析错误，不会执行）
- 剥离后被隐藏的执行路径（如 `iex '...'` / `eval '...'`，payload 在字符串里）通过直接拦截 `Invoke-Expression`/`iex`/`eval` 本身来封堵

**pwsh 递归强删规则**（`Remove-Item` 系）的设计要点：

- 别名齐全：`Remove-Item` 及其别名 `rm`/`ri`/`del`/`erase`/`rd`/`rmdir`
- 参数顺序无关：`-Force -Recurse` 与 `-Recurse -Force` 等价拦截（用 lookahead 实现）
- 支持参数缩写：`-r`/`-rec`、`-fo`/`-forc` 等合法前缀
- 目标覆盖：盘符路径正反斜杠（`C:\` / `C:/`）、`$env:` 路径、注册表（`HKLM:\` 借助 `M:\` 匹配）
- 不带盘符目标的递归强删（如 `Remove-Item -Recurse -Force .\build`）不拦截，保留正常项目清理能力

### 10.6 为什么工具输出要截断？

- **上下文窗口限制**：工具输出太大会占用 LLM 上下文
- **成本控制**：减少 token 消耗
- **安全性**：防止恶意输出撑爆上下文

### 10.7 为什么采用"内联为主、大输出落盘"的混合存储？

- **Session 体积小**：超过 10,000 字符的输出和多模态内容不存 session，只存 `_meta` 元信息（`output_path`、`file_size`、`truncated` 等）
- **小输出零开销**：常规输出直接内联在 tool_result 的 content 中，无需磁盘往返
- **历史加载快**：页面刷新时不必加载大量工具输出
- **按需读取**：LLM 可根据需要重读完整内容（截断时会提示使用 read 工具读取原文件）
- **前后端解耦**：后端注入内容，前端无感知

### 10.8 高风险命令的会话级审批（ask 档）

deny 黑名单分两档：**ask**（破坏性操作，可询问用户）与 **deny**（跨工具隔离、iex/eval 等架构性拒绝，硬拒）。

ask 档命中时，命令不直接拒绝，而是走"拦截 → 询问 → 会话级批准"流程：

1. **工具层**（bash/pwsh）：查 `ApprovalStore.is_approved(decision_id)` → 已批准放行执行；未批准返回 `completed=False` 占位符 + `meta.approval_required`（decision_id 为规范化命令的 sha256 前 16 位，确定性）
2. **Master 循环**（`core/agent.py`）：把首个 approval_required 降级为错误提示、记录 pending，批处理完后**合成一张 ask_user 卡**（选项"允许本次会话"/"拒绝"）→ 占位 break；同批多条只问一条，其余拒绝
3. **answer 端点**（web_api.py）：答案含"允许本次会话" → `store.approve(did, cmd)`；否则仅清 pending（自定义输入视为拒绝）
4. **模型重发**：resume 后模型读到批准，原样重发命令 → `is_approved` 命中 → 放行；此后**本会话内**（含子代理）同命令不再询问

**会话级、内存不持久化**：`ApprovalStore` 由 master Agent 持有（`_approved: decision_id→command`，不按次数消费 + 单槽 `pending`），服务器重启即失效，不写配置不落盘。

**子代理共享**：根/子代理的 bash/pwsh 与 AgentTool 构造时透传同一 `ApprovalStore` 实例——
- 已批准命令子代理可直接执行（工具层共享放行）
- 已批准命令列表注入子代理 pinned 任务消息（`build_approved_commands_section`，approval.py），子模型知晓可直接执行
- 子代理无 ask_user：未批准命令的占位符被子循环 `_downgrade_approval_result`（`core/agent.py`）降级为普通 error，不挂起不询问

**消息配对**：合成 ask_user 需手动补 `assistant` tool_use 块（`generate_short_id()` 生成 id），且全部工具结果处理完后再追加，避免悬挂 tool_result 或打断本批其他 tool_use 的配对。

相关文件：`core/tools/approval.py`（ApprovalStore、ask/deny 常量、decision_id 与文案）、`core/tools/bash.py`、`core/tools/pwsh.py`、`core/agent.py`（`_handle_approval_required`、`_downgrade_approval_result`）、`core/tools/agent_tool.py`（透传）、`web/web_api.py`（answer_ask_user 记录批准）。

---

## 十一、相关文件

| 文件 | 职责 |
|------|------|
| `core/tools/registry.py` | TOOL_REGISTRY 统一注册表 + `create_tools()` 工厂（按角色白名单实例化） |
| `core/tools/__init__.py` | 重新导出 create_tools / TOOL_REGISTRY，提供 `get_tool_by_name()` 查找 |
| `core/tools/base.py` | Tool 基类 + ToolResult + BackgroundTaskManager |
| `core/tools/approval.py` | 会话级审批（ApprovalStore、ask/deny 常量、文案与 decision_id） |
| `core/tools/*.py` | 全部工具实现（平铺） |
| `core/agents/*.json` | 角色定义：工具白名单（tools）、行为开关、system prompt 块 |
| `core/agent_config.py` | AgentRoleConfig + `load_agent_role`（读取角色 JSON） |
| `core/base_agent.py` | 工具执行循环（`_execute_tool`）+ `_resolve_tool_results()` |
| `core/agent.py` | 统一 Agent（master 交互 / worker/lite 自主）、审批合成与降级 |
| `core/skills/` | 全局内置技能（平铺目录，frontmatter roles 声明适用角色） |

---

**文档版本**: v2.0  
**创建时间**: 2026-08-25  
**更新时间**: 2026-09-10（重构：工具系统从 shared/root/sub 三层目录 + 硬编码工厂改为统一注册表 TOOL_REGISTRY + 角色 JSON 白名单；llm 工具已移除；skill 改为平铺 + frontmatter roles 过滤；agent 改为 AgentTool 并新增 agent_type；审批逻辑迁至统一 Agent）  
**状态**: 已实现
