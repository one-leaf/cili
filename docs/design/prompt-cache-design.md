# Prompt Cache 优化设计文档

本文档描述 Cili Agent 的 Prompt Cache 优化方案：缓存断点布局、System Prompt 静态/动态分离，以及缓存状态追踪机制，目标是将 Anthropic Prompt Cache 命中率提升至 >90%，降低重复 input 计费。

---

## 一、设计目标

- **高命中率**：将 Anthropic Prompt Cache 命中率从不可控状态提升至 >90%（排除压缩后首轮）
- **跨用户共享**：静态区（role + tools + skills）通过 `scope: 'global'` 跨会话共享
- **Session 内稳定**：动态区（env_context）在 session 内缓存，`/clear` 或 `/compact` 时重算
- **可观测**：追踪缓存命中率，检测并诊断缓存失效原因

---

## 二、现状问题

### 2.1 缓存断点布局缺陷

优化前的断点布局（`core/llm/anthropic.py`）：

| 区域 | 断点数 | 问题 |
|------|--------|------|
| system | 1（整段） | 无 static/dynamic 分离，动态内容导致整段失效 |
| tools | 0 | 每轮重新缓存 ~3000–8000 tokens |
| messages | 1（仅末尾） | messages[-1] 一个断点，稳定区无断点 |

### 2.2 动态内容位置致命

优化前，`env_context`（含 `datetime.now()`）与 `claude_md` 合并注入到 `messages[0]`（通过 `assemble_context`）。

`env_context` 每天变化，`claude_md` 修改后也变化，导致从请求体第一字节开始就不同，Anthropic 前缀缓存 100% 失效。

### 2.3 压缩与缓存的冲突

| 压缩层 | 对缓存的影响 |
|--------|-------------|
| L1 microcompact | 替换旧 tool_result 内容 → 前缀改变 → 缓存失效（但 L1 幂等，首次后稳定） |
| L2 full compact | 完整重建消息序列 → 所有缓存失效 |
| L3 emergency | 标记旧 tool_use 为无效 → 改变前缀 → 缓存失效 |

---

## 三、缓存断点布局

优化后的请求体布局如下：

```
API 请求体（优化后）：

system: [
  { text: 静态区（role + tools + skills），scope: 'global' },   ← 跨用户共享
  { text: 动态区（env_context） },                                ← session 内稳定
]

tools: [ ..., { name: last_tool, cache_control: {...} } ]        ← 新增断点

messages: [
  hist[0..N-3] + cache_control 在 hist[N-3] ],                   ← 稳定区断点
  hist[N-2],
  hist[N-1] + cache_control                                       ← 尾部断点
]
```

### 3.1 断点说明

| 断点 | 位置 | 说明 |
|------|------|------|
| System 静态区 | system[0] | `scope: 'global'`，跨用户共享，内容不变则永久命中 |
| Tools 末尾 | tools[-1] | 工具定义稳定，每轮节省 ~3000–8000 tokens |
| Messages[-3] | 倒数第三条消息 | tool-use loop 中，N-3 之前内容在多轮内稳定 |
| Messages[-1] | 末尾消息 | 保留原断点，覆盖最新变化 |

---

## 四、System Prompt 静态/动态分离

### 4.1 分离策略

在 system prompt 构建时插入 `DYNAMIC_BOUNDARY` 哨兵，将内容分为两段：

```
system prompt 构建流程：

static_blocks[]          ← BOUNDARY 之前：role + tools_def + skills + ...
  └─ scope: 'global'     ← 跨用户共享缓存

─── DYNAMIC_BOUNDARY ────

dynamic_blocks[]         ← BOUNDARY 之后：env_context
  └─ 无 cache_control    ← session 内稳定，不跨用户
```

`DYNAMIC_BOUNDARY` 是一个不会出现在正常文本中的哨兵字符串，`build_system_prompt()` 返回带该哨兵的 `list[str]`，由 `AnthropicAdapter` 负责分割处理。

### 4.2 环境上下文的 Session 内缓存

`env_context` 块（`_gen_context`）使用 `agent._prompt_section_cache` 做 session 内缓存：

- 首次访问：计算并写入缓存
- 后续访问：直接读取缓存（不重新计算）
- 清除时机：`reset()`、`compact()`、`switch_session()`

### 4.3 角色配置修改

三个角色的 JSON 配置（`core/agents/`）均已添加 `dynamic_boundary` + `context` 块：

| 角色 | system_prompt.blocks 变化 | user_layers 变化 |
|------|--------------------------|-----------------|
| master | 末尾追加 `dynamic_boundary` + `context` 块 | 移除 `context`，保留 `claude_md` |
| worker | 末尾追加 `dynamic_boundary` + `context` 块 | 移除 `context`，保留 `task_message` + `runtime_prompts` |
| lite | 末尾追加 `dynamic_boundary` + `context` 块 | 无变化 |

### 4.4 返回值类型变更

`build_system_prompt()` 由返回 `str` 改为返回 `list[str]`：

- 列表中包含所有静态块（顺序拼接）
- 如有动态块，在中间插入 `DYNAMIC_BOUNDARY`，再追加动态块
- 下游全链路（adapter → client → runner）的 `system` 参数类型统一为 `str | list[str]`

---

## 五、缓存状态追踪

### 5.1 CacheState 类

新增 `core/cache_state.py`，每个 SessionRunner 持有一个实例，追踪缓存状态：

**属性**：

| 属性 | 类型 | 说明 |
|------|------|------|
| `l1_triggered` | bool | L1 microcompact 是否曾触发 |
| `l2_triggered` | bool | L2 full compact 是否曾触发 |
| `l3_triggered` | bool | L3 emergency 是否曾触发 |
| `_last_cache_read` | int | 上次 LLM 响应的 cache_read tokens |
| `_last_request_time` | float | 上次请求时间戳 |
| `total_cache_read` | int | 累计 cache_read tokens |
| `total_cache_write` | int | 累计 cache_write tokens |
| `total_input` | int | 累计新计费 input tokens |
| `cache_break_count` | int | 检测到的缓存失效次数 |
| `llm_call_count` | int | LLM 调用总次数 |

**方法**：

| 方法 | 说明 |
|------|------|
| `on_compression(layer)` | 记录压缩事件（layer: 1=L1, 2=L2, 3=L3） |
| `on_llm_response(cache_read, cache_write, input_tokens)` | 更新统计并检测缓存失效 |
| `reset_after_compact()` | L2 full compact 或 switch_session 后重置压缩标记和缓存基线，保留累计统计 |
| `reset()` | 完全重置（`/clear` 时调用），清除所有统计 |
| `hit_rate`（属性） | 累计命中率 = `cache_read / (cache_read + input_tokens)` |
| `to_dict()` | 返回统计摘要（供日志或前端展示） |

### 5.2 缓存失效检测

在 `on_llm_response()` 中，当满足以下**两个条件同时成立**时判定缓存失效：

1. **相对下降**：`cache_read < last_cache_read × 0.95`（下降超过 5%）
2. **绝对下降**：`last_cache_read - cache_read > 2000`（绝对下降超过 2000 tokens）

双重条件避免小幅波动或首次调用被误判为缓存失效。

### 5.3 失效原因诊断

检测到缓存失效时，按以下优先级链诊断原因（首次匹配）：

| 优先级 | 条件 | 诊断结果 |
|--------|------|---------|
| 1 | `l2_triggered == True` | L2 full compact 重建了消息序列 |
| 2 | `l1_triggered == True` | L1 microcompact 首次触发改变了旧 tool_result 内容 |
| 3 | `l3_triggered == True` | L3 emergency 标记了旧 tool_use 为无效 |
| 4 | 距上次请求超过 4 分钟 | TTL 过期（Anthropic 短 TTL 实际约 5 分钟，保守用 4 分钟） |
| 5 | 以上均不满足 | 未知（可能是动态注入内容变化或外部因素） |

---

## 六、与三层压缩的协调

### 6.1 压缩事件通知

`runner._check_and_compress()` 在各层压缩触发后通知 `cache_state`：

| 压缩层 | 通知时机 | 说明 |
|--------|---------|------|
| L1 microcompact | `saved > 0` 后 | 首次触发会打破缓存，之后幂等稳定 |
| L2 full compact | 压缩完成后 | 先 `reset_after_compact()` 再 `on_compression(2)`，避免旧基线干扰 |
| L3 emergency | `saved > 0` 后 | 旧 tool_use 被标记为无效 |

**注意**：L2 full compact 的处理顺序为先重置再标记（`reset_after_compact()` 在前，`on_compression(2)` 在后），防止 `reset_after_compact()` 将刚设置的 `l2_triggered` 重置为 `False`。

### 6.2 LLM 响应后更新

三处 LLM 响应点均调用 `on_llm_response()`：

| 调用点 | 位置 |
|--------|------|
| 非流式响应 | `runner._call_llm_non_streaming()` |
| 流式响应 | `runner._call_llm_streaming()` |
| 413 重试响应 | `runner._call_llm_non_streaming()` 内部重试路径 |

摘要生成请求不通知 `cache_state`（摘要请求的 cache 统计不反映正常对话的缓存状态）。

### 6.3 生命周期事件

| 事件 | 调用方法 | 说明 |
|------|---------|------|
| `/clear`（reset） | `cache_state.reset()` | 清除所有统计和标记 |
| `/compact`（手动压缩） | `reset_after_compact()` + `on_compression(2)` | 重置基线并标记 L2 |
| `switch_session` | `reset_after_compact()` | 重置基线（不同会话的 cache_read 不可比），保留累计统计 |

---

## 七、非 Anthropic 端点兼容

### 7.1 OpenAI 适配器

`OpenAIAdapter.serialize()` 收到 `list[str]` 类型的 system 时：
- 过滤掉 `DYNAMIC_BOUNDARY` 哨兵
- 将剩余内容 join 为单字符串，作为 system role message 发送
- OpenAI 协议不支持 `cache_control`，无需进一步处理

### 7.2 非官方端点（中转/网关）

`AnthropicAdapter._prompt_cache_enabled` 为 `False` 时（检测到非官方 API 端点）：
- 不调用任何 `cache_control` 相关逻辑（这些端点不识别会返回 400）
- `list[str]` 类型的 system 过滤 `DYNAMIC_BOUNDARY` 后 join 为字符串
- `str` 类型的 system 直接透传

---

## 八、相关文件

| 文件 | 职责 |
|------|------|
| `core/cache_state.py` | CacheState 类：缓存状态追踪、失效检测、诊断、统计 |
| `core/prompt_builder.py` | `DYNAMIC_BOUNDARY` 哨兵、`build_system_prompt()` 返回 `list[str]`、`_gen_context()` session 缓存、`clear_prompt_section_cache()` |
| `core/llm/anthropic.py` | 4 断点布局实现：system 静态区（`scope: 'global'`）、tools[-1]、messages[-3]、messages[-1] |
| `core/llm/openai.py` | `list[str]` system 过滤 BOUNDARY 并 join 为字符串 |
| `core/llm/adapter.py` | `serialize()` 签名统一为 `system: str | list[str]` |
| `core/llm/client.py` | `chat()` / `chat_stream()` / `chat_structured()` 签名同步更新 |
| `core/session_runner.py` | `_build_system_prompt()` 返回 `list[str]`、`_prompt_section_cache` 初始化、`reset()`/`compact()`/`switch_session()` 集成 |
| `core/session_runner_runtime/runner.py` | `_check_and_compress()` 通知压缩事件、LLM 响应后更新缓存统计 |
| `core/base_session_runner.py` | `CacheState` 初始化、`system` 参数类型同步更新 |
| `core/agents/master.json` | system_prompt.blocks 末尾追加 `dynamic_boundary` + `context`，user_layers 移除 `context` |
| `core/agents/worker.json` | 同上，user_layers 移除 `context`，保留 `task_message` + `runtime_prompts` |
| `core/agents/lite.json` | system_prompt.blocks 末尾追加 `dynamic_boundary` + `context` |
| `test/test_cache_state.py` | CacheState 单元测试（19 个） |

---

## 九、设计决策

### 9.1 为什么 `claude_md` 保留在 user_layers 而非 system 动态区？

- `claude_md`（CLAUDE.md 内容）随文件修改变化，放 system 静态区会破坏 global cache
- 放 system 动态区需要 session 内缓存，但用户编辑后需立即反映，缓存反而导致滞后
- 保留在 user_layers（通过 `assemble_context` 注入 messages），由于 messages[0] 已无 `env_context`（已移到 system），其变化频率降低，对 messages 层缓存的影响有限

### 9.2 为什么 `worker/lite` 的 `task_message` 保留在 user_layers？

- `task_message` 是一次性注入后持久化的，内容固定，首次插入后稳定
- 与 `claude_md` 合并后位于 messages[0]，不破坏缓存

### 9.3 为什么 TTL 用 4 分钟（而非 Anthropic 实际的 5 分钟）？

- Anthropic 短 TTL 实际约 5 分钟，但受负载波动影响可能提前失效
- 保守用 4 分钟（`_TTL_MS = 4 × 60 × 1000`）诊断 TTL 过期，减少误判为"未知"原因

### 9.4 为什么缓存失效检测需要双重条件？

- 仅相对下降（>5%）：小 cache_read 值的正常波动会触发误报
- 仅绝对下降（>2000 tokens）：大请求的微小比例下降被误判
- 两个条件同时满足才判定失效，显著降低误报率

### 9.5 为什么 L2 处理顺序是先 reset 再标记？

- `reset_after_compact()` 会将 `l2_triggered` 重置为 `False`，同时清除 `_last_cache_read` 基线
- 若先 `on_compression(2)` 再 `reset_after_compact()`，`l2_triggered` 会被立即清零，下次失效诊断将错误地报为"未知"
- 正确顺序：先 reset（清基线 + 清标记）→ 再 `on_compression(2)`（标记 L2 供下次诊断）

### 9.6 为什么 switch_session 只 reset_after_compact 而非 reset？

- 切换会话时，旧会话的 `_last_cache_read` 基线对新会话无意义，必须清除
- 但累计统计（`total_cache_read`、`llm_call_count` 等）代表 agent 的长期使用量，应保留
- `reset_after_compact()` 恰好满足：清基线 + 清压缩标记，保留累计统计

---

## 十、实施进度

| 阶段 | 说明 | 状态 |
|------|------|------|
| P0：缓存断点优化 | tools[-1] + messages[-3] 双断点 | 已实现 |
| P1：System Prompt 静态/动态分离 | `DYNAMIC_BOUNDARY` + context 块 + 类型变更 | 已实现 |
| P2：缓存状态追踪 | `CacheState` + runner 集成 + 19 个单元测试 | 已实现 |
| P3：主动保温（CacheWarmer） | TTL 到期前发送保温请求，保持缓存条目活跃 | 未实现（可选） |

---

**文档版本**: v1.0  
**创建时间**: 2026-09-28  
**状态**: P0/P1/P2 已实现
