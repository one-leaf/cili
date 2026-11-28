# DeepSeek 服务端联网工具设计

> 按当前实现整理（2026-11-28）。代码：`core/tools/deepseek_web.py`、
> `core/llm/deepseek_capabilities.py`、`core/tools/registry.py`（按模型可见性注册）。

## 一、背景

DeepSeek 的 `deepseek_web_search` / `deepseek_read_url` 是**服务端工具**：搜索与抓取在
DeepSeek 侧执行，结果以 `server_tool_use` / `*_tool_result` 内容块随响应返回，不是普通
客户端函数调用。cili 把它们包装成普通客户端工具，避免服务端块进入主对话历史
（持久化、回放、压缩、跨模型切换都不需要特殊处理）。

## 二、包装方式

每次工具调用内部再发一条**辅助 Messages 请求**：

- 只声明一个服务端工具（`{"type": server_type, "name": server_type}`），不声明任何客户端工具；
- 固定 `reasoning="off"`：只消费摘要文本，不触发深度搜索；
- `max_tokens=1024`；系统提示要求“必须使用该工具，再简要总结”；
- 解析响应：`web_search_tool_result`（结果数组或错误对象）、`web_fetch_tool_result`
  （页面内容/错误码）、`TextBlock` 摘要；
- 返回给主模型的是普通 `tool_result`，内容按外部不可信数据包装；主历史里不出现服务端块。

代价：每次工具调用多一次模型往返（与浏览器搜索类工具相比，省去本地渲染/抓取）。

## 三、工具

| 工具 | 参数 | 返回 |
|---|---|---|
| `deepseek_web_search` | `query` | 结果标题/URL/摘要（最多 30 条、摘要 ≤500 字）+ 服务端总结 |
| `deepseek_read_url` | `url`（必须是绝对 http(s) URL） | 页面 URL、标题 + 服务端摘要 |

结果在返回前按不可信数据标记包装；搜不到/读不到时返回错误结果，不虚构内容。

## 四、可见性与降级

`core/llm/deepseek_capabilities.py`：

- `is_deepseek_model(model)`：`backend=deepseek`、模型名含 `deepseek`、或 host 为
  `api.deepseek.com` / `*.deepseek.com`；
- `tools_visible(model)`：必须是 DeepSeek 模型且协议为 `anthropic`；
- 上游没有能力查询接口，只能“先试后禁”：工具收到
  `unsupported server tool` / `Access denied for web search` 时 `mark_unsupported(model_name)`，
  记入**进程级**禁用集合（重启后重新探测）；随后回调 runner 重算工具可见性，后续轮次不再下发。

runner 在 `_eligible_tools` 里按当前模型过滤；模型切换时重建工具并重新接线
`model_provider` 与 `capability_missing_handler`。

## 五、工具注册

- 两个工具在角色 JSON 白名单中（master/worker），但注册时按 `tools_visible` 过滤；
- 非 DeepSeek/非 anthropic 协议下模型看不到这两个工具，退回 `web_search` / `browser`；
- 工具本身 `concurrency_safe=true`，但每次调用会创建并关闭临时 LLM 客户端。

## 六、错误处理

| 情况 | 行为 |
|---|---|
| 无可用模型 | 错误结果 “no model is available” |
| 当前模型不可见 | 错误结果，提示切换模型或改用 web_search/browser |
| 能力缺失标记命中 | 记入禁用集合 + 刷新可见性 + 明确错误 |
| 其他请求失败 | 错误结果，附最多 800 字异常/响应体，不打断回合 |

## 七、测试

`test/test_deepseek_web_tools.py`。

## 八、已知边界

- 只有部分 DeepSeek 模型/端点支持服务端联网；能力探测是“试错 + 进程级缓存”；
- 辅助请求额外消耗一次模型调用与 token；
- 返回的是服务端摘要/片段，不是原始页面全文；需要全文时用 `browser` 或 `deepseek_read_url`；
- 服务端块不会进入主历史，因此跨模型切换、压缩、回放无需清洗。
