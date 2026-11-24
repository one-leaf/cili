# LLM 供应商 API 参考

> 按当前实现重写（2026-11-25）。代码：`core/llm/server_presets.py`、`core/llm/chatgpt_oauth/`、
> `core/llm/deepseek_capabilities.py`、`core/llm/model_catalog.py`、`core/llm/discovery.py`。

## 一、协议

| 协议 | 默认路径 | 适配器 |
|---|---|---|
| `anthropic` | `/v1/messages` | `AnthropicAdapter` |
| `openai` | `/v1/chat/completions` | `OpenAIAdapter`（含 `openai-compatible` 别名） |
| `openai-responses` | `/v1/responses` | `OpenAIResponsesAdapter` |

Base URL 与路径拼接由 `adapter.join_api_url` 处理（base 自带 `/v1` 去重）。
供应商路径不在默认位置时用 `api_paths` 覆盖（见预设表）。

## 二、预设（`server_presets.PRESETS`）

| 键 | 组 | 默认协议 | Base URL（anthropic） | 认证 |
|---|---|---|---|---|
| `custom` | direct | — | 用户填写 | auto |
| `anthropic` | direct | anthropic | `https://api.anthropic.com` | auto |
| `openai` | direct | openai | —（openai: `https://api.openai.com`） | auto |
| `openrouter` | direct | openai | `https://openrouter.ai/api` | auto |
| `litellm` | direct | openai | `http://localhost:4000` | bearer |
| `deepseek` | direct | anthropic | `https://api.deepseek.com/anthropic` | auto |
| `chatgpt-oauth` | plan | openai-responses | —（`https://api.openai.com/v1`） | oauth |
| `opencode-go` | plan | anthropic | `https://opencode.ai/inference/go/anthropic` | auto |
| `qwen-token-plan` | plan | anthropic | `https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic` | bearer |
| `qianfan-coding` | plan | anthropic | `https://qianfan.baidubce.com/anthropic/coding` | bearer |
| `volcengine-coding` | plan | anthropic | `https://ark.cn-beijing.volces.com/api/coding` | bearer |
| `glm-coding` | plan | anthropic | `https://open.bigmodel.cn/api/anthropic` | bearer |
| `kimi-coding` | plan | anthropic | `https://api.kimi.com/coding` | bearer |
| `minimax-plan` | plan | anthropic | `https://api.minimax.cn/anthropic` | bearer |
| `tencent-token-plan` | plan | anthropic | `https://tokenhub.tencentmaas.com/plan/anthropic` | bearer |
| `xfyun-coding` | plan | anthropic | `https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic` | bearer |
| `stepfun-step-plan` | plan | anthropic | `https://api.stepfun.com/step_plan` | bearer |
| `xiaomi-token-plan` | plan | anthropic | `https://token-plan-cn.xiaomimimo.com/anthropic` | bearer |

补充字段：

- `deepseek`：openai/openai-responses 的 base 为 `https://api.deepseek.com`，responses 路径 `/responses`。
- `chatgpt-oauth`：仅 `openai-responses`；会话头 `session-id: {session_id}`；
  模型清单 `https://api.openai.com/v1/models`；端点与协议由代码锁定。
- `openrouter`：`x-session-id: {session_id}`。

预设值仅供参考：用户在设置页可覆盖 base_url/api_path/headers。

## 三、认证类型（`auth_type`）

| 值 | 头 |
|---|---|
| `auto` | anthropic → `x-api-key` + `anthropic-version`；openai → `Authorization: Bearer` |
| `bearer` | `Authorization: Bearer <api_key>` |
| `x-api-key` | `x-api-key` |
| `none` | 不加认证头 |
| `oauth` | `chatgpt-oauth` 凭证（见下） |

静态头来自预设 `headers`；会话头来自预设 `session_headers`（`{session_id}` 占位），
显式 headers 同名覆盖。

## 四、ChatGPT OAuth

- 登录：设备/回环回调（`login.py`），凭证存 `data/cili/tools/chatgpt/accounts/{id}.json`，
  凭证标识前缀 `chatgpt-oauth:`。
- 请求整形：`profile.shape_request`（预览期字段、`prompt_cache_key` 等）。
- 端点锁定：`assert_oauth_endpoint`（协议必须 `openai-responses`，base 必须官方域名）。
- 刷新：`manager` 缓存与单飞；`auth.refresh` 失败分 fatal/transient；
  错误码映射见 `chatgpt_oauth/errors.py`（套餐额度/区域/授权失效）。

## 五、DeepSeek 服务端联网工具

- 工具：`deepseek_web_search` / `deepseek_read_url`（客户端包装服务端工具调用）。
- 门控：`deepseek_capabilities.tools_visible(model)`——协议必须 anthropic、模型属于 DeepSeek；
  上游不支持时 `mark_unsupported` 记进程级禁用集合，后续不再下发。
- 识别：`SERVER_TOOL_NAMES`（`core.tools.deepseek_web`）；工具列表中带 `cache_control` 的
  服务端工具不参与本地缓存断点。

## 六、模型清单

- 探测路由 `POST /api/config/detect-models`：
  - anthropic：`GET {base}/v1/models?limit=100`（跟随 `has_more/last_id`）；
  - openai：`GET {base}/models`；
  - OAuth：`GET https://api.openai.com/v1/models`（401 刷新一次）。
- 解析：`model_catalog.parse_models_payload` 同时支持 OpenAI 形状 `{data:[{id}]}` 与
  ChatGPT 目录 `{models:[{slug,visibility}]}`。
- 拉取：`discovery.fetch_model_ids`。

## 七、错误与重试要点

- 429 细分为限流（重试）与配额/余额（不重试）；`retry-after` 头与响应体提示均被识别。
- 5xx/连接错误按退避重试；`CILI_LLM_TOTAL_RETRY_TIMEOUT` 控制总时长（默认 300s）。
- 413（请求体过大）触发去图重试（Anthropic）与前端清屏哨兵。
- 分类与文案见 `core/llm/errors.py`（含 `chatgpt_oauth/errors.lookup_error`）。
