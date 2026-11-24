# Prompt Cache 设计

> 按当前实现重写（2026-11-25）。代码：`core/llm/anthropic.py`、`core/llm/openai.py`、
> `core/llm/adapter.py`、`core/prompt_builder.py`。

## 一、契约

`Adapter` 基类声明：

- `cache_enabled`（属性）：当前配置/协议是否启用缓存。
- `apply_prompt_cache(body, system, messages, tools=None)`：在请求体上打缓存断点；
  不支持缓存的适配器为 no-op。

## 二、Anthropic 断点（4 处）

| 断点 | 位置 | cache_control |
|---|---|---|
| system 静态区 | `DYNAMIC_BOUNDARY` 之前的文本块最后一段 | `ephemeral` + `scope: global` |
| tools | 最后一个普通 tool schema | `ephemeral` |
| 消息倒数第三块 | `messages[-3]` 的最后一个可缓存块 | `ephemeral` |
| 消息最后一块 | `messages[-1]` 的最后一个可缓存块 | `ephemeral` |

- 可缓存块类型白名单：thinking 块不标（缓存行为未确认）。
- 动态区（boundary 之后：环境/时间/接口能力）不标缓存，避免会话特定内容打断前缀。
- 服务端工具（带自身 cache_control 的）不参与 tools 断点。

## 三、开关

`ResolvedModel.cache_control`：

| 值 | Anthropic | OpenAI 兼容网关 |
|---|---|---|
| `auto`（默认） | 启用 | 不启用（标准 OpenAI 用隐式前缀缓存） |
| `true` | 启用 | 启用（对支持 `cache_control` 的网关，如 OpenRouter Anthropic 模型） |
| `false` | 禁用 | 禁用 |

非官方端点（中转/网关）不识别 `cache_control` 会直接 400，通过 `cache_control: "false"` 关闭。

## 四、用量

`UsageData` 记录 `cache_read_tokens` / `cache_write_tokens`（Anthropic 字段直读；
OpenAI 兼容网关的 cached tokens 字段归一化）。前端上下文面板据此显示命中率。

## 五、稳定前缀的工程约束

1. system 静态区必须放在 `DYNAMIC_BOUNDARY` 之前（见 `system-prompt-design.md`）。
2. 工具顺序按角色白名单固定，避免每次请求重排导致 tools 断点漂移。
3. 活跃轮次内压缩/记忆注入尽量发生在边界处，减少中途改写历史前缀。
