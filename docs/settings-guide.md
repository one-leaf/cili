# 设置指南

> 按当前实现重写（2026-11-25）。配置文件：`data/cili/setting.json`；工作区级设置在其
> `workspaces.json` 条目与 `{workspace}/.cili/` 下。

## 一、设置界面结构

- **全局设置**（齿轮）：模型列表（Providers）/ Agents / 系统参数 / 权限 / QQ / 微信 / 邮件 / 关于。
- **工作区设置**（工作区下拉旁）：基础信息 / 项目提示词 / Git 版本管理 / 教材下载。
- 青少年模式面板在全局设置内（开启后按策略灰显受限项）。

## 二、模型配置（providers + agents）

两级结构：

```json
{
  "providers": {
    "default": {
      "protocol": "anthropic",
      "api_key": "sk-...",
      "base_url": "https://api.anthropic.com",
      "auth_type": "auto",
      "backend": "anthropic",
      "headers": {},
      "models": {
        "main": {"name": "claude-fable-5-2", "max_output_tokens": 32768, "max_context_tokens": 200000}
      }
    }
  },
  "agents": {"master": {"model": "default/main"}}
}
```

- `protocol`：`anthropic` / `openai` / `openai-responses`（旧键 `interface_type` 由迁移改写）。
- `backend`：供应商预设键（见 `docs/design/llm-api-reference.md`），决定默认 URL/头/会话头。
- `auth_type`：`auto` / `bearer` / `x-api-key` / `none` / `oauth`。
- 模型条目：`name`（真实模型 ID，空=用键名）、输出/上下文上限、`model_type`（reasoning/chat）、
  `input`（text/image）、`reasoning` 档位、`cache_control`（auto/true/false）。
- Agents：角色 → `"provider/模型键"`；worker/lite 未配置时继承 master。
- 设置页提供「测试」「探测模型」：OAuth 走 ChatGPT 登录流程。

## 三、系统参数（`system`）

| 键 | 默认 | 说明 |
|---|---|---|
| `pip_mirror` | 华为云镜像 | pip 安装源 |
| `allowed_ips` | `[]` | 未配置令牌时的 IP 白名单（支持 CIDR） |
| `browser_path` | 自动探测 | Edge/Chrome 路径 |
| `search_engine` | `bing` | web_search 引擎 |
| `mineru_api_key` | 空 | PDF→Markdown（MinerU） |
| `max_iterations` | 200 | 自主模式最大迭代 |
| `max_concurrent_agents` | 2 | 并行子代理上限 |
| `access_token` | 空 | 配置后所有请求必须携带令牌 |
| `parallel_tools` | true | 并发安全工具并行执行 |

## 四、权限（顶层 `permissions`）

```json
{
  "permissions": {
    "enabled": true,
    "default_mode": "allow",
    "readonly_bypass": true,
    "outside": {"read": "allow", "write": "ask", "delete": "ask", "navigate": "ai"},
    "rules_hot_reload": true,
    "judge": {"enabled": true, "timeout_seconds": 60, "on_error": "ask",
              "persist_allow": true, "cache": "session", "max_consecutive_failures": 3},
    "approval": {"timeout_seconds": 300, "auto_resume": "web"}
  }
}
```

- 规则（allow/ask/deny + 工具范围）保存在全局/工作区 `approval_rules.json`；
  优先级 `deny > ask > allow`；只读白名单免判定。
- `outside`：工作区外读默认放行，写/删默认询问；`navigate` 可为 `ai`（判定器裁决）。
- `judge`：lite 模型判定器；`approval`：审批卡超时与自动恢复（web/all/none；
  `all` 仅覆盖有推送通道的 web/cli，不含 IM）。

## 五、图片（顶层 `images`）

| 键 | 默认 | 说明 |
|---|---|---|
| `history_mode` | `current_turn` | 历史图片只保留当前回合内联，其余降级为引用 |
| `max_dimension` | 4096 | 入站图片归一化上限 |
| `request_image_max_bytes` | 2MB | 单张请求图片目标预算 |
| `max_request_image_bytes` | 20MB | 请求图片总预算 |
| `max_images_per_request` | 100 | 单请求图片数上限 |
| `max_images_per_tool_result` | 8 | 工具结果图片上限 |
| `jpeg_quality` | 85 | 重编码质量 |

## 六、接入端配置

### QQ（`qq.accounts[]`）

`id` / `enabled` / `app_id` / `secret` / `msg_format`（markdown|text）/ `session_mode` /
`session_locked` / 节流参数（flush_interval_seconds、flush_max_wait_seconds）。
凭据只由扫码绑定写入；接口返回不含 secret；表单留空/掩码 = 保留原值。

### 微信（`wechat.accounts[]`）

`id` / `enabled` / `session_mode` / `session_locked` / `context_message_budget`（默认 8）/
`context_token_max_age_seconds`（默认 60）。凭证在 `data/cili/tools/wechat/accounts/{id}.json`。

### 邮件（`email.accounts[]`）

`id`（= 邮箱地址）/ `email_address` / `password` / `imap_host`/`port`/`security` /
`smtp_host`/`port`/`security` / `allowed_senders`（**默认拒绝**）/ `enabled` /
`session_mode` / 额度与体积参数。

## 七、MCP（`mcp_servers`）

按服务器名配置命令/传输/环境/请求头；设置页可查看工具清单、测试连接、重连。

## 八、工作区设置

- 基础信息：名称、目录（目录必须是已存在且可写）。
- 项目提示词：优先级 AGENTS.md > agent.md > CLAUDE.md > claude.md；可在界面编辑或
  一键生成（后台 worker 扫描）。
- Git 版本管理：启用后回合结束自动提交并可同步远端；支持拉取/推送与提交历史。
- 教材下载：按学段/学科/版本/年级选择官方教材，后台下载并生成 Markdown。

## 九、青少年模式

家长密码开关、解锁窗口（5/15/30/60 分钟）、策略（隐藏/分享/记忆删除/文件删除/越界/
白名单/审批 strict/MCP/浏览器上传/cron/工作流/提示词块）、每日时长与使用时段、
审计与使用报告。硬禁动作（会话删除/消息撤销/执行日志删除/工作区注销）解锁也不放行。

## 十、关于与升级

查看版本、检查更新、下载并应用升级（应用后重启）；服务端可配置
`access_token` 与 `allowed_ips` 控制远程访问。
