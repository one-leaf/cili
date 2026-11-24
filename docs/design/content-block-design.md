# LLM 内容块与适配层设计

> 按当前实现重写（2026-11-25）。代码：`core/llm/`。

## 一、包结构

| 文件 | 职责 |
|---|---|
| `types.py` | 内容块、Message、UsageData、StreamChunk、LLMResponse、块互转帮手 |
| `adapter.py` | `Adapter` 抽象基类（序列化/解析/流翻译/缓存钩子/头构造/URL 拼接） |
| `anthropic.py` | `AnthropicAdapter`（Messages API；Anthropic 事件流） |
| `openai.py` | `OpenAIAdapter`（Chat Completions；兼容第三方网关） |
| `openai_responses.py` | `OpenAIResponsesAdapter`（Responses API；input items/reasoning 回放） |
| `transport.py` | `HttpTransport`（httpx、SSE 解析、重试、重试超时） |
| `assembler.py` | `BlockAssembler`（把 StreamChunk 累积为 ContentBlock） |
| `client.py` | `LLMClient` 与 `create_llm_client`（按协议选适配器） |
| `errors.py` | 错误分类 `classify_llm_error` / `format_llm_error` |
| `chatgpt_oauth/` | Sign in with ChatGPT：auth/credentials/errors/login/manager/profile/store |
| `server_presets.py` | 供应商预设（base_url/api_path/鉴权头/会话头/模型清单端点） |
| `deepseek_capabilities.py` | DeepSeek 服务端联网工具门控（含进程级降级集合） |
| `model_catalog.py` | `/v1/models` 与 ChatGPT 目录解析（供应商无关） |
| `discovery.py` | 模型列表拉取（anthropic 分页 / openai 单页） |

## 二、内容块

`ContentBlock = TextBlock | ReasoningBlock | ImageBlock | ToolCallBlock | ToolResultBlock | ServerBlock`

| 块 | 关键字段 | 说明 |
|---|---|---|
| `TextBlock` | `text` | 正文 |
| `ReasoningBlock` | `text`、`signature`、`redacted` | 思考/推理回放（Anthropic thinking / OpenAI reasoning） |
| `ImageBlock` | `data`(base64)/`path`/`media_type` | 多模态图片；历史按图片策略降级为路径引用 |
| `ToolCallBlock` | `id`、`name`、`input`、`parse_arguments()` | 工具调用 |
| `ToolResultBlock` | `tool_use_id`、`content`、`is_error` | 工具结果（可含图片块） |
| `ServerBlock` | `type` ∈ `SERVER_BLOCK_TYPES` | 服务端工具块（web_search 等），原样回放 |

块级互转：`block_to_dict / block_from_dict / blocks_to_dicts / blocks_from_dicts`
（供会话持久化与投影）。

## 三、Message

```python
Message(role, content: str | list[ContentBlock])  # from_dict / to_dict
```

- 角色：system / user / assistant / tool（工具结果以 user 消息内的 `ToolResultBlock` 表达）。
- 适配器负责把中立 Message 序列化为协议格式；供应商差异（如 OpenAI 的 `tool_calls`、
  Responses 的 `input` items）全部封装在各自 Adapter 内。

## 四、流式协议

`StreamChunk`（统一流协议）：`block_start / text_delta / reasoning_delta / tool_call_delta /
block_end / usage / finish`。

- Adapter `translate_stream(events)` 把供应商 SSE 事件翻译为 StreamChunk。
- `BlockAssembler` 累积为最终 `ContentBlock` 列表。
- `HttpTransport` 负责 SSE 行解析与错误事件（`StreamErrorEvent`）。

## 五、Adapter 契约

```python
class Adapter(ABC):
    @property
    def cache_enabled(self) -> bool: ...        # 协议/网关是否启用 prompt cache
    def apply_prompt_cache(self, body, system, messages, tools=None) -> None: ...
    def build_headers(self, session_id: str = "") -> dict[str, str]: ...
    def serialize(self, ...) -> dict: ...
    def parse_response(self, data) -> tuple[list[ContentBlock], stop_reason, UsageData]: ...
    def translate_stream(self, events) -> Iterable[StreamChunk]: ...
```

- 缓存钩子由基类声明、子类按协议实现；不支持缓存的适配器保持默认 no-op。
- `join_api_url(base_url, path)` 统一 base 与路径拼装（自带 `/v1` 去重）。
- 静态头/会话头来自 `server_presets.get_preset(backend)`；OAuth provider 的端点与协议由代码锁定
  （`chatgpt_oauth.assert_oauth_endpoint`）。

## 六、LLMClient

`create_llm_client(config: ResolvedModel) -> LLMClient` 按 `config.protocol` 选择适配器：
`anthropic` / `openai`（含 `openai-compatible` 别名）/ `openai-responses`。

公共 API：

| 方法 | 说明 |
|---|---|
| `chat(messages, system=, tools=, max_tokens=, session_id=)` | 非流式，返回 `LLMResponse` |
| `chat_stream(...)` | 流式，逐块产出 `StreamChunk` |
| `chat_structured(messages, system, output_schema, max_tokens=)` | 结构化输出（解析/修复/截断重试） |

`LLMResponse`：`content`、`stop_reason`、`usage`、`raw`。

重试：`HttpTransport.with_retry`（429/5xx/连接错误；`retry-after` 与响应体提示；
`CILI_LLM_TOTAL_RETRY_TIMEOUT` 控制总时长，默认 300s）。

## 七、错误分类

`core/llm/errors.py`：

- `classify_llm_error(e) -> LLMErrorInfo(kind, message, should_retry, retry_after)`
- `kind`：`timeout / connection / auth / quota / rate_limit / server / context_length /
  bad_request / unknown`
- 429 细分：配额类错误码不重试；ChatGPT 套餐额度码在 `chatgpt_oauth/errors.lookup_error`。
- 用户可见文案统一中文；`format_llm_error` 生成最终提示。

## 八、配置与模型解析

模型配置两级（`core/config.py`）：`providers`（接入层 + 模型表）与 `agents`（角色 → 模型引用）。
`resolve_model(ref) -> ResolvedModel` 合成运行时对象（protocol/base_url/auth_type/backend/headers/
context/output 上限/image 与 reasoning 设置）。环境变量覆盖只作用于 master 接入层
（`apply_env_overrides`）。

## 九、ChatGPT OAuth（`chatgpt_oauth/`）

- 登录：`login.start/poll/complete/cancel`，授权回调由 `web/routes_chatgpt_oauth.register_hooks` 注册。
- 凭证：`credentials.Credentials`（scrypt 无关，OAuth token 集）+ `store`（每账号一文件，
  `data/cili/tools/chatgpt/accounts/{id}.json`）；凭证前缀 `chatgpt-oauth:`。
- 请求整形：`profile.shape_request`（预览期字段与顺序）；`assert_oauth_endpoint` 锁定端点。
- 错误：`errors.lookup_error` 映射套餐额度码；刷新失败分 fatal/transient。
