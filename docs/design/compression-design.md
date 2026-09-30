# 消息压缩设计文档

本文档描述 Cili Agent 的三层消息压缩机制，防止上下文超出 token 限制。

## 一、设计目标

- **渐进式**：先标记无效消息，不够再完整压缩
- **可靠性**：紧急压缩防止 413 Payload Too Large 错误
- **无感知**：LLM 看到的消息格式保持一致

---

## 二、架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                        BaseAgent._check_and_compress()           │
│                                                                   │
│   Layer 1: Microcompact  ──────────────────────────────────┐     │
│   每轮都运行，标记孤立 tool_result 和旧错误结果对           │     │
│                                                              │     │
│   Layer 2: Full Compact  ───────────────────────────────┐  │     │
│   token > 80% 阈值时，LLM 摘要旧消息                     │  │     │
│                                                          │  │     │
│   Layer 3: Emergency  ───────────────────────────────┐  │  │     │
│   body > 3MB 时，旧图片替换为文本占位符              │  │  │     │
└───────────────────────────────────────────────────────┴──┴──┴─────┘
```

---

## 三、三层压缩详解

### 3.1 Layer 1: Microcompact（轻量压缩）

**触发条件**：每轮 LLM 调用前都运行

**实现位置**：`core/compression.py::microcompact_mark_orphans_and_errors()`

**策略**：
1. **孤立 tool_result 标记**：扫描所有 `role=user` 消息中的 `tool_result` 块，若其 `tool_use_id` 在有效消息中找不到对应的 `tool_use`（例如对应 assistant 消息已被 Layer 2/3 标记失效），则将该 user 消息标记为 `_meta.valid=False`
2. **错误结果对标记**：统计所有含 `is_error=True` 的 tool_result，保留最近 **10 条**，更早的错误结果对（包含 tool_use 的 assistant 消息 + 包含 tool_result 的 user 消息）整对标记为 `_meta.valid=False`

**元数据字段**（消息级 `_meta`）：
| 字段 | 类型 | 说明 |
|------|------|------|
| `valid` | bool | 消息是否有效（false = 发送给 API 时过滤） |

**示例**：
```json
{
  "role": "user",
  "content": [{"type": "tool_result", "tool_use_id": "call_abc123", ...}],
  "_meta": {
    "valid": false
  }
}
```

**注意**：
- 本层不再对 tool_result 内容进行压缩或清空，所有内容保持原样
- 孤立检测依赖前序 Layer 2/3 已标记的消息（跨轮生效）

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
  ... 分界点之前的老消息（已标记为消息级 _meta.valid=False，保留但不发送） ...
  ... 保留的最近消息 ...
  {"role": "user", "content": "[Our previous conversation has been compacted due to context length.]"},
  {"role": "assistant", "content": "{summary}"}
]
```

---

### 3.3 Layer 3: Emergency（紧急压缩）

**触发条件**：请求体 > 3MB（`MAX_BODY_SIZE = 3_000_000`）

**实现位置**：`core/base_agent.py::_mark_old_images_invalid()`

**策略**：
将图片**从旧到新依次替换为文本占位符**（`[image removed to reduce request size]`），每替换一张重新计算请求体大小，直到小于 3MB 或所有图片都已替换。

**标记方式**（图片就地替换 tool_result 内的 image 子块为文本占位符，不做消息级无效，避免 tool_use/tool_result 配对断裂）：
```python
# 图片：就地替换 tool_result 内的 image 子块为文本占位符
{
  "type": "tool_result",
  "content": [
    {"type": "image", "source": {...}},  # → 被替换为
    {"type": "text", "text": "[image removed to reduce request size]"}
  ]
}
```

**注意**：此层压缩会丢失图片信息，LLM 无法恢复。不再标记旧工具调用无效。

---

## 四、外部存储机制

### 4.1 工具结果存储

并非所有工具输出都存储到外部文件。只有以下情况才会写入外部文件：
- **流式工具**（`bash`、`python`）：实时写入，供前端轮询显示
- **大输出**：超过 10,000 字符（`_LARGE_OUTPUT_THRESHOLD`，将被截断）
- **多模态内容**：含图片，保存为 `.json`

其余小体积非流式输出直接内联存储在消息 `content` 中。

**存储位置**：
- Master（交互会话）：`{session_dir}/{tool_use_id}.txt` 或 `.json`
- Worker/Lite（委派执行）：`{session_dir}/{exec_dir}/{tool_use_id}.txt` 或 `.json`

**存储时机**：`Tool.execute()` 返回 `ToolResult` 时

**实现位置**：`core/tools/base.py::Tool.save_output_to_file()`

### 4.2 按需读取

`_load_external_tool_results()` 在发送 LLM 前读取外部文件，获取完整的工具输出内容。

---

## 五、Token 估算

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
| `ERROR_RESULT_KEEP_RECENT` | 10 | Microcompact 保留的最近错误结果数 |
| `FULL_COMPACT_TOKEN_RATIO` | 0.80 | 触发 Full Compact 的 token 比例 |
| `MAX_BODY_SIZE` | 3_000_000 | 触发 Emergency 的字节数（3MB） |

---

## 八、调用流程

```
用户输入
    │
    ▼
Agent.run()（master 交互式 / worker、lite 自主式）
    │
    ├── 添加用户消息
    │
    └── 循环：
            │
            ├── _check_and_compress()
            │       │
            │       ├── Layer 1: microcompact_mark_orphans_and_errors()
            │       │
            │       ├── Layer 2: (token > 阈值) _perform_full_compact()
            │       │
            │       └── Layer 3: (body > 3MB) _mark_old_images_invalid()
            │
            ├── _call_llm()
            │       │
            │       ├── _get_messages_with_header()
            │       │
            │       ├── _load_external_tool_results()  ← 读取外部文件
            │       │
            │       └── HTTP 请求
            │
            └── 解析响应 → 执行工具 → 添加消息 → 继续循环
```

---

## 九、文件清单

| 文件 | 职责 |
|------|------|
| `core/compression.py` | 压缩函数（microcompact 标记、token 计数） |
| `core/base_agent.py` | 三层压缩调用逻辑、`_load_external_tool_results()`、图片替换 |
| `core/agent_runtime/runner.py` | 压缩调度（`_check_and_compress`）、Full Compact 实现 |
| `core/tools/read.py` | 读取工具输出文件（替代旧的 `read_tool_result` 工具） |
| `core/tools/base.py` | 工具输出外部存储 |

---

## 十、设计决策

### 10.1 为什么 Layer 1 不再压缩内容？

Microcompact 不再对 tool_result 内容进行压缩或清空，只标记消息有效性。

**原因**：
- 简化逻辑，减少 `_load_external_tool_results` 的复杂性
- 内容保留在消息中，前端可直接展示完整历史
- 错误结果对的整对标记保持 tool_use/tool_result 配对完整性

### 10.2 为什么错误结果对保留最近 10 条？

经验值：
- 错误结果通常需要 LLM 记住以调整策略
- 太早的错误（超过 10 条）参考价值低，可安全移除

### 10.3 为什么 Full Compact 用 LLM 摘要？

简单截断会丢失关键上下文（如已完成的工作、关键决策）。LLM 摘要能保留语义，但增加一次 API 调用。

### 10.4 为什么 Emergency 只替换图片？

图片体积大（数千至数万 token），是请求体超限的主要原因。只替换图片而不标记工具调用失效，可保留工具执行的完整历史，LLM 仍能了解之前做了什么操作。
