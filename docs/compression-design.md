# 消息压缩设计文档

本文档描述 Cili Agent 的三层消息压缩机制，防止上下文超出 token 限制。

## 一、设计目标

- **渐进式**：先轻量压缩，不够再完整压缩
- **可恢复**：压缩后的工具结果可通过 `read_tool_result` 工具重新获取
- **可靠性**：紧急压缩防止 413 Payload Too Large 错误
- **无感知**：LLM 看到的消息格式保持一致

---

## 二、架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                        BaseAgent._check_and_compress()           │
│                                                                   │
│   Layer 1: Microcompact  ──────────────────────────────────┐     │
│   每轮都运行，标记旧工具结果为 _meta.compacted=True          │     │
│                                                              │     │
│   Layer 2: Full Compact  ───────────────────────────────┐  │     │
│   token > 80% 阈值时，LLM 摘要旧消息                     │  │     │
│                                                          │  │     │
│   Layer 3: Emergency  ───────────────────────────────┐  │  │     │
│   body > 3MB 时，标记旧工具调用/图片为 valid=False    │  │  │     │
│                                                       │  │  │     │
└───────────────────────────────────────────────────────┴──┴──┴─────┘
                              │
                              ▼
              _resolve_tool_results() 注入占位符
              compacted 的工具结果 → "[Compacted: use read_tool_result...]"
```

---

## 三、三层压缩详解

### 3.1 Layer 1: Microcompact（轻量压缩）

**触发条件**：每轮 LLM 调用前都运行

**实现位置**：`core/compression.py::microcompact_tool_results()`

**策略**：
- 保留最近 6 条工具结果（`keep_recent=6`）
- 更早的工具结果标记为 `_meta.compacted = True`
- 跳过小于 200 字符的工具结果（压缩收益低）

**元数据字段**（`tool_result._meta`）：
| 字段 | 类型 | 说明 |
|------|------|------|
| `compacted` | bool | 是否被压缩 |
| `output_path` | str | 外部存储文件路径 |
| `file_size` | int | 原始文件大小（字节） |

**示例**：
```json
{
  "type": "tool_result",
  "tool_use_id": "call_abc123",
  "_meta": {
    "compacted": true,
    "output_path": "exec_xxx/call_abc123.txt",
    "file_size": 15234
  }
}
```

**LLM 视角**：
压缩后的工具结果在发送给 LLM 时，由 `_resolve_tool_results()` 注入占位符：
```
[Compacted: use `read_tool_result` tool with tool_use_id="call_abc123" to retrieve original content]
```

---

### 3.2 Layer 2: Full Compact（完整压缩）

**触发条件**：`total_tokens > max_context_tokens * 0.80`

**实现位置**：`core/base_agent.py::_perform_full_compact()`

**策略**：
1. 保留最近 N 条用户消息（`KEEP_USER_MESSAGES = 3`）
2. 更早的消息由 LLM 生成摘要
3. 摘要作为新的 user+assistant 消息对插入
4. 旧消息标记消息级 `_meta.valid=False`（不删除，发送给 API 时被过滤）

**摘要 Prompt**：
```
请用中文简洁地总结以下对话的主要内容，包括：
1. 用户的主要需求和目标
2. 已完成的关键操作
3. 当前进展状态
4. 重要的上下文信息

对话内容：
{conversation_text}

请用 200-400 字总结：
```

系统提示词：`你是一个对话总结助手。请用中文简洁地总结对话要点。`

**压缩后消息格式**：
旧消息不做删除，而是标记消息级 `_meta.valid=False`；在消息列表末尾追加两条新消息（英文占位提示 + 原始摘要文本）：

```json
[
  {"role": "user", "content": "[Our previous conversation has been compacted due to context length.]"},
  {"role": "assistant", "content": "{summary}"},
  ... 分界点之前的老消息（已标记为消息级 _meta.valid=False，保留但不发送） ...
  ... 保留的最近消息 ...
]
```

---

### 3.3 Layer 3: Emergency（紧急压缩）

**触发条件**：请求体 > 3MB（`MAX_BODY_SIZE = 3_000_000`）

**实现位置**：`core/base_agent.py::_mark_old_tool_calls_invalid()`、`_mark_old_images_invalid()`

**策略**：
1. 优先将包含旧工具调用的**整条消息**标记为 `_meta.valid=False`（消息级标记），保留最近 3 轮
2. 若仍超限，将包含旧图片的消息标记为 `_meta.valid=False`（消息级标记），保留最近 3 张

**标记方式**（消息级 `_meta.valid`，工具调用/图片所在的整条消息被跳过）：
```python
{
  "role": "assistant",  # 或包含 tool_result 的 user 消息
  "content": [{"type": "tool_use", "id": "call_xyz", ...}],
  "_meta": {"valid": false}
}
```

**注意**：此层压缩会丢失工具调用信息，LLM 无法恢复。

---

## 四、外部存储机制

### 4.1 工具结果存储

并非所有工具输出都存储到外部文件。只有以下情况才会写入外部文件：
- **流式工具**（`bash`、`python`）：实时写入，供前端轮询显示
- **大输出**：超过 10,000 字符（`_LARGE_OUTPUT_THRESHOLD`，将被截断）
- **多模态内容**：含图片，保存为 `.json`

其余小体积非流式输出直接内联存储在消息 `content` 中。

**存储位置**：
- RootAgent：`{session_dir}/{tool_use_id}.txt` 或 `.json`
- SubAgent：`{session_dir}/{exec_dir}/{tool_use_id}.txt` 或 `.json`

**存储时机**：`Tool.execute()` 返回 `ToolResult` 时

**实现位置**：`core/tools/shared/base.py::Tool._save_output()`

### 4.2 按需读取

`_resolve_tool_results()` 在发送 LLM 前读取外部文件：
- 未压缩：读取完整内容
- 已压缩：注入占位符

---

## 五、`read_tool_result` 工具

当 LLM 需要重新获取被压缩的工具结果时，使用此工具。

**参数**：
| 字段 | 类型 | 说明 |
|------|------|------|
| `tool_use_id` | str | 工具调用的唯一 ID |

**实现位置**：`core/tools/shared/read_tool_result.py`

**示例调用**：
```python
read_tool_result(tool_use_id="call_abc123")
```

---

## 六、Token 估算

### 6.1 估算算法

**实现位置**：`core/compression.py::count_tokens_approx()`

```python
def count_tokens_approx(text: str) -> int:
    chinese_chars = sum(1 for c in text if '一' <= c <= '鿿')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 2.5 + other_chars / 4)
```

- 中文：约 2.5 字符/token
- 英文：约 4 字符/token

### 6.2 图片 Token

```python
# 图片约 750-1000 tokens
data = sub.get("source", {}).get("data", "")
total += max(750, len(data) // 100)
```

---

## 七、配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MICROCOMPACT_KEEP_RECENT` | 6 | Microcompact 保留的最近工具结果数 |
| `FULL_COMPACT_TOKEN_RATIO` | 0.80 | 触发 Full Compact 的 token 比例 |
| `MAX_BODY_SIZE` | 3_000_000 | 触发 Emergency 的字节数（3MB） |
| `KEEP_USER_MESSAGES` | 3 | Full Compact 保留的用户消息数 |

---

## 八、调用流程

```
用户输入
    │
    ▼
BaseAgent.run()
    │
    ├── 添加用户消息
    │
    └── 循环：
            │
            ├── _check_and_compress()
            │       │
            │       ├── Layer 1: microcompact_tool_results()
            │       │
            │       ├── Layer 2: (token > 阈值) _perform_full_compact()
            │       │
            │       └── Layer 3: (body > 3MB) _mark_old_*_invalid()
            │
            ├── _call_llm()
            │       │
            │       ├── _get_messages_with_header()
            │       │
            │       ├── _resolve_tool_results()  ← 注入占位符
            │       │
            │       └── HTTP 请求
            │
            └── 解析响应 → 执行工具 → 添加消息 → 继续循环
```

---

## 九、文件清单

| 文件 | 职责 |
|------|------|
| `core/compression.py` | 压缩函数（microcompact、token 计数、LLM 摘要） |
| `core/base_agent.py` | 三层压缩调用逻辑、`_resolve_tool_results()` |
| `core/tools/shared/read_tool_result.py` | 重新获取压缩的工具结果 |
| `core/tools/shared/base.py` | 工具输出外部存储 |

---

## 十、设计决策

### 10.1 为什么用标记而非替换？

Microcompact 使用 `_meta.compacted = True` 标记，而非直接替换内容为占位符。

**原因**：
- 保留原始数据，支持 `read_tool_result` 恢复
- 发送 LLM 时再注入占位符，避免 Session 存储冗余

### 10.2 为什么保留最近 6 条？

经验值：
- 太少：LLM 频繁调用 `read_tool_result`，增加延迟
- 太多：压缩效果差，token 消耗高

### 10.3 为什么 Full Compact 用 LLM 摘要？

简单截断会丢失关键上下文（如已完成的工作、关键决策）。LLM 摘要能保留语义，但增加一次 API 调用。

### 10.4 为什么需要 Emergency 层？

某些场景（如大文件读取、多图片）可能使请求体远超 token 限制。Emergency 层是最后防线，防止 413 错误。
