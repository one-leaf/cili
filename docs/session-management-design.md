# Session 会话管理设计文档

本文档描述 Cili Agent 的会话管理系统（Session Manager），负责消息存储、持久化、上下文压缩和使用量追踪。

---

## 一、功能概述

SessionManager 独立于 LLM 客户端，专门管理对话数据。它是 RootAgent 和 Web API 之间的数据层，负责：

- **消息管理**：添加、获取、过滤消息
- **持久化**：自动保存到磁盘，支持恢复
- **有效消息过滤**：标记无效内容，API 请求前自动过滤
- **上下文压缩**：三层自动压缩，防止超出 token 限制
- **使用量追踪**：统计 token 消耗和 API 调用次数
- **SubAgent 日志**：管理子代理执行记录

---

## 二、存储设计

### 2.1 目录结构

每个工作区有独立的 sessions 目录，每个会话是一个子目录：

```
data/agents/{uuid}/
└── sessions/
    ├── a1b2c3d4/                        # 会话 1（8 位十六进制 ID）
    │   ├── index.json                   # 主会话数据
    │   ├── toolu_abc123.txt             # RootAgent 工具输出（实时流式写入）
    │   ├── toolu_def456.txt
    │   ├── exec_a1b2c3d4/               # SubAgent 1
    │   │   ├── index.json               # SubAgent 执行日志
    │   │   ├── toolu_ghi789.txt         # SubAgent 工具输出
    │   │   └── toolu_jkl012.txt
    │   └── exec_e5f6g7h8/               # SubAgent 2
    │       └── index.json
    ├── e5f6g7h8/                    # 会话 2
    │   └── index.json
    └── ...
```

**文件命名规则**：
- **会话目录**：8 位十六进制短 ID（如 `a1b2c3d4`），由 `secrets.token_hex(4)` 生成
- **主会话文件**：固定为 `index.json`
- **工具输出文件**：`{tool_use_id}.txt`，与 `index.json` 同级（RootAgent）或在 SubAgent 子目录内
- **SubAgent 目录**：`exec_{id}/`，内含 `index.json` 和该 SubAgent 调用的所有工具输出文件

### 2.2 index.json 格式

主会话文件包含完整的对话数据：

```json
{
  "session_id": "a1b2c3d4",
  "name": "Python 异步编程讨论",
  "messages": [
    {
      "role": "user",
      "content": "帮我写一个异步爬虫"
    },
    {
      "role": "assistant",
      "content": [
        {
          "type": "thinking",
          "thinking": "用户需要异步爬虫...",
          "_valid": true
        },
        {
          "type": "text",
          "text": "好的，我来帮你写一个异步爬虫...",
          "_valid": true
        },
        {
          "type": "tool_use",
          "id": "toolu_123",
          "name": "write",
          "input": {"file_path": "crawler.py", "content": "..."},
          "_valid": true
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "tool_result",
          "tool_use_id": "toolu_123",
          "tool_name": "write",
          "_file_size": 24,
          "_truncated": false,
          "_compacted": false,
          "_output_path": "toolu_123.txt",
          "_is_error": false
        }
      ]
    },
    {
      "role": "assistant",
      "content": [
        {
          "type": "tool_use",
          "id": "toolu_sub_001",
          "name": "subagent",
          "input": {"task": "优化爬虫性能"},
          "_valid": true
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "tool_result",
          "tool_use_id": "toolu_sub_001",
          "content": "{\"status\": \"completed\", \"summary\": \"...\"}",
          "_meta": {
            "tool_name": "subagent",
            "exec_id": "exec_a1b2c3d4",
            "completed": true
          },
          "_valid": true
        }
      ]
    }
  ],
  "metadata": {
    "created_at": "2026-08-25 10:00:00",
    "updated_at": "2026-08-25 10:32:00",
    "usage": {
      "input_tokens": 15000,
      "output_tokens": 3000,
      "api_calls": 8,
      "cache_read_tokens": 12000,
      "cache_creation_tokens": 5000
    },
    "subagent_count": 1
  }
}
```

**关键字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 8 位十六进制 ID |
| `name` | string | 会话名称（可重命名） |
| `messages` | array | 消息列表（user/assistant） |
| `messages[].role` | string | 消息角色（user/assistant） |
| `messages[].content` | string/array | 消息内容（字符串或 content blocks） |
| `messages[]._valid` | bool | 是否有效（false 表示不发送给 API） |
| `tool_result.tool_name` | string | 工具名称（如 bash、read、write） |
| `tool_result._file_size` | int | 外部输出文件的字节数 |
| `tool_result._truncated` | bool | 输出是否被截断（>30K 字符） |
| `tool_result._compacted` | bool | 是否被 microcompact 压缩过 |
| `tool_result._output_path` | string | 外部输出文件相对路径（如 toolu_123.txt） |
| `tool_result._is_error` | bool | 工具执行是否失败 |
| `metadata.created_at` | string | 创建时间（yyyy-MM-dd HH:mm:ss） |
| `metadata.updated_at` | string | 最后更新时间 |
| `metadata.usage` | object | 使用量统计 |
| `metadata.subagent_count` | int | SubAgent 执行次数 |

**外部优先存储**：工具结果的内容不保存在 session 中，只保存元信息。实际内容保存在外部文件 `{tool_use_id}.txt`，发送 LLM 时按需读取。

### 2.3 SubAgent 执行日志

SubAgent 执行日志独立存储在子目录中：

**存储路径**：`{session_dir}/exec_{id}/index.json`

```json
{
  "exec_id": "exec_a1b2c3d4",
  "session_id": "a1b2c3d4",
  "task": "优化爬虫性能，使用连接池和并发控制",
  "summary": "已完成性能优化，添加了连接池限制和并发控制",
  "metadata": {
    "started_at": "2026-08-25 10:30:00",
    "ended_at": "2026-08-25 10:32:00",
    "duration_seconds": 120,
    "status": "completed",
    "iterations": 5,
    "max_iterations": 50
  },
  "messages": [
    // SubAgent 的完整消息历史
  ]
}
```

### 2.4 工具输出文件（外部优先存储）

工具执行时，完整输出实时写入外部 `.txt` 文件，供前端轮询实时显示，也作为 LLM 获取内容的唯一来源。

**存储路径**：
- RootAgent 工具：`{session_dir}/{tool_use_id}.txt`
- SubAgent 工具：`{session_dir}/{exec_dir}/{tool_use_id}.txt`

**工作机制**：
1. 工具执行前，Agent 设置 `tool.output_file = {路径}`
2. `_run_bash()` 逐行读取子进程输出，同时 append 写入 output_file（每行 flush）
3. 前端通过 stream API 轮询文件新增内容，实现实时显示
4. **Session 中只保存元信息**（`_file_size`、`_truncated`、`_output_path` 等），不保存内容
5. 发送 LLM 前，`_resolve_tool_results()` 从外部文件按需读取内容注入消息中
6. 处理三种情况：
   - 正常输出：直接读取文件内容
   - 截断输出（>30K 字符）：读取后截断 + 引导语
   - 压缩输出（`_compacted=True`）：注入占位符

**文件生命周期**：随会话删除自动清理（`shutil.rmtree`）。

**设计原因**：
- Session 文件体积小（只存元信息，不存内容）
- 前端实时看到命令进度，不用等到命令完成
- LLM 上下文不被大输出撑爆，同时保留完整数据可追溯
- 按 `tool_use_id` 隔离文件，天然支持并发工具执行

---

## 三、核心类

### 3.1 SessionManager

会话管理器，每个会话对应一个实例。

```python
class SessionManager:
    session_id: str                 # 8 位十六进制 ID
    sessions_dir: Path              # 工作区级 sessions 目录
    session_dir: Path               # 当前会话目录
    messages: list[dict]            # 消息列表
    metadata: dict                  # 元数据（时间、使用量等）
    name: str                       # 会话名称
    _valid_messages_cache: list[dict] | None  # 有效消息缓存
    _messages_dirty: bool           # 脏标记（消息变更后设为 True）
```

**主要方法**：

| 方法 | 说明 |
|------|------|
| `add_message(role, content, *, flush=True, extra=None)` | 添加消息（flush 保留兼容，不会自动保存） |
| `get_messages()` | 获取所有消息 |
| `get_valid_messages()` | 获取有效消息（过滤 _valid=False，支持缓存） |
| `clear()` | 清空所有消息 |
| `get_last_n_messages(n)` | 获取最后 N 条消息 |
| `get_message_count()` | 获取消息数量 |
| `update_tool_result(tool_use_id, updates)` | 更新指定 tool_use_id 的 tool_result 字段 |
| `save()` | 持久化到 index.json |
| `load()` | 从 index.json 加载 |
| `delete()` | 删除会话目录 |
| `list_sessions(sessions_dir)` | 列出所有会话（静态方法） |
| `rename(new_name)` | 重命名会话 |
| `update_usage(...)` | 更新使用量统计 |
| `get_usage()` | 获取使用量统计 |
| `microcompact_tool_results(keep_recent)` | Microcompact 压缩（替换内容为占位符） |
| `mark_old_tool_calls_invalid(keep_recent_rounds)` | 标记旧工具调用为无效 |
| `mark_old_images_invalid(keep_recent)` | 标记旧图片为无效 |
| `save_subagent_log(...)` | 保存 SubAgent 执行日志 |
| `load_subagent_log(exec_id)` | 加载 SubAgent 执行日志 |
| `list_subagent_logs()` | 列出所有执行日志 |
| `delete_subagent_log(exec_id)` | 删除执行日志 |
| `to_dict()` | 转换为字典（用于 API 返回） |

**有效消息缓存机制**：

`get_valid_messages()` 使用脏标记缓存模式，避免重复过滤：

```python
# 内部状态
_valid_messages_cache: list[dict] | None = None
_messages_dirty: bool = True

# 获取时检查缓存
def get_valid_messages(self) -> list[dict]:
    if not self._messages_dirty and self._valid_messages_cache is not None:
        return self._valid_messages_cache
    # ... 执行过滤 ...
    self._valid_messages_cache = result
    self._messages_dirty = False
    return result

# 消息变更时标记为脏
def add_message(self, ...):
    self.messages.append(message)
    self._messages_dirty = True
```

**优势**：
- 多轮工具执行中只过滤一次
- 消息变更自动失效缓存
- 减少重复计算开销

### 3.2 静态工厂方法

```python
# 创建新会话
session = SessionManager.create_new_session(sessions_dir, name="新会话")

# 加载已存在的会话
session = SessionManager.load_session(session_id, sessions_dir)

# 列出所有会话
sessions = SessionManager.list_sessions(sessions_dir)
# 返回: [{"session_id": "...", "name": "...", "created_at": "...", "updated_at": "...", "usage": {...}, "subagent_count": 0, ...}]
# 注意：metadata 中的所有字段会被展开到顶层（**metadata）
```

---

## 四、消息管理

### 4.1 添加消息

**自动处理**：
- 为 content blocks 添加 `_valid=True`（如果未设置）
- 更新 `metadata.updated_at`

### 4.2 获取消息

```python
# 获取所有消息（包括无效的）
all_messages = session.get_messages()

# 获取有效消息（用于 API 请求）
valid_messages = session.get_valid_messages()
```

### 4.3 有效消息过滤

`get_valid_messages()` 递归过滤无效内容：

**过滤规则**：
1. 跳过整条消息标记 `_valid=False` 的
2. 跳过 content blocks 中 `_valid=False` 的
3. 对 `tool_result` 递归过滤子块
4. 清理内部字段（`_valid`, `_compacted`）

**示例**：

```python
# 原始消息
messages = [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "你好！", "_valid": True},
        {"type": "image", "source": {...}, "_valid": False}  # 被标记无效
    ]},
]

# 过滤后
valid_messages = [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "你好！"}  # _valid 字段已清理
    ]}
]
```

---

## 五、SubAgent 执行日志

SubAgent 在主会话中以 tool_use + tool_result 消息对的形式呈现，与主 Agent 调用 SubAgent 的格式完全一致。

### 5.1 主会话消息格式

主会话中通过 assistant(tool_use) + user(tool_result) 表示 SubAgent 执行：

```python
# assistant 消息：模拟 LLM 调用 subagent
{
    "role": "assistant",
    "content": [
        {
            "type": "tool_use",
            "id": "toolu_sub_001",
            "name": "subagent",
            "input": {"task": "优化爬虫性能"}
        }
    ]
}

# user 消息：SubAgent 结果（含 exec_id，UI 据此渲染卡片）
{
    "role": "user",
    "content": [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_sub_001",
            "content": "{\"status\": \"completed\", ...}",
            "_meta": {
                "tool_name": "subagent",
                "exec_id": "exec_a1b2c3d4",
                "completed": true
            }
        }
    ]
}
```

### 5.2 保存完整日志

SubAgent 执行完成后保存完整日志到独立文件：

```python
session.save_subagent_log(
    exec_id="exec_a1b2c3d4",
    task="优化爬虫性能",
    messages=subagent_messages,
    metadata={
        "started_at": "2026-08-25 10:30:00",
        "ended_at": "2026-08-25 10:32:00",
        "duration_seconds": 120,
        "status": "completed",
        "iterations": 5,
        "max_iterations": 50
    },
    summary="已完成性能优化"
)
```

### 5.4 查询日志

```python
# 列出所有执行（仅元数据）
logs = session.list_subagent_logs()
# 返回: [{"exec_id": "...", "task": "...", "summary": "...", "metadata": {...}}]

# 加载完整日志
log = session.load_subagent_log("exec_a1b2c3d4")
# 返回: {"exec_id": "...", "task": "...", "messages": [...], ...}

# 删除日志
session.delete_subagent_log("exec_a1b2c3d4")
```

---

## 六、持久化机制

### 6.1 原子写入

所有文件写入都使用原子写入，防止数据损坏：

```python
def save(self) -> None:
    session_file = self.session_dir / "index.json"
    
    # 1. 写入临时文件
    temp_file = session_file.with_suffix(".json.tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    # 2. 原子替换（Windows 上也安全）
    temp_file.replace(session_file)
```

**优势**：
- 写入中断不会损坏原文件
- 并发读取不会看到半写入状态
- 失败时自动清理临时文件

### 6.2 加载时机

SessionManager 不会自动保存，需要显式调用 `save()`：

```python
# 添加消息
session.add_message("user", "你好")

# 显式保存
session.save()
```

**RootAgent 集成**：
- 每轮 LLM 调用后自动保存
- SubAgent 执行日志实时保存（每轮迭代后）

### 6.3 加载会话

```python
session = SessionManager.load_session(session_id, sessions_dir)
if session is None:
    print("会话不存在")
```

---

## 七、使用量追踪

### 7.1 更新使用量

每次 LLM API 调用后更新统计：

```python
session.update_usage(
    input_tokens=1500,
    output_tokens=300,
    api_calls=1,
    cache_read_tokens=1000,
    cache_creation_tokens=500
)
```

### 7.2 获取使用量

```python
usage = session.get_usage()
# 返回: {
#     "input_tokens": 15000,
#     "output_tokens": 3000,
#     "api_calls": 8,
#     "cache_read_tokens": 12000,
#     "cache_creation_tokens": 5000
# }
```

### 7.3 使用量字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `input_tokens` | int | 输入 token 总数 |
| `output_tokens` | int | 输出 token 总数 |
| `api_calls` | int | API 调用次数 |
| `cache_read_tokens` | int | 缓存读取 token 数 |
| `cache_creation_tokens` | int | 缓存创建 token 数 |

---

## 八、自动压缩机制

三层压缩机制，防止超出 token 限制：

### 8.0 压缩架构

压缩逻辑分为两层：
- **`core/compression.py`**：独立压缩模块，提供共享压缩函数（SubAgent 直接使用）
- **`core/session.py`**：SessionManager 内置压缩方法（RootAgent 使用，额外管理脏标记）

| 压缩函数 | 位置 | 使用者 |
|---------|------|--------|
| `microcompact_tool_results()` | compression.py | 两者共用（通过 BaseAgent） |
| `count_tokens_approx()` | compression.py | 两者共用 |
| `count_messages_tokens()` | compression.py | 两者共用 |
| `compact_messages_with_summary()` | compression.py | 两者共用 |
| `mark_old_tool_calls_invalid()` | base_agent.py | RootAgent 专属 |
| `mark_old_images_invalid()` | base_agent.py | RootAgent 专属 |

**注意**：`session.py` 中也有 `microcompact_tool_results()` 方法，但目前未被使用。RootAgent 和 SubAgent 都通过 `BaseAgent._check_and_compress()` 调用 `compression.py` 的版本，行为完全一致。

### 8.1 第一层：Microcompact（轻量压缩）

**触发时机**：每轮 LLM 调用前自动运行

**实现位置**：`core/compression.py` 的 `microcompact_tool_results()`

**压缩策略**：
- 保留最近 6 条工具结果消息
- 更早的工具结果仅设置 `_compacted = True` 标记
- 工具结果内容已保存在外部文件（`{tool_use_id}.txt`）
- 发送 LLM 时由 `_resolve_tool_results()` 检测 `_compacted=True` 后注入占位符

```python
from core.compression import microcompact_tool_results
saved_bytes = microcompact_tool_results(messages, keep_recent=6)
```

**示例**：

```python
# 压缩前：Session 中存储的元信息
{
    "type": "tool_result",
    "tool_use_id": "toolu_123",
    "tool_name": "bash",
    "_file_size": 10240,
    "_truncated": false,
    "_compacted": false,
    "_output_path": "toolu_123.txt"
}

# 压缩后：仅设置标记，不修改内容
{
    "type": "tool_result",
    "tool_use_id": "toolu_123",
    "tool_name": "bash",
    "_file_size": 10240,
    "_truncated": false,
    "_compacted": true,  # 仅添加此标记
    "_output_path": "toolu_123.txt"
}

# 发送 LLM 时：_resolve_tool_results() 检测到 _compacted=True
# 自动将 content 替换为占位符 "[旧工具结果已压缩，内容可从外部文件读取]"
```

**优势**：
- 不调用 LLM，速度快
- 原始内容保留在外部文件，LLM 可通过 `read` 工具按需重读
- RootAgent 和 SubAgent 行为完全一致，工具结果都保存在外部文件

### 8.2 第二层：Full Auto Compact（完整压缩）

**触发时机**：token 数 > max_context_tokens × 0.80

**压缩策略**：
- 调用 LLM 生成结构化摘要
- 用摘要替换早期对话
- 保留最后 6 条消息

```python
# 在 compression.py compact_messages_with_summary() 中实现
def compact_messages_with_summary(messages, llm_client, keep_recent_count=6):
    # 1. 分离要压缩和要保留的消息
    to_compact = messages[:-keep_recent_count]
    to_keep = messages[-keep_recent_count:]
    
    # 2. 调用 LLM 生成摘要
    summary = summarize_messages_for_compact(to_compact, llm_client)
    
    # 3. 替换早期消息为摘要
    messages.clear()
    messages.append({"role": "user", "content": f"[上下文压缩] 以下是之前对话的摘要：\n\n{summary}"})
    messages.append({"role": "assistant", "content": f"好的，我已经理解了之前的对话内容..."})
    messages.extend(to_keep)
```

**优势**：
- 大幅减少上下文（可能减少 50%+）
- 保留关键信息（摘要）
- 保留最近对话（最后 3 条）

### 8.3 第三层：紧急 Body Size 压缩

**触发时机**：请求体大小接近 3MB（代理限制）

**压缩策略**：

#### 标记旧工具调用为无效

```python
saved_bytes = session.mark_old_tool_calls_invalid(keep_recent_rounds=5)
```

- 保留最近 5 轮（5 个 assistant 消息）的工具调用
- 更早的标记为 `_valid=False`
- 轮次按 assistant 消息计数

#### 标记旧图片为无效

```python
saved_bytes = session.mark_old_images_invalid(keep_recent=5)
```

- 保留最近 5 条消息中的图片
- 更早的图片标记为 `_valid=False`
- 图片数据很大，优先压缩

**效果**：
- 标记为 `_valid=False` 的内容仍保留在消息列表中
- `get_valid_messages()` 会自动过滤掉这些内容
- UI 仍可显示历史记录，但 API 请求不包含

---

## 九、_valid 字段机制

### 9.1 设计目的

`_valid` 字段用于标记内容是否应该发送给 API，但不从消息列表中删除：

- **UI 可见**：用户可以在 Web UI 中看到完整历史
- **API 不发送**：`get_valid_messages()` 自动过滤
- **可恢复**：理论上可以重新标记为有效

### 9.2 使用场景

| 场景 | 标记方式 |
|------|---------|
| Microcompact 压缩 | 替换为占位符，原始内容可从外部文件读取 |
| 紧急压缩（工具调用） | 标记 `_valid=False` |
| 紧急压缩（图片） | 标记 `_valid=False` |
| 413 错误重试 | 标记所有图片为 `_valid=False` |

### 9.3 过滤逻辑

`get_valid_messages()` 的过滤流程：

```
遍历所有消息
│
├─ 消息级别 _valid=False → 跳过整条消息
│
└─ 遍历 content blocks
    │
    ├─ block._valid=False → 跳过该 block
    │
    └─ 对 tool_result 递归过滤子块
        └─ 子块._valid=False → 跳过该子块
```

---

## 十、与 RootAgent 集成

### 10.1 RootAgent 使用 SessionManager

```python
class RootAgent:
    def __init__(self, session_id, sessions_dir, ...):
        self.session_manager = SessionManager(session_id, sessions_dir)
        self.client = create_llm_client(config)
    
    def run(self, user_input):
        # 1. 添加用户消息
        self.session_manager.add_message("user", user_input)
        
        # 2. 自动压缩
        self._auto_compact()
        
        # 3. 调用 LLM（注入工具结果内容）
        messages = self.session_manager.get_valid_messages()
        messages = self._resolve_tool_results(messages)  # 从外部文件按需读取内容
        response = self.client.chat_stream(messages, ...)
        
        # 4. 添加助手消息
        self.session_manager.add_message("assistant", response.content)
        
        # 5. 执行工具（只存元信息到 session）
        for tool_use in response.tool_uses:
            result = self._execute_tool(tool_use)  # 返回 tool_result 元信息
            self.session_manager.add_message("user", [result])
        
        # 6. 保存会话
        self.session_manager.save()
```

### 10.2 Web API 使用

```python
# web/web_api.py

# 获取或创建 RootAgent
agent = agents.get(workspace_uuid, session_id)
if agent is None:
    sessions_dir = workspace_dir / "sessions"
    session = SessionManager.load_session(session_id, sessions_dir)
    agent = RootAgent(session_id, sessions_dir, ...)
    agents[key] = agent

# 发送消息
async def send_message():
    agent.run(user_input)
    # RootAgent 内部会自动保存
    
# 获取会话列表
@app.get("/api/workspaces/{uuid}/sessions")
def list_sessions(uuid):
    sessions_dir = workspace_dir / "sessions"
    return SessionManager.list_sessions(sessions_dir)

# 获取会话详情
@app.get("/api/workspaces/{uuid}/sessions/{id}")
def get_session(uuid, id):
    sessions_dir = workspace_dir / "sessions"
    session = SessionManager.load_session(id, sessions_dir)
    return session.to_dict()
```

---

## 十一、设计决策

### 11.1 为什么每个会话一个目录？

- **文件隔离**：主会话和 SubAgent 日志分开，互不干扰
- **易于管理**：删除会话只需删除目录
- **扩展性**：未来可以添加更多文件（如附件、缓存）

### 11.2 为什么使用 8 位十六进制 ID？

- **可读性**：比 UUID 短，便于调试
- **唯一性**：16^8 = 4.29 亿种组合，足够使用
- **安全性**：使用 `secrets.token_hex()`，不可预测

### 11.3 为什么 SubAgent 日志独立存储？

- **减少主文件大小**：主会话只存引用，不存完整日志
- **懒加载**：前端展开卡片时才加载完整日志
- **实时保存**：SubAgent 每轮迭代后保存，不会丢失

### 11.4 为什么 SubAgent 使用子目录而非扁平文件？

- **工具输出隔离**：SubAgent 的 `{tool_use_id}.txt` 和 `index.json` 放在同一目录，便于管理和清理
- **避免命名冲突**：SubAgent 和 RootAgent 的工具输出可能使用相同的 `tool_use_id` 格式，子目录天然隔离
- **完整性**：删除 SubAgent 时整个目录一起删除，不会遗留孤立文件

### 11.5 为什么工具输出使用外部文件？

- **实时显示**：前端轮询文件获取新内容，命令执行期间即可看到进度
- **上下文保护**：大输出截断后给 LLM，完整内容保存在文件中供按需读取
- **可追溯**：LLM 可以用 `read` 工具分批读取完整输出
- **并发安全**：按 `tool_use_id` 隔离文件，多个工具同时执行互不干扰

### 11.6 为什么需要三层压缩？

- **渐进式**：先轻量压缩，不够再完整压缩
- **性能**：Microcompact 不调用 LLM，速度快
- **可靠性**：紧急压缩防止 413 错误

---

## 十二、相关文件

| 文件 | 职责 |
|------|------|
| `core/session.py` | SessionManager 实现（含 RootAgent 专用压缩方法） |
| `core/compression.py` | 独立压缩模块（共享压缩函数，SubAgent 使用） |
| `core/root_agent.py` | 使用 SessionManager 管理对话 |
| `core/sub_agent.py` | 使用 compression.py 管理压缩 |
| `web/web_api.py` | 提供会话管理 API |
| `web/static/app.js` | 前端会话列表和消息展示 |

---

## 十三、API 端点

### 13.1 会话管理

```
GET  /api/workspaces/{uuid}/sessions
POST /api/workspaces/{uuid}/sessions
GET  /api/workspaces/{uuid}/sessions/{id}        # 返回前自动注入工具结果内容
DELETE /api/workspaces/{uuid}/sessions/{id}
POST /api/workspaces/{uuid}/sessions/{id}/rename
```

### 13.2 消息发送

```
POST /api/workspaces/{uuid}/sessions/{id}/messages
```

SSE 流式返回助手响应。

### 13.3 SubAgent 执行

```
GET /api/workspaces/{uuid}/sessions/{id}/executions
GET /api/workspaces/{uuid}/sessions/{id}/executions/{exec_id}  # 返回前自动注入工具结果内容
DELETE /api/workspaces/{uuid}/sessions/{id}/executions/{exec_id}
```

### 13.4 工具输出实时流

```
GET /api/workspaces/{uuid}/sessions/{id}/stream/{tool_use_id}?offset=0
```

返回 JSON：`{content: "新内容", offset: 新字节位置, exists: true/false}`

前端在 `tool_use` SSE 事件时启动 500ms 轮询，`tool_result` 事件时停止。`offset` 参数实现增量读取，避免重复传输已显示内容。

**外部优先架构说明**：
- Session API 返回前，后端调用 `_resolve_tool_results_for_session()` 从外部文件读取内容注入消息
- 前端无感知，直接从返回的消息中渲染工具结果
- SubAgent 执行日志 API 同样在返回前注入内容

---

**文档版本**: v1.1  
**创建时间**: 2026-08-25  
**更新时间**: 2026-08-28（外部优先存储架构重构）  
**状态**: 已实现
