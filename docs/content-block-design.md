# 内容块类型系统与 LLM 底层架构设计文档

## 概述

Cili Agent 的 LLM 底层采用 **Adapter 分层架构**，通过类型化的内容块（ContentBlock）和统一的消息格式（Message）实现 provider 无关的 LLM 交互。

## 架构总览

```
LLMClient (client.py)
  ├── Adapter (adapter.py) — 抽象基类
  │     ├── AnthropicAdapter (anthropic.py)
  │     └── OpenAIAdapter (openai.py)
  ├── HttpTransport (transport.py) — HTTP 请求、重试、SSE 解析
  └── BlockAssembler (assembler.py) — 流式数据块累积

核心类型 (types.py)
  ├── ContentBlock: TextBlock | ReasoningBlock | ToolCallBlock | ImageBlock | ToolResultBlock
  ├── Message — Provider 无关的统一消息格式
  ├── StreamChunk — 统一流式协议（8 种事件类型）
  ├── UsageData — Token 用量（provider 无关）
  └── LLMResponse — 非流式响应
```

### Adapter Sandwich 模式

```
发送：Message → Adapter.serialize() → wire format → HttpTransport → API
接收：API → HttpTransport → Adapter.translate_stream() → StreamChunk → BlockAssembler → ContentBlock
```

---

## 核心类型 (core/llm/types.py)

### ContentBlock 类型

```python
@dataclass TextBlock:
    text: str = ""

@dataclass ReasoningBlock:
    text: str = ""
    signature: str | None = None  # provider 私有回放元数据

@dataclass ToolCallBlock:
    id: str = ""
    name: str = ""
    arguments: str = ""           # 原始 JSON 字符串，延迟解析

    def parse_arguments(self) -> dict[str, Any]:
        """工具执行时调用，解析 JSON → dict"""

@dataclass ImageBlock:
    data: str = ""                # base64
    mime_type: str = ""

@dataclass ToolResultBlock:
    tool_use_id: str = ""
    content: str | list[dict] = ""    # str 纯文本，list[dict] 多模态（text + image blocks）
    is_error: bool = False
    # SubAgent 扩展字段
    exec_id: str = ""
    iterations: int = 0
    message_count: int = 0
    duration_seconds: float = 0
```

### Message

Provider 无关的统一消息格式。`content` 可以是字符串（纯文本用户消息）或 ContentBlock 列表（assistant 消息、工具结果等）。

```python
@dataclass Message:
    role: str                           # "user" | "assistant" | "tool"
    content: str | list[ContentBlock]

    # assistant 消息元数据
    provider: str = ""
    model: str = ""
    usage: UsageData | None = None
    stop_reason: str = ""

    # 压缩/有效性标记
    compacted: bool = False        # 工具结果已压缩
    invalidated: bool = False      # 消息无效，发送时排除
```

### UsageData

```python
@dataclass UsageData:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
```

### StreamChunk — 统一流式协议

```python
@dataclass StreamChunk:
    type: str  # chunk 类型
    index: int = 0  # 块索引
    data: dict[str, Any] = field(default_factory=dict)
```

8 种事件类型，adapter 负责将 provider SSE 翻译为此格式：

| type | data 字段 | 说明 |
|------|-----------|------|
| `block_start` | `block_type` | 新内容块开始 |
| `text_delta` | `text` | 文本内容增量 |
| `reasoning_delta` | `text` | 推理内容增量 |
| `signature_delta` | `signature` | 思考块签名（赋值替换，非累积；Anthropic 多轮重放） |
| `tool_call_delta` | `id?`, `name?`, `arguments?` | 工具调用增量 |
| `block_end` | — | 内容块结束 |
| `usage` | `usage: UsageData` | Token 用量更新 |
| `finish` | `stop_reason` | 流结束 |

### LLMResponse

```python
@dataclass LLMResponse:
    content: list[ContentBlock]
    stop_reason: str = ""
    usage: UsageData = UsageData()
    headers: dict[str, str] = field(default_factory=dict)

    def get_text(self) -> str
    def get_tool_calls(self) -> list[ToolCallBlock]
    def content_as_dicts(self) -> list[dict]  # 用于消息存储
```

---

## Adapter 层

### Adapter 抽象基类

```python
class Adapter(ABC):
    @property
    def api_path(self) -> str           # "/v1/messages" or "/v1/chat/completions"

    def build_headers(self) -> dict     # API 认证头
    def serialize(self, ...) -> dict    # Message → wire format
    def translate_stream(self, events)  # SSE events → StreamChunks
    def parse_response(self, data)      # wire format → (blocks, stop_reason, usage)
```

### AnthropicAdapter

- **API**: `/v1/messages`，`x-api-key` 认证
- **工具调用**: 存储为 `tool_use`，Python 字段 `arguments: str` ↔ API `input: dict`
- **推理内容**: `thinking` block with `signature`
- **扩展思考**: 非流式请求自动启用，`budget_tokens` 按 `reasoning_effort` 映射：low→1024, medium→4096, high→10000

### OpenAIAdapter

- **API**: `/v1/chat/completions`，`Bearer` 认证
- **工具调用**: 内部 `tool_call` ↔ API `function` calling（`arguments` 保持 str）
- **消息转换**: system 消息前置，tool_result → role=tool 消息
- **推理模型**: o1/o3/o4/o5/qwen3/qwq/deepseek-r1 自动设置 `reasoning_effort`

---

## 传输层 (core/llm/transport.py)

HttpTransport 负责 HTTP 请求、SSE 解析和重试：

- **重试**: 429/5xx 自动重试，最多 4 次，指数退避 + 抖动
- **Retry-After**: 尊重服务器返回的重试间隔
- **SSE 解析**: 逐行解析 `data:` 行，yield JSON events
- **中断支持**: `stop_check` 回调支持用户中断

---

## BlockAssembler (core/llm/assembler.py)

累积 StreamChunks 为 ContentBlocks：

```python
assembler = BlockAssembler()
for chunk in adapter.translate_stream(event_iterator):
    assembler.push(chunk)
# assembler.blocks → list[ContentBlock]
# assembler.stop_reason → str
# assembler.usage → UsageData
```

**关键**：`translate_stream()` 必须接收完整的事件迭代器（而非逐事件调用），以维护内部状态（tool_call_indices 等）跨事件持久化。

---

## 数据流

### 1. 流式请求

```
base_agent._call_llm_streaming()
  → LLMClient.chat_stream(messages=[Message], ...)
    → adapter.serialize(messages) → request body
    → transport.stream(url, headers, body) → event iterator
    → adapter.translate_stream(events) → StreamChunks
    → assembler.push(chunk) → ContentBlocks
  → LLMResponse(content=blocks, stop_reason, usage)
```

### 2. 工具调用处理

```python
# 1. 响应包含 ToolCallBlock（arguments 是原始 JSON 字符串）
response = self._call_llm(streaming=True, ...)

# 2. 存储 assistant 消息（转为 dict）
self.add_message("assistant", response.content_as_dicts())
# → {"type": "tool_use", "id": "...", "name": "...", "input": {...}}

# 3. 工具执行时解析 JSON
for block in response.get_tool_calls():
    input_data = block.parse_arguments()  # str → dict
    result = self._execute_tool(block.name, input_data, block.id)
```

### 3. 消息存储格式

消息存储为 dict 列表（JSON 可序列化）：

```json
{
  "role": "assistant",
  "content": [
    {"type": "text", "text": "..."},
    {"type": "tool_use", "id": "...", "name": "...", "input": {"key": "value"}}
  ]
}
```

发送 API 前，dict 消息通过 `Message.from_dict()` 转为 Message 对象，再由 adapter 转为 wire format。

---

## 序列化格式对照

### ToolCallBlock

| 层面 | 格式 |
|------|------|
| 内部类型 | `ToolCallBlock(id, name, arguments: str)` |
| 存储格式 | `{"type": "tool_use", "id", "name", "input": dict}` |
| Anthropic API | `{"type": "tool_use", "id", "name", "input": dict}` |
| OpenAI API | `{"type": "function", "function": {"name", "arguments": str}}` |

### ToolResultBlock

| 层面 | 格式 |
|------|------|
| 内部类型 | `ToolResultBlock(tool_use_id, content, is_error)` |
| 存储格式 | `{"type": "tool_result", "tool_use_id", "content", "is_error"}` |
| Anthropic API | 嵌在 user 消息中，`tool_use_id` |
| OpenAI API | 独立 `role: "tool"` 消息，`tool_call_id` |

---

## ToolResult (core/tools/shared/base.py)

工具执行结果，支持新接口（blocks）和兼容旧接口（output）：

```python
class ToolResult:
    def __init__(self, output="", error=False, content=None,
                 blocks=None, is_error=False, meta=None):
        # 新接口：blocks 是 ContentBlock 列表
        # 兼容接口：output → TextBlock

    @property
    def output(self) -> str:    # 从 blocks 提取文本
    @property
    def error(self) -> bool:    # alias for is_error
```

---

## 配置

双模型架构：

```json
{
  "model": { "name": "claude-sonnet-4-6", "interface_type": "anthropic", ... },
  "llm_model": { "name": "claude-haiku-4-5", "interface_type": "anthropic", ... }
}
```

- `model`: RootAgent 主对话模型
- `llm_model`: LLMTool 单轮处理模型（可选）

工厂函数：`create_llm_client(config) → LLMClient`

---

## 相关文件

| 文件 | 说明 |
|------|------|
| `core/llm/types.py` | ContentBlock, Message, StreamChunk, UsageData, LLMResponse |
| `core/llm/adapter.py` | Adapter 抽象基类 |
| `core/llm/anthropic.py` | AnthropicAdapter |
| `core/llm/openai.py` | OpenAIAdapter |
| `core/llm/transport.py` | HttpTransport（HTTP、重试、SSE） |
| `core/llm/assembler.py` | BlockAssembler |
| `core/llm/client.py` | LLMClient（公共 API） |
| `core/llm/__init__.py` | 导出、create_llm_client 工厂 |
| `core/base_agent.py` | BaseAgent（消息管理、工具执行、压缩） |
| `core/root_agent.py` | RootAgent（流式交互） |
| `core/sub_agent.py` | SubAgent（非流式委派） |
| `core/tools/shared/base.py` | Tool 基类、ToolResult |

## 参考

- Anthropic Messages API
- OpenAI Chat Completions API
- DeepSeek Harness ContentBlock / StreamChunk 设计
- Pi-main 消息类型设计
