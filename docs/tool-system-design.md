# Tool 工具系统设计文档

本文档描述 Cili Agent 的工具系统架构、工具基类、工具层次结构和执行流程。

---

## 一、功能概述

工具（Tools）是 Agent 与外部世界交互的能力。每个工具封装一种操作（读文件、执行命令、搜索等），由 LLM 通过 tool_use 调用。

**核心特性**：
- **三层工具架构**：shared（共用）、root（RootAgent 专属）、sub（SubAgent 专属）
- **统一基类**：所有工具继承 `Tool` 基类，共享路径解析、命令执行等方法
- **JSON Schema 参数**：工具参数使用标准 JSON Schema 描述
- **ToolResult 返回**：统一的结果数据结构
- **工具注册表**：动态创建和查找工具实例

---

## 二、架构设计

### 2.1 工具层次

工具按使用场景分为三层目录：

```
core/tools/
├── __init__.py              # 顶层注册表：create_tools(), get_tool_by_name()
├── shared/                  # 共用工具（17~18 个）
│   ├── __init__.py          # create_shared_tools()
│   ├── base.py              # Tool 基类 + ToolResult + BackgroundTaskManager
│   ├── read.py              # 读取文件
│   ├── write.py             # 写入文件
│   ├── edit.py              # 精确替换
│   ├── bash.py              # Shell 命令
│   ├── grep.py              # 正则搜索
│   ├── find.py              # 文件查找
│   ├── browser.py           # 浏览器自动化
│   ├── web_search.py        # 网络搜索
│   ├── memory.py            # 长期记忆
│   ├── python_tool.py       # Python 执行（共用）
│   ├── llm_tool.py          # 单轮 LLM 调用（条件加载，需配置 llm_model）
│   ├── todo.py              # 任务规划（TodoWrite）
│   ├── latex.py             # LaTeX 编译（支持 tectonic/pdflatex/xelatex/lualatex）
│   ├── message_bus_tool.py  # 跨会话消息传递
│   ├── cron_tool.py         # 用户级定时任务管理
│   ├── read_tool_result.py  # 检索压缩的工具结果
│   ├── temp.py              # 临时文件/目录管理
│   ├── loop.py              # 循环任务进度追踪（配合 cron 使用）
│   └── skill.py             # 技能工具（共用逻辑，RootAgent/SubAgent 各自实例化）
├── root/                    # RootAgent 专属（3 个）
│   ├── __init__.py          # create_root_tools()
│   ├── subagent_tool.py     # SubAgent 委派工具
│   └── ask_user.py          # AskUser 用户交互工具
└── sub/                     # SubAgent 专属（无独立文件）
    └── __init__.py          # create_sub_tools()（复用 shared skill + 创建 sub 专属 SkillTool 实例）
```

**工厂函数**：
- `create_shared_tools(**kwargs, config=None, cron_task_id="")` → 17 个固定工具 + LLMTool（条件加载）= 17~18 个共用工具
- `create_root_tools(**kwargs)` → 3 个 RootAgent 专属工具（SkillTool + SubAgentTool + AskUserTool）
- `create_sub_tools(**kwargs, config=None, cron_task_id="")` → `create_shared_tools()` + 1 个 SubAgent 专属 SkillTool 实例
- `create_tools(**kwargs, config=None)` = `create_shared_tools() + create_root_tools()` → RootAgent 的完整工具集（20~21 个）
- SubAgent 的工具集 = `create_sub_tools()` → 18~19 个工具

**工具分配**：

| Agent 类型 | 工具集 | 数量 |
|-----------|--------|------|
| RootAgent | shared + root | 19~20 个 |
| SubAgent | shared + sub (SkillTool) | 17~18 个 |

**LLMTool 条件加载**：当 `config.llm_model` 未配置时，`create_shared_tools()` 不包含 LLMTool（总数为 16 而非 17）。

### 2.2 工具注册表

`core/tools/__init__.py` 提供工具注册和查找：

```python
# 创建 RootAgent 的完整工具集
tools = create_tools(cwd="/workspace", workspace_uuid="abc123", session_manager=sm, config=cfg)

# 按名称查找工具（O(1) 缓存查找）
tool = get_tool_by_name(tools, "bash")
```

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

    def coerce_input(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """根据 parameters schema 修正 LLM 传入的参数类型（如 str→int）。"""

    def save_output_to_file(self, output: str) -> None:
        """统一保存工具输出到外部文件（由 _execute_tool 在 finally 中调用）。"""

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
@dataclass
class ToolResult:
    output: str                          # 工具输出文本
    error: bool = False                  # 是否出错
    content: list[dict] | None = None    # 多模态内容（如图片）
```

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
| `save_output_to_file(output)` | 统一保存工具输出到外部文件（由 `_execute_tool` 在 finally 中调用） |

**参数类型修正**：

| 方法 | 说明 |
|------|------|
| `coerce_input(kwargs)` | 根据 parameters schema 修正 LLM 传入的参数类型（如 str→int、str→bool），跳过缺失的可选参数 |

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
| `_start_background_task(command, shell_path, env_prefix)` | 启动后台任务，返回 task_id |
| `_read_background_task(task_id)` | 非阻塞读取后台任务累积输出 |
| `_kill_background_task(task_id)` | 终止后台任务 |
| `_write_stdin_to_task(task_id, text)` | 向运行中的后台任务发送 stdin 输入 |
| `_list_background_tasks()` | 列出所有后台任务及状态 |

**_run_bash 特性**：
- 自动将 Python 环境目录（`_VENV_DIR` 和 `_VENV_SCRIPTS`）添加到 PATH
- 支持超时控制（默认 30s）
- **合并 stderr 到 stdout**（`stderr=subprocess.STDOUT`，确保实时输出可见）
- 字符数截断（硬上限 30,000，默认 30,000；`BASH_MAX_OUTPUT_LENGTH` 环境变量可调，不突破硬上限）
- 行数截断（最多 2000 行）
- Token 预算截断（`BASH_MAX_OUTPUT_TOKENS` 环境变量，默认 10,000 tokens，使用 `truncate_middle` 保留首尾）
- 非零退出码时添加 `[exit code: N]` 前缀
- 实时流式输出：逐行写入 `output_file`（供前端轮询）

---

## 四、工具列表

### 4.1 共用工具（shared/）

| 工具 | 文件 | 说明 |
|------|------|------|
| read | read.py | 读取文件内容（文本 + 图片 base64） |
| write | write.py | 创建/覆盖文件（自动创建父目录） |
| edit | edit.py | 精确文本替换（old_text 必须唯一） |
| bash | bash.py | Shell 命令（通过 Git Bash），支持后台执行和交互式 stdin |
| grep | grep.py | 正则搜索（支持 glob/type 过滤） |
| find | find.py | 文件查找（glob 模式） |
| browser | browser.py | Chrome 自动化（Playwright + CDP） |
| web_search | web_search.py | Bing 中国搜索（委托给 browser） |
| memory | memory.py | 长期记忆（knowledge + skill） |
| python | python_tool.py | Python 代码执行 + 脚本运行，支持后台执行 |
| llm | llm_tool.py | 单轮 LLM 调用（翻译/摘要/提取） |
| todo_write | todo.py | 任务规划（整表替换，三态状态） |
| latex | latex.py | LaTeX 编译（支持 tectonic/pdflatex/xelatex/lualatex） |
| message_bus | message_bus_tool.py | 跨会话消息传递（发送/接收/检查消息） |
| cron | cron_tool.py | 用户级定时任务管理（创建/列出/删除/执行任务） |
| read_tool_result | read_tool_result.py | 检索已压缩的工具结果（通过 tool_use_id） |
| temp | temp.py | 临时文件和目录管理（按 session 隔离） |
| loop | loop.py | 循环任务进度追踪（配合 cron 实现自循环任务） |

### 4.2 RootAgent 专属工具（root/）

| 工具 | 文件 | 说明 |
|------|------|------|
| skill | skill.py（shared/） | RootAgent 技能工具实例（扫描 core/skills/root/） |
| subagent | subagent_tool.py | 委派复杂任务给 SubAgent |
| ask_user | ask_user.py | 向用户提问，收集决策（RootAgent 专属，SubAgent 后台无法交互） |

**注意**：`skill.py` 的共用逻辑位于 `shared/` 目录，RootAgent 和 SubAgent 各自创建独立的 SkillTool 实例，扫描不同的技能目录。

### 4.3 SubAgent 专属工具（sub/）

| 工具 | 文件 | 说明 |
|------|------|------|
| skill | skill.py（shared/） | SubAgent 技能工具实例（扫描 core/skills/sub/） |

SubAgent 没有独立的工具文件，而是在 `sub/__init__.py` 中复用 shared 的 SkillTool 类，传入不同的 `skills_dir` 参数。

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
│   ├─ input_data = tool.coerce_input(input_data)          # 修正参数类型
│   ├─ result = tool.execute(**input_data) → ToolResult
│   ├─ tool.save_output_to_file(result.output)             # 保存输出到外部文件
│   └─ 构建 tool_result 元信息（不存内容）
│       {"type": "tool_result", "tool_use_id": "toolu_123", "tool_name": "bash",
│        "_file_size": 1234, "_truncated": false, "_output_path": "toolu_123.txt"}
│
├─ 发送 LLM 前，调用 _resolve_tool_results()
│   ├─ 从外部文件按需读取内容
│   ├─ 处理截断/压缩标记
│   └─ 注入到消息的 content 字段
│
└─ 发送 tool_result 消息给 LLM，继续循环
```

**关键点**：
- Session 中只保存工具输出的元信息（`_file_size`、`_truncated`、`_output_path` 等）
- 实际内容保存在外部文件 `{tool_use_id}.txt`
- 发送 LLM 前，`_resolve_tool_results()` 从外部文件按需读取内容注入消息

### 5.2 工具 Schema 生成

RootAgent 启动时，将所有工具转换为 Anthropic API 格式：

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
                "timeout": {"type": "integer", "default": 30}
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
| Bash | 30,000 字符 | 10,000 字符 | `BASH_MAX_OUTPUT_LENGTH` 环境变量可调，不突破硬上限 |
| Read | 10,000 tokens | 2000 行 | 单行最长 2000 字符，offset/limit 分片读取，`CILI_FILE_READ_MAX_OUTPUT_TOKENS` 环境变量可调 |
| Grep | 20,000 字符 | 250 匹配行 | 最多 100 个文件，超过自动截断 |
| Find | 100 条路径 | 100 条 | 超过由 `head -n` 截断 |

**截断行为**：超过上限时静默截断，末尾追加提示（如 `... (truncated from N to M chars)`）。

---

## 七、后台任务执行

bash、python 和 subagent 工具支持后台执行长运行命令/任务，并通过统一的后台任务管理接口进行控制。

### 7.1 功能概述

| 功能 | 参数 | 说明 |
|------|------|------|
| 启动后台任务 | `run_in_background: true` | 立即返回 task_id，命令/SubAgent 在后台运行 |
| 读取输出 | `read_task: "bg-N"` | 非阻塞读取累积输出/SubAgent 状态 |
| 终止任务 | `kill_task: "bg-N"` | 终止后台任务 |
| 写入 stdin | `write_stdin: {task_id, text}` | 向运行中的进程发送输入（仅 shell 任务） |
| 列出任务 | `list_tasks: true` | 列出所有后台任务及状态 |

### 7.2 后台任务管理器

`BackgroundTaskManager`（定义在 `base.py`）是类级别的单例，所有工具实例共享：

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
    task_id: str                    # 任务 ID（格式：bg-N 或 subagent-N）
    task_type: str                  # "shell" 或 "subagent"
    command: str                    # 执行的命令（shell 任务）
    process: subprocess.Popen       # 子进程对象（shell 任务）
    output_file: str | None         # 输出文件路径（shell 任务）
    output_queue: queue.Queue       # 输出行队列（供 read_task 消费）
    reader_thread: threading.Thread # 输出读取线程
    status: str                     # running/completed/killed/error
    exit_code: int | None           # 退出码
    created_at: float               # 创建时间戳
    stdin_pipe: Any                 # stdin 管道（供 write_stdin 使用）
    # SubAgent 专用字段
    subagent: Any                   # SubAgent 实例
    session_manager: Any            # SessionManager 实例
    result: dict | None             # SubAgent 执行结果
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

**subagent 后台执行**：
```python
# 后台运行 SubAgent
subagent(task="Complex task...", run_in_background=True)
# → "SubAgent started in background. Task ID: subagent-1"

# 查询 SubAgent 状态
subagent(read_task="subagent-1")
# → "SubAgent subagent-1 is still running (5 iterations)"
# → "SubAgent subagent-1 completed: summary..."

# 终止后台 SubAgent
subagent(kill_task="subagent-1")
# → "Terminated SubAgent task subagent-1"

# 列出所有后台任务
subagent(list_tasks=True)
# → 2 background task(s):
#   bg-1 (shell): sleep 100 [running]
#   subagent-1 (subagent): Complex task... [running]
```

### 7.5 MessageBus 跨会话消息传递

`MessageBus` 是一个轻量级的跨会话消息传递机制，模块级别单例（与 BrowserService/CronScheduler 同模式）。

**核心模块**：`core/message_bus.py`
**工具**：`core/tools/shared/message_bus_tool.py`（shared 工具，RootAgent 和 SubAgent 均可使用）

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

### 7.4 设计要点

- **线程安全**：`BackgroundTaskManager` 使用锁保护注册表
- **实时输出**：后台任务使用独立线程逐行读取 stdout，写入 `output_queue` 和 `output_file`
- **增量消费**：`read_task` 只返回自上次读取以来的新输出
- **自动清理**：任务完成后自动从注册表移除
- **stdin 保持打开**：后台任务的 stdin pipe 保持打开，支持后续 `write_stdin`

### 7.6 read_tool_result — 检索压缩的工具结果

`read_tool_result` 用于检索被压缩（microcompact）的旧工具结果。当工具输出被压缩后，占位符会提示 LLM 使用此工具通过 `tool_use_id` 重新获取原始内容。

**核心模块**：`core/tools/shared/read_tool_result.py`

**调用方式**：
```python
read_tool_result(tool_use_id="toolu_01ABC123")
```

**设计要点**：
- **自动定位文件**：通过 `self.session_manager.session_dir` 找到正确的会话目录
- **SubAgent 支持**：自动搜索 `exec_*` 子目录中的文件
- **无需路径知识**：LLM 只需传入 `tool_use_id`，无需知道文件存储位置

**压缩占位符格式**：
```
[Compacted: use `read_tool_result` tool with tool_use_id="toolu_01ABC123" to retrieve original content]
```

**实现**：`core/tools/shared/read_tool_result.py`（shared 工具，RootAgent 和 SubAgent 均可使用）

---

## 八、关键工具详解

### 8.0 loop — 循环任务进度追踪

`loop` 工具用于跟踪跨多次调度周期的迭代任务进度。每个项（文件、记录等）具有三种状态：`"pending"`、`"done"`、`"failed:{reason}"`。

**核心特性**：
- **5 个 Action**：sync（同步项列表）、next（取下一个待处理项）、done（标记完成）、fail（标记失败）、status（查看进度）
- **幂等同步**：`sync` 只追加新增项，已完成项不重复处理
- **cron 集成**：检测到 `cron_task_id` 时，自动同步 cron 的 `remaining` 计数器
- **自动终止**：配合 cron 的 `remaining` 计数器，所有项完成时任务自动 disable

**调用方式**：
```python
# 同步文件列表（幂等，只追加新项）
loop(action="sync", items=["file1.md", "file2.md", "file3.md"])
# → {"added": 3, "total": 3, "pending": 3, "done": 0, "failed": 0}

# 获取下一个待处理项
loop(action="next")
# → {"item": "file1.md"} 或 {"item": null}

# 标记完成
loop(action="done", item="file1.md")
# → {"done": 1, "pending": 2, "failed": 0}

# 标记失败
loop(action="fail", item="file2.md", error="encoding error")
# → {"done": 1, "pending": 1, "failed": 1}

# 查看进度
loop(action="status")
# → {"total": 3, "done": 1, "pending": 1, "failed": 1}
```

**状态文件**：`data/cili/tools/loop/{task_id}.json`

```json
{
  "file1.md": "done",
  "file2.md": "failed:encoding error",
  "file3.md": "pending"
}
```

**与 cron 集成**：
当 LoopTool 由 cron 触发时（`cron_task_id` 不为空），`sync` action 会自动同步 cron 的 `remaining` 计数器：

```
1. Cron 执行前：remaining = 9999（默认值）
2. SubAgent 调用 loop(sync, items=[10005个文件])
   → loop 检测到 cron_task_id → 更新 cron.remaining = 10005 (pending_count + 1)
3. SubAgent 处理 1 个文件
4. Cron 下次执行：remaining = 10005 - 1 = 10004
5. ... 重复直到 remaining = 0 → 自动 disable
```

**参数**：
- `action`: sync | next | done | fail | status
- `task_id`: 任务标识（默认使用 cron_task_id）
- `items`: 项列表（sync action 使用）
- `item`: 项标识（done/fail action 使用）
- `error`: 失败原因（fail action 使用）

**实现**：`core/tools/shared/loop.py`

### 8.1 llm — 单轮 LLM 调用

`llm` 是共享工具（文件：`llm_tool.py`），用于单轮 LLM 调用（翻译、摘要、提取等）。**需要配置 LLM 模型**（`llm_model`），否则返回"不可用"。

**输入方式**：
- `input_text`: 直接文本输入
- `input_file`: 从文件读取

**输出方式**：
- 默认：返回文本结果
- `output_file`: 写入文件

**结构化输出**：
- `output_schema`: JSON Schema，定义期望输出结构

**示例**：
```json
// 翻译
{"input_text": "Translate to Chinese: Hello world"}

// 文件处理
{"input_file": "article.txt", "output_file": "article_zh.txt"}

// 结构化提取
{"input_text": "Extract dates from: meeting on 2026-08-24", 
 "output_schema": {"type": "object", "properties": {"dates": {"type": "array"}}}}
```

**实现**：`shared/llm_tool.py`，使用 `chat()` 或 `chat_structured()` 调用 LLM API。

**与 subagent 工具的区别**：

| 特性 | llm | subagent 工具 |
|------|-----|---------------|
| 工具 | 无（纯 LLM 调用） | 完整工具集 |
| 适用 | 简单文本处理 | 需要工具交互的复杂任务 |
| 超时 | LLM API 超时 | 1 小时 |
| 上下文 | 单轮 | 多轮循环 |

### 8.2 subagent 工具 — 任务委派

`subagent` 工具在独立的 SubAgent 中执行复杂任务。

**调用方式**：
```python
# 同步模式（阻塞直到完成）
subagent(
    task="Read input.txt, translate to Chinese, write to output.txt",
    plan=["Read input.txt", "Translate content", "Write result"],
    max_iterations=50
)

# 后台模式（立即返回 task_id）
subagent(
    task="Long-running task...",
    run_in_background=True
)

# 查询后台任务状态
subagent(read_task="subagent-1")

# 终止后台任务
subagent(kill_task="subagent-1")

# 列出所有后台任务（shell + subagent）
subagent(list_tasks=True)
```

**参数**：
- `task`: 任务目标描述
- `plan`: 执行计划（有序步骤列表）
- `max_iterations`: 最大迭代次数（默认 50，最大 100）
- `run_in_background`: 后台执行模式（立即返回 task_id）
- `read_task`: 读取后台 SubAgent 状态
- `kill_task`: 终止后台 SubAgent
- `list_tasks`: 列出所有后台任务

**返回值**：
```python
# 同步模式
{"status": "completed", "summary": "翻译完成", "iterations": 12}
# 或 {"status": "error", "message": "...", "iterations": 3}      # LLM 调用失败
# 或 {"status": "timeout", "iterations": 50}                      # 超过最大迭代次数
# 或 {"status": "stopped", "message": "Stopped by user", "iterations": 5}  # 用户手动停止
# 或 {"status": "failed", "message": "...", "iterations": 10}     # 连续工具调用失败

# 后台模式
"SubAgent started in background. Task ID: subagent-1"
```

**关键特性**：
- **独立工具集**：shared 工具 + sub 专属 skill
- **结构化任务**：task + plan 拼接到 system prompt 末尾（不可压缩）
- **专用 prompt**：`build_sub_prompt()` 动态构建
- **1 小时超时**
- **禁止嵌套**：SubAgent 不能再调用 subagent 工具
- **后台执行**：`run_in_background=true` 在独立线程中运行 SubAgent
- **懒加载 UI**：后台 SubAgent 完成后，`_subagent_ref` 状态更新到主会话，前端通过懒加载刷新

**会话消息结构**：
- 主会话只存 `_subagent_ref`（状态占位符，UI 可见，LLM 上下文自动过滤）
- 完整执行日志存入独立 `exec_*.json` 文件，每轮迭代实时保存

**后台 SubAgent 生命周期**：
1. `run_in_background=True` → 注册 task_id（格式：`subagent-N`）
2. 独立线程运行 `SubAgent.run()`，完成后自动更新 `_subagent_ref` 状态
3. `read_task` → 查询状态（running/completed）和摘要
4. `kill_task` → 设置 `subagent._stopped=True` 终止 SubAgent

**回调链路**：
```
web_api.py 注入 on_subagent_start 回调
  → RootAgent._on_subagent_start
    → SubAgentTool.on_subagent_start
      → 推送 SSE 事件
        → 前端渲染卡片
```

**实现**：`core/sub_agent.py` 中的 `SubAgent` 类，`core/tools/root/subagent_tool.py` 提供工具接口。

### 8.3 temp — 临时文件/目录管理

`temp` 工具用于管理当前 session 的临时文件和目录。临时数据存放在 `data/agents/{uuid}/.cili/tmp/{session_id}/`（或 `workspace/.cili/tmp/{session_id}/` 当 workspace_uuid 为空时）。

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
- **路径解析**：workspace_uuid 为空时 fallback 到项目根 `workspace/`

**实现**：`shared/temp.py`

---

## 九、添加新工具

### 9.1 创建工具文件

在 `core/tools/shared/`（或 root/sub/）创建新文件：

```python
# core/tools/shared/my_tool.py
from core.tools.shared.base import Tool, ToolResult

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

在对应的 `__init__.py` 中添加：

```python
# core/tools/shared/__init__.py
from core.tools.shared.my_tool import MyTool

def create_shared_tools(**kwargs) -> list[Tool]:
    return [
        # ... existing tools ...
        MyTool(**kwargs),
    ]
```

### 9.3 工具命名规范

- 使用 snake_case（如 `web_search`, `llm`）
- 名称应清晰表达工具功能
- 避免与现有工具重名

---

## 十、设计决策

### 10.1 为什么分三层（shared/root/sub）？

- **共用工具**：大部分工具 RootAgent 和 SubAgent 都需要（如 read/write/bash）
- **RootAgent 专属**：skill（扫描 root/ 目录的技能）、subagent（委派任务）
- **SubAgent 专属**：skill（扫描 sub/ 目录的技能，内容不同）
- **共用逻辑复用**：skill.py 和 python_tool.py 在 shared/，两边共用同一实现
- **条件加载**：llm 仅在配置了 llm_model 时加载，避免无谓的 API 调用

### 10.2 为什么 read/write/edit 用直接 I/O？

- **性能**：避免 subprocess 开销（spawn Python 进程）
- **简单**：直接 `open()` 读写，不需要解析输出
- **可靠**：无 shell 转义问题

### 10.3 为什么 bash 工具用 Git Bash？

- **Windows 兼容**：Windows 原生 cmd/PowerShell 语法不同
- **Unix 工具**：grep、sed、awk 等 Unix 工具在 Git Bash 中可用
- **一致性**：跨平台行为一致

### 10.4 为什么工具输出要截断？

- **上下文窗口限制**：工具输出太大会占用 LLM 上下文
- **成本控制**：减少 token 消耗
- **安全性**：防止恶意输出撑爆上下文

### 10.5 为什么使用外部优先存储架构？

- **Session 体积小**：只存元信息（`_file_size`、`_output_path` 等），不存内容
- **历史加载快**：页面刷新时不必加载大量工具输出
- **按需读取**：LLM 可根据需要重读完整内容
- **前后端解耦**：后端注入内容，前端无感知

---

## 十一、相关文件

| 文件 | 职责 |
|------|------|
| `core/tools/__init__.py` | 工具注册表（create_tools, get_tool_by_name） |
| `core/tools/shared/base.py` | Tool 基类 + ToolResult |
| `core/tools/shared/*.py` | 共用工具实现 |
| `core/tools/root/*.py` | RootAgent 专属工具 |
| `core/tools/sub/*.py` | SubAgent 专属工具 |
| `core/base_agent.py` | 工具执行循环（`_execute_tool`）+ `_resolve_tool_results()` |
| `core/root_agent.py` | RootAgent 流式交互 |
| `core/sub_agent.py` | SubAgent 非流式循环 |

---

**文档版本**: v1.4  
**创建时间**: 2026-08-25  
**更新时间**: 2026-09-01（新增 loop 工具：循环任务进度追踪，配合 cron 实现自循环任务）  
**状态**: 已实现
