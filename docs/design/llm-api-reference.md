# LLM API 接口参考

本文档详细记录 Anthropic Messages API、OpenAI Chat Completions API 和 OpenAI Responses API 的原始接口结构，供开发参考。

---

## 目录

- [一、Anthropic Messages API](#一anthropic-messages-api)
  - [1.1 请求格式](#11-请求格式)
  - [1.2 非流式响应](#12-非流式响应)
  - [1.3 流式响应 (SSE)](#13-流式响应-sse)
- [二、OpenAI Chat Completions API](#二openai-chat-completions-api)
  - [2.1 请求格式](#21-请求格式)
  - [2.2 非流式响应](#22-非流式响应)
  - [2.3 流式响应 (SSE)](#23-流式响应-sse)
- [三、OpenAI Responses API](#三openai-responses-api)
  - [3.1 请求格式](#31-请求格式)
  - [3.2 非流式响应](#32-非流式响应)
  - [3.3 流式响应 (SSE)](#33-流式响应-sse)
- [四、内部统一格式](#四内部统一格式)

---

## 一、Anthropic Messages API

### 1.1 请求格式

**端点**: `POST /v1/messages`

**请求头**:
```http
Content-Type: application/json
x-api-key: sk-ant-...
anthropic-version: 2023-06-01
```

**请求体**:
```jsonc
{
  // 必需字段
  "model": "claude-opus-4-6",           // 模型名称
  "max_tokens": 16384,                   // 最大输出 token 数
  "messages": [                          // 对话消息列表
    {
      "role": "user",                    // "user" | "assistant"
      "content": "你好"                   // string | ContentBlock[]
    }
  ],

  // 可选字段
  "system": "你是一个助手...",            // 系统提示词
  "temperature": 0.2,                    // 温度 (0.0-1.0)，与 thinking 互斥
  "stream": false,                       // 是否流式响应

  // Thinking 配置（与 temperature 互斥）
  "thinking": {
    "type": "enabled",                   // "enabled" | "disabled"
    "budget_tokens": 4096                // thinking 最大 token 数，必须 < max_tokens
  },
  // 或使用 effort 级别（Cili 内部通过 reasoning_effort 映射）：
  // low → budget_tokens=1024, medium → 4096, high → 10000
  // reasoning="off" → 显式 {"type": "disabled"}（部分网关对「不传 thinking」的默认
  // 是开思考，必须显式关；实测单次判定 ~27s → 关掉后 ~6s）

  // 工具定义
  "tools": [
    {
      "name": "read_file",               // 工具名称
      "description": "读取文件内容",      // 工具描述
      "input_schema": {                  // JSON Schema
        "type": "object",
        "properties": {
          "file_path": {
            "type": "string",
            "description": "文件路径"
          }
        },
        "required": ["file_path"]
      }
    }
  ],

  // LiteLLM 会话标识：以 HTTP 头发送（x-litellm-session-id），非请求体字段
}
```

**LiteLLM 会话标识**：会话 id 不走请求体，而是以 HTTP 头发送——`backend: "litellm"` 时
cili 自动附加 `x-litellm-session-id: <master 的 session id>`（sub agent 透传同一 id），
供网关按会话聚合同一会话的全部请求；无会话时整条省略。

**消息格式示例**:

```jsonc
// 用户消息（纯文本）
{"role": "user", "content": "你好"}

// 用户消息（多模态）
{"role": "user", "content": [
  {"type": "text", "text": "这张图片是什么？"},
  {"type": "image", "source": {
    "type": "base64",
    "media_type": "image/png",
    "data": "..."
  }}
]}

// 助手消息（文本 + 工具调用）
{"role": "assistant", "content": [
  {"type": "thinking", "thinking": "让我分析一下..."},
  {"type": "text", "text": "我来读取文件"},
  {"type": "tool_use", "id": "toolu_abc123", "name": "read_file", "input": {"file_path": "/tmp/test.txt"}}
]}

// 用户消息（工具结果）
{"role": "user", "content": [
  {"type": "tool_result", "tool_use_id": "toolu_abc123", "content": "文件内容..."}
]}
```

### 1.2 非流式响应

**响应体**:
```jsonc
{
  "id": "msg_01XFDUDYJgAACzvnptvVoYEL",  // 消息 ID
  "type": "message",                      // 固定值 "message"
  "role": "assistant",                    // 固定值 "assistant"
  "content": [                            // 内容块列表
    {
      "type": "thinking",                 // thinking 块
      "thinking": "让我思考一下这个问题..." // thinking 内容
    },
    {
      "type": "text",                     // 文本块
      "text": "这是回复内容"
    },
    {
      "type": "tool_use",                 // 工具调用块
      "id": "toolu_01ABCxyz",            // 工具调用 ID
      "name": "read_file",               // 工具名称
      "input": {"file_path": "/tmp/test.txt"}  // 工具参数
    }
  ],
  "model": "claude-opus-4-6",            // 实际使用的模型
  "stop_reason": "end_turn",             // 停止原因
  "stop_sequence": null,                 // 停止序列（如配置）
  "usage": {
    "input_tokens": 100,                 // 输入 token 数
    "output_tokens": 50,                 // 输出 token 数（含 thinking）
    "cache_read_input_tokens": 0,        // 缓存读取 token 数
    "cache_creation_input_tokens": 0     // 缓存创建 token 数
  }
}
```

**stop_reason 值**:
| 值 | 说明 |
|---|------|
| `end_turn` | 自然结束 |
| `tool_use` | 需要执行工具 |
| `max_tokens` | 达到最大 token 限制 |
| `stop_sequence` | 命中停止序列 |

### 1.3 流式响应 (SSE)

**请求**: 添加 `"stream": true`

**响应**: `Content-Type: text/event-stream`

**事件序列**:
```
event: message_start
data: {"type":"message_start","message":{"id":"msg_...","type":"message","role":"assistant","content":[],"model":"claude-opus-4-6","stop_reason":null,"usage":{"input_tokens":100,"output_tokens":1}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"让我"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"思考一下"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"这是"}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"回复"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

event: content_block_start
data: {"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"toolu_...","name":"read_file","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\"file_"}}

event: content_block_delta
data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"path\":\"/tmp\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":2}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":50}}

event: message_stop
data: {"type":"message_stop"}
```

**SSE 事件类型**:
| 事件 | 说明 | 关键字段 |
|------|------|----------|
| `message_start` | 消息开始 | `message.usage`, `message.id` |
| `content_block_start` | 内容块开始 | `index`, `content_block.type` |
| `content_block_delta` | 内容增量 | `delta.type`: `text_delta` / `thinking_delta` / `input_json_delta` |
| `content_block_stop` | 内容块结束 | `index` |
| `message_delta` | 消息结束增量 | `delta.stop_reason`, `usage.output_tokens` |
| `message_stop` | 消息结束 | 无 |

---

## 二、OpenAI Chat Completions API

### 2.1 请求格式

**端点**: `POST /v1/chat/completions`

**请求头**:
```http
Content-Type: application/json
Authorization: Bearer sk-...
```

**请求体**:
```jsonc
{
  // 必需字段
  "model": "gpt-4o",                     // 模型名称
  "messages": [                           // 对话消息列表
    {
      "role": "user",                    // "system" | "user" | "assistant" | "tool"
      "content": "你好"                   // string
    }
  ],

  // 可选字段
  "max_tokens": 16384,                   // 最大输出 token 数
  "temperature": 0.7,                    // 温度 (0.0-2.0)
  "stream": false,                       // 是否流式响应
  "stream_options": {"include_usage": true},  // 流式响应包含 usage

  // Reasoning 配置（仅推理模型：o1, o3, o4-mini, o5, qwen3, qwq, deepseek-r1 等）
  "reasoning_effort": "medium",          // "low" | "medium" | "high"
  // 注意：推理模型不支持 temperature

  // 工具定义
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "read_file",
        "description": "读取文件内容",
        "parameters": {                  // JSON Schema
          "type": "object",
          "properties": {
            "file_path": {
              "type": "string",
              "description": "文件路径"
            }
          },
          "required": ["file_path"]
        }
      }
    }
  ],

  // LiteLLM 会话标识：以 HTTP 头发送（x-litellm-session-id），非请求体字段
}
```

**LiteLLM 会话标识**：会话 id 不走请求体，而是以 HTTP 头发送——`backend: "litellm"` 时
cili 自动附加 `x-litellm-session-id: <master 的 session id>`（sub agent 透传同一 id），
供网关按会话聚合同一会话的全部请求；无会话时整条省略。

**消息格式示例**:

```jsonc
// 系统消息
{"role": "system", "content": "你是一个助手..."}

// 用户消息
{"role": "user", "content": "你好"}

// 助手消息（纯文本）
{"role": "assistant", "content": "你好！有什么可以帮助你的？"}

// 助手消息（带工具调用）
{
  "role": "assistant",
  "content": "我来读取文件",              // 可以为 null
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "read_file",
        "arguments": "{\"file_path\": \"/tmp/test.txt\"}"  // JSON 字符串
      }
    }
  ]
}

// 工具结果消息（每个工具调用一条）
{
  "role": "tool",
  "tool_call_id": "call_abc123",         // 对应 tool_calls 中的 id
  "content": "文件内容..."
}
```

### 2.2 非流式响应

**响应体**:
```jsonc
{
  "id": "chatcmpl-abc123",               // 响应 ID
  "object": "chat.completion",           // 固定值
  "created": 1234567890,                 // Unix 时间戳
  "model": "gpt-4o",                     // 实际使用的模型
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "这是回复内容",         // 文本内容（可能为 null）
        "reasoning_content": "让我思考...", // 推理内容（推理模型）
        "tool_calls": [                   // 工具调用列表
          {
            "id": "call_abc123",
            "type": "function",
            "function": {
              "name": "read_file",
              "arguments": "{\"file_path\": \"/tmp/test.txt\"}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"      // 停止原因
    }
  ],
  "usage": {
    "prompt_tokens": 100,                // 输入 token 数
    "completion_tokens": 50,             // 输出 token 数
    "total_tokens": 150                  // 总 token 数
  },
  "system_fingerprint": "fp_..."         // 系统指纹（可选）
}
```

**finish_reason 值**:
| 值 | 说明 |
|---|------|
| `stop` | 自然结束 |
| `tool_calls` | 需要执行工具 |
| `length` | 达到最大 token 限制 |
| `content_filter` | 内容被过滤 |

### 2.3 流式响应 (SSE)

**请求**: 添加 `"stream": true, "stream_options": {"include_usage": true}`

**响应**: `Content-Type: text/event-stream`

**事件序列**:
```
data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4o","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4o","choices":[{"index":0,"delta":{"reasoning_content":"让我"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4o","choices":[{"index":0,"delta":{"reasoning_content":"思考一下"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"这是"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"回复"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4o","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_abc123","type":"function","function":{"name":"read_file","arguments":"{\"file_"}}]},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4o","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"path\":\"/tmp\"}"}}]},"finish_reason":null}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}

data: {"id":"chatcmpl-abc123","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4o","choices":[],"usage":{"prompt_tokens":100,"completion_tokens":50,"total_tokens":150}}

data: [DONE]
```

**流式 delta 字段**:
| 字段 | 说明 |
|------|------|
| `delta.role` | 角色（仅首次） |
| `delta.content` | 文本增量 |
| `delta.reasoning_content` | 推理内容增量（推理模型） |
| `delta.tool_calls` | 工具调用增量 |
| `finish_reason` | 停止原因（最后一次） |
| `usage` | Token 使用（最后一次，需开启） |

---

## 三、OpenAI Responses API

### 3.1 请求格式

**端点**: `POST /v1/responses`

**请求头**:
```http
Content-Type: application/json
Authorization: Bearer sk-...
```

**请求体**:
```jsonc
{
  "model": "gpt-5.1",
  "instructions": "你是一个助手...",       // 系统提示词（顶层字段）
  "input": [                               // input items，而不是 messages
    {
      "role": "user",
      "content": [
        {"type": "input_text", "text": "你好"},
        {"type": "input_image", "image_url": "data:image/png;base64,...", "detail": "auto"}
      ]
    },
    {
      "type": "message", "role": "assistant", "status": "completed",
      "content": [{"type": "output_text", "text": "我来读取文件"}]
    },
    {
      "type": "function_call",              // 工具调用是独立 item
      "call_id": "call_abc123",             // 与 function_call_output 配对
      "name": "read_file",
      "arguments": "{\"file_path\":\"/tmp/test.txt\"}"
    },
    {
      "type": "function_call_output",
      "call_id": "call_abc123",
      "output": "文件内容..."                // 或 input_text/input_image 列表
    },
    {
      "type": "reasoning",                  // 思考回放（store=false 多轮必需）
      "summary": [{"type": "summary_text", "text": "..."}],
      "encrypted_content": "gAAAA..."
    }
  ],
  "tools": [                               // 扁平 function 格式
    {
      "type": "function",
      "name": "read_file",
      "description": "读取文件内容",
      "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}},
      "strict": false
    }
  ],
  "tool_choice": "auto",
  "max_output_tokens": 16384,              // 下限 16
  "reasoning": {"effort": "medium", "summary": "auto"},  // low/medium/high/minimal/none
  "include": ["reasoning.encrypted_content"],            // 取回加密思考内容
  "store": false,                          // cili 无服务端会话，始终 false
  "stream": false,
  "prompt_cache_key": "<session id>"       // 可选；改善网关缓存路由
}
```

**与 Chat Completions 的关键差异**:

| 维度 | Chat Completions | Responses |
|------|------------------|-----------|
| 消息 | `messages`（role/tool_calls） | `input` items（message/function_call/function_call_output/reasoning） |
| 系统提示 | `role: system` 消息 | 顶层 `instructions` |
| 工具定义 | `{"type":"function","function":{...}}` | `{"type":"function","name","parameters",...}`（无 function 包裹） |
| 工具结果 | `role: tool` 消息 | `function_call_output` item（用 `call_id` 配对） |
| 思考回放 | `reasoning_content` 文本 | `reasoning` item（含 `encrypted_content`） |
| 输出上限 | `max_tokens` | `max_output_tokens` |
| 流式完成信号 | `finish_reason` + `[DONE]` | `response.completed` / `response.incomplete` |

### 3.2 非流式响应

**响应体**:
```jsonc
{
  "id": "resp_abc123",
  "object": "response",
  "status": "completed",                   // completed | incomplete | failed | cancelled
  "model": "gpt-5.1",
  "output": [
    {
      "type": "reasoning", "id": "rs_abc123",
      "summary": [{"type": "summary_text", "text": "让我思考一下..."}],
      "encrypted_content": "gAAAA..."
    },
    {
      "type": "message", "role": "assistant", "status": "completed",
      "content": [{"type": "output_text", "text": "这是回复内容"}]
    },
    {
      "type": "function_call", "id": "fc_abc123", "call_id": "call_abc123",
      "name": "read_file", "arguments": "{\"file_path\":\"/tmp/test.txt\"}"
    }
  ],
  "incomplete_details": null,              // incomplete 时为 {"reason": "max_output_tokens"}
  "usage": {
    "input_tokens": 100,                   // 已包含缓存命中部分
    "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 0},
    "output_tokens": 50,
    "output_tokens_details": {"reasoning_tokens": 20},
    "total_tokens": 150
  }
}
```

**status → stop_reason 映射**:

| Responses status | 条件 | 内部 stop_reason |
|------------------|------|------------------|
| `completed` | 无 function_call | `end_turn` |
| `completed` | 有 function_call | `tool_use` |
| `incomplete` | `max_output_tokens` | `length` |
| `incomplete` | `content_filter` | `content_filter` |
| `failed` / `cancelled` | — | `error` |

### 3.3 流式响应 (SSE)

**事件序列**（节选）:
```
data: {"type":"response.created","response":{"id":"resp_...","model":"gpt-5.1","status":"in_progress"}}

data: {"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","id":"rs_...","summary":[]}}

data: {"type":"response.reasoning_summary_text.delta","output_index":0,"item_id":"rs_...","summary_index":0,"delta":"让我"}

data: {"type":"response.output_item.done","output_index":0,"item":{"type":"reasoning","id":"rs_...","summary":[{"type":"summary_text","text":"让我思考"}],"encrypted_content":"gAAAA..."}}

data: {"type":"response.output_item.added","output_index":1,"item":{"type":"message","id":"msg_...","role":"assistant","content":[]}}

data: {"type":"response.output_text.delta","output_index":1,"item_id":"msg_...","content_index":0,"delta":"这是"}

data: {"type":"response.output_item.added","output_index":2,"item":{"type":"function_call","id":"fc_...","call_id":"call_...","name":"read_file","arguments":""}}

data: {"type":"response.function_call_arguments.delta","output_index":2,"item_id":"fc_...","call_id":"call_...","delta":"{\"file_"}

data: {"type":"response.output_item.done","output_index":2,"item":{"type":"function_call","id":"fc_...","call_id":"call_...","name":"read_file","arguments":"{\"file_path\":\"/tmp\"}","status":"completed"}}

data: {"type":"response.completed","response":{"id":"resp_...","status":"completed","model":"gpt-5.1","output":[...],"usage":{"input_tokens":100,"output_tokens":50,"total_tokens":150}}}
```

**SSE 事件类型**:

| 事件 | 说明 | 关键字段 |
|------|------|----------|
| `response.created` | 响应开始 | `response.id`, `response.model` |
| `response.output_item.added` | 输出 item 开始 | `output_index`, `item.type` |
| `response.output_text.delta` | 文本增量 | `output_index`, `item_id`, `delta` |
| `response.refusal.delta` | 拒绝原因增量 | 同上 |
| `response.reasoning_summary_text.delta` | 思考摘要增量 | `output_index`, `summary_index`, `delta` |
| `response.reasoning_text.delta` | 原始思考增量（部分端点） | 同上 |
| `response.function_call_arguments.delta` | 工具参数增量 | `output_index`, `call_id`, `delta` |
| `response.function_call_arguments.done` | 工具参数完整值 | `arguments` |
| `response.output_item.done` | 输出 item 完成 | `item`（含 `encrypted_content`） |
| `response.completed` / `response.incomplete` | 终态 | `response.status`, `response.output`, `response.usage` |
| `response.failed` | 失败 | `response.error.code/message` |

**注意**: Responses 的 `response.completed` 是唯一可信终态；思考的 `encrypted_content`
可能只在终态 `response.output` 里下发（Azure 等），cili 会以终态 item 覆盖流式签名。

---

## 四、内部统一格式

Cili 内部使用统一的 `LLMResponse` 格式，屏蔽两种 API 的差异。

### 3.1 LLMResponse 结构

```python
@dataclass
class LLMResponse:
    content: list[ContentBlock]        # 类型化内容块列表（TextBlock, ReasoningBlock, ToolCallBlock 等）
    stop_reason: str                   # "end_turn" | "tool_use" | "max_tokens" | "stop_sequence" | "length" | "content_filter"
    usage: UsageData                   # 类型化的 token 用量
    headers: dict[str, str]            # 响应头（用于 LiteLLM 检测等）
```

### 3.2 Content Block 类型

内部使用类型化 dataclass，而非 plain dict。所有类型均为 `ContentBlock = TextBlock | ReasoningBlock | ImageBlock | ToolCallBlock | ToolResultBlock` 的联合类型。

**TextBlock**（text 块）:
```python
@dataclass
class TextBlock:
    text: str = ""
```

**ReasoningBlock**（thinking 块）:
```python
@dataclass
class ReasoningBlock:
    text: str = ""
    signature: str | None = None  # Anthropic thinking 签名（可选）
```

**ToolCallBlock**（tool_use 块）:
```python
@dataclass
class ToolCallBlock:
    id: str = ""                 # tool_use_id / Responses call_id
    name: str = ""
    arguments: str = ""          # 原始 JSON 字符串（延迟解析，工具执行时才 parse）
    thought_signature: str = ""  # Google Gemini API 兼容字段
    item_id: str = ""            # Responses 的 fc_... item id（与 reasoning 配对）
```

**ImageBlock**（image 块）:
```python
@dataclass
class ImageBlock:
    data: str = ""        # base64 编码
    mime_type: str = ""
```

**ToolResultBlock**（tool_result 块）:
```python
@dataclass
class ToolResultBlock:
    tool_use_id: str = ""
    content: str | list[dict] = ""  # 纯文本或多模态内容
    is_error: bool = False
    # Worker/Lite 子代理执行扩展字段（可选）
    exec_id: str = ""
    iterations: int = 0
    message_count: int = 0
    duration_seconds: float = 0
```

序列化示例（发送前调用 `.to_dict()`）：
```json
{"type": "text", "text": "这是文本内容"}
{"type": "thinking", "thinking": "这是思考内容"}
{"type": "tool_use", "id": "toolu_abc123", "name": "read_file", "input": {"file_path": "/tmp/test.txt"}}
```

### 3.3 统一 Usage 格式

```python
@dataclass
class UsageData:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0     # 缓存读取 token 数
    cache_write_tokens: int = 0    # 缓存创建 token 数
```

注意：内部统一使用 `cache_read_tokens` / `cache_write_tokens`，与 Anthropic API 的 `cache_read_input_tokens` / `cache_creation_input_tokens` 名称不同。`UsageData.from_anthropic()` 和 `UsageData.from_openai()` 负责从各 provider 格式转换。

### 3.4 stop_reason 映射

| Anthropic | OpenAI | 内部 |
|-----------|--------|------|
| `end_turn` | `stop` | `end_turn` |
| `tool_use` | `tool_calls` | `tool_use` |
| `max_tokens` | - | `max_tokens` |
| `stop_sequence` | - | `stop_sequence` |
| - | `length` | `length` |
| - | `content_filter` | `content_filter` |

Anthropic 原生值直接使用（不做映射）；OpenAI 的 `finish_reason` 仅 `stop`→`end_turn`、`tool_calls`→`tool_use` 两项转换为 Anthropic 等价值，其余（`length`、`content_filter` 等）原样透传。

### 3.5 消息格式对比

**助手消息（含工具调用）**:

```python
# 内部使用 Message dataclass + typed ContentBlock
Message(
    role="assistant",
    content=[
        TextBlock(text="我来读取文件"),
        ToolCallBlock(id="toolu_abc123", name="read_file", arguments='{"file_path": "/tmp/test.txt"}'),
    ]
)

# 发送前通过 Adapter.serialize() 转换为 provider 格式

# Anthropic 格式（发送时）
{
    "role": "assistant",
    "content": [
        {"type": "text", "text": "我来读取文件"},
        {"type": "tool_use", "id": "toolu_abc123", "name": "read_file", "input": {"file_path": "/tmp/test.txt"}}
    ]
}

# OpenAI 格式（发送前转换）
{
    "role": "assistant",
    "content": "我来读取文件",
    "tool_calls": [
        {
            "id": "toolu_abc123",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": "{\"file_path\": \"/tmp/test.txt\"}"  # JSON 字符串
            }
        }
    ]
}
```

**工具结果消息**:

```python
# 内部使用 Message dataclass + ToolResultBlock
Message(
    role="user",
    content=[
        ToolResultBlock(tool_use_id="toolu_abc123", content="文件内容...")
    ]
)

# Anthropic 格式（发送时）
{
    "role": "user",
    "content": [
        {"type": "tool_result", "tool_use_id": "toolu_abc123", "content": "文件内容..."}
    ]
}

# OpenAI 格式（发送前转换，每个工具结果单独一条消息）
{
    "role": "tool",
    "tool_call_id": "toolu_abc123",
    "content": "文件内容..."
}
```

---

## 附录：关键差异总结

| 特性 | Anthropic | OpenAI Chat Completions | OpenAI Responses |
|------|-----------|-------------------------|------------------|
| **端点** | `/v1/messages` | `/v1/chat/completions` | `/v1/responses`（可由 `provider.api_path` 覆盖，如 DeepSeek 用 `/responses`） |
| **认证** | `x-api-key` | `Authorization: Bearer` | `Authorization: Bearer` |
| **系统提示** | 顶层 `system` 字段 | `messages` 中 `role: system` | 顶层 `instructions` |
| **工具参数** | `input_schema` | `function.parameters` | 扁平 `parameters` |
| **工具参数格式** | JSON 对象 | JSON 字符串 | JSON 字符串 |
| **工具结果** | `tool_result` in user message | 独立 `role: tool` message | `function_call_output` item |
| **Thinking** | `thinking.budget_tokens` / `effort` | `reasoning_effort` (仅推理模型) | `reasoning.effort` + `summary` |
| **Thinking 与 temperature** | 互斥 | reasoning_effort 与 temperature 互斥 | reasoning 与 temperature 互斥 |
| **流式 usage** | `message_delta` 事件 | `stream_options.include_usage` | `response.completed.usage` |
| **Thinking 多轮** | 必须保留在消息中 | `reasoning_content` 会重发（CoT passback） | `reasoning` item + `encrypted_content` |

---

*文档版本: v1.1*
*最后更新: 2026-11-16*
