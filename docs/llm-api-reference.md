# LLM API 接口参考

本文档详细记录 Anthropic Messages API 和 OpenAI Chat Completions API 的原始接口结构，供开发参考。

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
- [三、内部统一格式](#三内部统一格式)

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
  // 或使用 effort 级别：
  "thinking": {
    "type": "enabled",
    "effort": "medium"                   // "low" | "medium" | "high" | "xhigh" | "max"
  },

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

  // LiteLLM 代理路由（可选）
  "litellm_session_id": "session-abc123"
}
```

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

  // Reasoning 配置（仅推理模型：o1, o3, o4-mini 等）
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

  // LiteLLM 代理路由（可选）
  "litellm_session_id": "session-abc123"
}
```

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

## 三、内部统一格式

Cili 内部使用统一的 `LLMResponse` 格式，屏蔽两种 API 的差异。

### 3.1 LLMResponse 结构

```python
@dataclass
class LLMResponse:
    content: list[dict[str, Any]]  # 内容块列表
    stop_reason: str               # "end_turn" | "tool_use" | "stopped" | "error"
    usage: dict                    # 统一的 usage 格式
    headers: dict                  # 响应头（用于 LiteLLM 检测等）
```

### 3.2 Content Block 类型

**text 块**:
```json
{"type": "text", "text": "这是文本内容"}
```

**thinking 块**:
```json
{"type": "thinking", "thinking": "这是思考内容"}
```

**tool_use 块**:
```json
{
  "type": "tool_use",
  "id": "toolu_abc123",
  "name": "read_file",
  "input": {"file_path": "/tmp/test.txt"}
}
```

### 3.3 统一 Usage 格式

```python
usage = {
    "input_tokens": 100,               # 输入 token 数
    "output_tokens": 50,               # 输出 token 数
    "cache_read_input_tokens": 0,      # 缓存读取 token 数
    "cache_creation_input_tokens": 0   # 缓存创建 token 数
}
```

### 3.4 stop_reason 映射

| Anthropic | OpenAI | 内部 |
|-----------|--------|------|
| `end_turn` | `stop` | `end_turn` |
| `tool_use` | `tool_calls` | `tool_use` |
| `max_tokens` | `length` | `max_tokens` |
| `stop_sequence` | - | `stop_sequence` |

### 3.5 消息格式对比

**助手消息（含工具调用）**:

```python
# Anthropic 格式（内部使用）
{
    "role": "assistant",
    "content": [
        {"type": "text", "text": "我来读取文件"},
        {"type": "tool_use", "id": "toolu_abc123", "name": "read_file", "input": {...}}
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
# Anthropic 格式（内部使用）
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

| 特性 | Anthropic | OpenAI |
|------|-----------|--------|
| **端点** | `/v1/messages` | `/v1/chat/completions` |
| **认证** | `x-api-key` | `Authorization: Bearer` |
| **系统提示** | 顶层 `system` 字段 | `messages` 中 `role: system` |
| **工具参数** | `input_schema` | `function.parameters` |
| **工具参数格式** | JSON 对象 | JSON 字符串 |
| **工具结果** | `tool_result` in user message | 独立 `role: tool` message |
| **Thinking** | `thinking.budget_tokens` / `effort` | `reasoning_effort` (仅推理模型) |
| **Thinking 与 temperature** | 互斥 | reasoning_effort 与 temperature 互斥 |
| **流式 usage** | `message_delta` 事件 | `stream_options.include_usage` |
| **Thinking 多轮** | 必须保留在消息中 | `reasoning_content` 不能重发 |

---

*文档版本: v1.0*
*最后更新: 2026-08-28*
