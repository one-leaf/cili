# MCP 工作区化设计

> 按当前实现整理（2026-11-28）。代码：`core/mcp_client.py`、`core/mcp_store.py`、
> `core/mcp_naming.py`、`core/tools/mcp.py`、`core/config.py`（`MCPConfig`）、
> `web/routes_config.py`（MCP 端点）、`web/static/mcp-settings.js`。

## 一、定位

MCP（Model Context Protocol）服务器按**工作区隔离**：每个工作区一份配置，连接与工具列表
只对当前工作区可见；工作区目录复制/迁移时配置随目录带走。

只支持 **streamableHttp** 传输（`mcp` SDK），不支持 stdio；MCP 是可选能力，
连接失败降级为「本次无 MCP 工具」，不阻断会话。

## 二、配置

目录：`{workspace}/.cili/mcp/{server}.json`，每个服务器一个文件，文件名即服务器名。

```json
{
  "url": "https://example.com/mcp",
  "headers": {"Authorization": "Bearer ..."},
  "tool_timeout": 30,
  "enabled_tools": ["*"]
}
```

- 服务器名校验：`^[A-Za-z0-9_][A-Za-z0-9_.-]*$`，≤64，不能以符号开头（防路径穿越）；
- 单个文件读取失败只告警，不影响其他服务器；
- `headers` 可放 Bearer / API Key；保存到磁盘，前端回显时掩码。

## 三、连接管理（`MCPProvider`）

- 进程级单例，后台线程跑常驻 asyncio loop（与 BrowserService 同范式）；
- 连接表的键是 `(workspace_uuid, server_name)`，`get_wrappers(workspace_uuid)` 只返回本工作区；
- **懒连接**：runner 创建或设置页查询时 `ensure_connected(workspace)`；配置签名未变则 no-op；
- 并发连接进程级串行化；部分服务器失败时保留旧签名，下次重试；
- 设置页查询用 `ensure_connected_async` 后台预热，HTTP 不阻塞；
- 状态：`connecting / connected / failed / offline`，附 `tool_count` 与工具清单；
- `reload(force=True)` 强制重连；`test_connect` 临时连接并枚举工具，测完即断开，不改变全局状态。

## 四、工具暴露

- 工具名：`mcp_{server}_{tool}`，非法字符替换、连续下划线压缩、≤64 字符；超长用 sha1 后缀；
- `enabled_tools` 白名单：`["*"]` 全部；否则匹配原始工具名或包装后的完整名；
- 参数 schema 规范化：`input_schema`（SDK 2.x）与 `inputSchema`（1.x）兼容；
  处理 `anyOf/oneOf` 的 nullable 分支、重写本地 `$ref`，确保模型 API 可用；
- 工具列表按名字排序：连接完成顺序不稳定，排序保证 system prompt/tools 前缀字节一致（prompt cache）；
- `MCPToolWrapper` 只持 provider + 工作区，不持 session；每次执行经 provider 取当前连接，
  服务器重连后自动使用新 session。

## 五、执行语义

- 调用超时：`tool_timeout`（默认 30 秒），外层再加 10 秒等待余量；
- 临时错误（连接类）自动重试一次，重试前重新取 session；超时/永久错误直接返回错误；
- MCP 返回按 content blocks 渲染为文本（图片只给字节数占位）；
- 结果按**外部不可信数据**包装（`<<< 外部不可信数据 >>>`）；大结果由工具输出外置机制落盘，
  可用 `read_tool_result` 分段读取；
- 青少年模式：`policy.mcp_tools=deny` 时拒绝（解锁窗口内放行）。

## 六、HTTP 与前端

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/mcp/servers?workspace=` | 配置 + 连接状态 + 工具清单（headers 掩码；触发后台预热） |
| PUT | `/api/mcp/servers` | 全量保存并重连；`_preserve_headers` 表示前端未回传密钥、沿用旧 headers；保存后刷新缓存 runner |
| POST | `/api/mcp/reload?workspace=` | 强制重连并刷新 runner |
| POST | `/api/mcp/test` | 临时测试连接（优先用请求体 config，否则按已保存配置） |

保存/重连带 `teen_guard("config_update")`；PUT 需 CSRF。设置页「MCP」页签按工作区维护，
支持新增/编辑/删除、连接状态与工具列表预览。

## 七、与其它子系统的关系

- **工作区**：配置随 `.cili/mcp/` 走；共享工作区的 MCP 配置由 Git remote-wins 同步；
- **权限**：MCP 工具不是 shell/路径目标，不进入 `PermissionResolver` 的命令/路径判定；
  除青少年模式外，注册即可用；
- **prompt cache**：工具名与顺序稳定，避免每次连接顺序不同导致缓存失效；
- **工具搜索**：MCP 工具属于 deferred tools，可通过 `tool_search` 激活。

## 八、测试

`test/test_mcp.py`、`test/test_mcp_store.py`。

## 九、已知边界

- 仅 streamableHttp；stdio、SSE（旧版）不接入；
- 连接是进程内长连接，无跨进程共享；服务重启后重新握手；
- 工具调用没有单工具审批粒度（仅青少年模式整体开关）；
- 服务器返回的 tool 描述/schema 直接进入 tools 前缀，异常 schema 由规范化层尽力修复。
