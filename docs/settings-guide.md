# 设置说明（setting.json）

Cili Agent 的所有配置都保存在 `data/cili/setting.json` 文件中。

首次使用时，可以将 `data/cili/setting.example.json` 复制为 `setting.json`，然后根据自己的情况修改。

本文档面向零基础用户，逐项解释每个配置的含义和用法。

---

## 文件位置

```
Cili Agent/
  data/
    cili/
      setting.json          ← 就是这个文件
      setting.example.json  ← 示例配置，可以复制后改名使用
```

也可以通过 Web UI 的 **设置页面** 来修改，效果是一样的。

---

## 整体结构

`setting.json` 是一个 JSON 格式的文件，内容分为四大块：

```json
{
  "providers": { ... },       // 模型提供商：凭据（API Key / Base URL / 协议）+ 模型表
  "agents": { ... },          // 角色分配：master / worker / lite 各用哪个模型
  "system": { ... },          // 系统参数配置
  "mcp_servers": { ... }      // MCP 服务器配置（可选）
}
```

模型配置是两级结构：**providers 管凭据和模型清单**，**agents 决定每个角色用哪个模型**。换 API Key 只改一处，多个模型共享同一凭据。

---

## 一、模型提供商（providers）

每个 provider 是一个"接入层"：API 地址、密钥、协议，外加它名下的模型清单。

```json
{
  "providers": {
    "default": {
      "interface_type": "anthropic",
      "api_key": "sk-xxx",
      "auth_type": "auto",
      "backend": "litellm",
      "base_url": "http://ai.example.com/llm",
      "headers": {},
      "models": {
        "main": {
          "name": "ac/qwen3.7-plus",
          "max_output_tokens": 36000,
          "max_context_tokens": 256000,
          "model_type": "reasoning",
          "input": ["text", "image"],
          "reasoning": "default",
          "cache_control": "auto"
        },
        "cheap": { "name": "ac/qwen3.7-flash", "max_output_tokens": 8192 }
      }
    }
  }
}
```

- **键名**（上面的 `default`、`main`、`cheap`）是自由命名的别名，agents 里用 `"provider键/模型键"` 引用（如 `"default/main"`）。
- 模型条目里 **`name` 省略时用键名当作发给 API 的模型 ID**——直连官方 API 时直接拿模型 ID 当键名即可，不用重复写。

### interface_type — 接口协议

| 值 | 说明 |
|----|------|
| `anthropic` | Anthropic Messages API（`/v1/messages`），默认值 |
| `openai` | OpenAI Chat Completions API（`/v1/chat/completions`） |

### api_key — API 密钥

支持两种写法：

- **明文**：直接写 `"sk-xxx"`
- **环境变量引用**：写成 `"${MY_API_KEY}"`，启动时展开。变量未设置时**启动失败并报错**（不会静默用空 key）。密钥不落盘，推荐这种方式。

### auth_type — 认证头方式

请求时自动补充的认证头，**显式 `headers` 里的同名头会覆盖它**：

| 值 | 行为 |
|----|------|
| `auto`（默认） | 按协议：anthropic → `x-api-key`；openai → `Authorization: Bearer` |
| `bearer` | 强制 `Authorization: Bearer <key>`（LiteLLM 等 OpenAI 兼容网关） |
| `x-api-key` | 强制 `x-api-key: <key>` |
| `none` | 不自动加认证头，用 `headers` 手动给 |

### backend — 后端服务器类型

自动填充会话标识头（网关按会话做粘性路由用）。每个预设填充的头：

| 值 | 自动填充的头 |
|----|-------------|
| `custom`（默认） | 不填充，`headers` 手写 |
| `codex` | `session-id` |
| `claude-code` | `x-claude-code-session-id` |
| `opencode` | `x-opencode-session` |
| `openrouter` | `x-session-id` |
| `litellm` | `x-litellm-session-id` |

头的值是当前会话 ID（每请求携带，无会话时省略）。`headers` 里也可以用 `{session_id}` 占位符手写任意会话头，显式值覆盖预设。

### base_url — API 地址

默认 `https://api.anthropic.com`。走聚合网关时填网关地址。

### headers — 附加请求头

JSON 对象。值支持 `{session_id}` 占位符（请求时展开为当前会话 ID，无会话时该头省略）：

```json
"headers": { "X-Session-ID": "{session_id}", "X-Extra": "static-value" }
```

### models — 模型表

每个条目的字段（除 `name` 外全部可选）：

#### name — 模型 ID

发给 API 的真实模型 ID。**省略时用条目键名**。

#### max_output_tokens — 最大输出

单次响应最大输出 token，默认 **8192**。

#### max_context_tokens — 上下文窗口

模型上下文窗口大小，默认 **128000**。用于上下文长度计算与压缩触发。

#### model_type — 模型类型

| 值 | 说明 |
|----|------|
| `reasoning`（默认） | 推理模型，思考选项可选 缺省/关闭/低/中/高 |
| `chat` | 非推理模型，无思考设置，请求不携带任何思考参数 |

#### input — 输入模态

勾选式数组：`text` / `image` / `audio` / `video` / `pdf` 的子集，默认 `["text", "image"]`，必须含 `text`。决定图片附件是转成图像块还是退化为路径文本。

#### reasoning — 思考档位

仅 `model_type="reasoning"` 时有效，默认 `default`（不发送思考参数，由 API 默认行为决定）。取值域按协议：

| 协议 | 取值 | 发送行为 |
|------|------|---------|
| anthropic | `default` / `off` / `low` / `medium` / `high` | low/medium/high → `thinking.budget_tokens` 1024/4096/10000；default/off 不发送 |
| openai | `default` / `none` / `minimal` / `low` / `medium` / `high` | 除 default 外 `reasoning_effort` 直传（none/minimal 需模型支持，不支持时等同缺省） |

#### cache_control — 缓存控制

| 值 | 说明 |
|----|------|
| `auto`（默认） | Anthropic 协议启用 prompt cache，OpenAI 协议禁用 |
| `true` | 强制启用（不支持的网关会失败） |
| `false` | 禁用 |

---

## 二、角色分配（agents）

每个角色引用 providers 里的一个模型：

```json
{
  "agents": {
    "master": { "model": "default/main" },
    "worker": { "model": "default/main" },
    "lite":   { "model": "default/cheap" }
  }
}
```

| 角色 | 职责 | 省略时 |
|------|------|--------|
| `master` | 日常对话与任务执行的主模型 | **必选**（`agents` 整个省略且全局只有一个可用模型时自动兜底） |
| `worker` | 委派子代理（完整工具集 + 检查阶段） | 继承 master |
| `lite` | 轻量子代理（最小工具集），记忆整合也用它 | 继承 master |

- 引用格式：`"provider键/模型键"`，启动时校验，引用不存在会**启动失败并列出全部可用引用**（fail-fast，不静默回退）。
- Worker/Lite 省略时继承 master——改 master 的模型，未显式配置的角色自动跟随。
- 环境变量 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` 与 CLI `--model` 只覆盖 **master** 的接入层与模型 ID，不影响 worker/lite。

## 三、系统参数配置（system）

这一块配置的是 Agent 运行环境相关的参数，和 AI 模型无关。

```json
"system": {
  "pip_mirror": "https://repo.huaweicloud.com/repository/pypi/simple/",
  "browser_path": "",
  "search_engine": "bing",
  "allowed_ips": [],
  "access_token": "",
  "mineru_api_key": "",
  "max_iterations": 200,
  "max_concurrent_agents": 5
}
```

### pip_mirror — Python 包安装源

**作用**：Agent 在执行 Python 脚本时，如果需要安装第三方包，会从这个地址下载。

**默认值**：`https://repo.huaweicloud.com/repository/pypi/simple/`（华为云镜像，国内速度快）

**常见镜像源**：

| 镜像源 | 地址 | 特点 |
|--------|------|------|
| 华为云（默认） | `https://repo.huaweicloud.com/repository/pypi/simple/` | 国内快，稳定 |
| 阿里云 | `https://mirrors.aliyun.com/pypi/simple/` | 国内快 |
| 腾讯云 | `https://mirrors.cloud.tencent.com/pypi/simple/` | 国内快 |
| 清华 | `https://pypi.tuna.tsinghua.edu.cn/simple/` | 国内快 |
| 官方源 | `https://pypi.org/simple/` | 国外，国内较慢 |

**怎么选择**：

- 如果你在中国大陆 → 用国内镜像（默认就是，不用改）
- 如果你在国外 → 改成官方源 `https://pypi.org/simple/`
- 如果安装 Python 包时速度很慢或超时 → 换一个镜像源试试

**留空**：如果留空 `""`，则使用 pip 默认源。

---

### browser_path — 浏览器路径

**作用**：指定 Agent 用来浏览网页、截图的浏览器程序的位置。

**默认值**：`""`（空字符串，表示自动检测）

**自动检测顺序**（当值为空时）：

1. Microsoft Edge（Program Files x86）
2. Microsoft Edge（Program Files）
3. Microsoft Edge（用户目录）
4. Google Chrome（Program Files）
5. Google Chrome（Program Files x86）
6. Google Chrome（用户目录）

**什么时候需要手动填写**：

- 自动检测找不到你的浏览器（比如装在非标准位置）
- 你想让 Agent 用特定的浏览器

**怎么填写**：

填浏览器可执行文件的完整路径，用双反斜杠 `\\` 分隔（JSON 格式要求）：

```json
// Windows 示例
"browser_path": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"

// macOS 示例
"browser_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

// Linux 示例
"browser_path": "/usr/bin/google-chrome"
```

**怎么找到自己的浏览器路径**：

1. 在桌面或开始菜单找到浏览器图标
2. 右键 → 属性（Windows）或 显示包内容（macOS）
3. 复制"目标"或"位置"路径

---

### search_engine — 搜索引擎

**作用**：Agent 搜索互联网时使用哪个搜索引擎。

**可选值**：

| 值 | 搜索引擎 |
|-----|---------|
| `"bing"` | 必应搜索（**默认**） |
| `"google"` | 谷歌搜索 |

**建议**：

- 在中国大陆 → 用 `"bing"`（可以正常访问）
- 如果能访问 Google → 可以改成 `"google"`

---

### allowed_ips — 允许访问的 IP 列表

**作用**：控制哪些 IP 地址可以访问 Agent 的 Web 界面。相当于一个简单的访问控制。

**默认值**：`[]`（空数组，表示只允许本机访问）

**说明**：

- `[]` — 只有本机能访问（127.0.0.1 / localhost），最安全
- `["192.168.1.100"]` — 允许指定 IP 访问
- `["192.168.1.0/24"]` — 允许整个网段访问

**使用场景**：

如果你想从局域网内其他电脑（比如手机、平板）访问 Agent 的 Web 界面，就需要把那些设备的 IP 加进来。

**安全提醒**：

- ⚠️ Agent 没有用户认证机制，任何能访问的人都可以操作
- ⚠️ 不要把它暴露到公网（不要填 `["0.0.0.0"]` 或 `["*"]`）
- ⚠️ 仅在受信任的局域网环境中使用

---

### access_token — 访问令牌

**作用**：Web 界面的访问令牌。配置后，访问 Agent 的 Web 请求需要携带该令牌才能通过。

**默认值**：`""`（空字符串，默认不启用）

**说明**：

- 留空 `""`：默认不启用访问控制
- 当服务绑定到非 localhost 地址（如通过 `allowed_ips` 允许局域网访问）且未配置 `access_token` 时，Agent 会**拒绝启动**，强制要求设置令牌以保证安全
- 配置后，Web 请求需携带该令牌才能访问

---

### mineru_api_key — MinerU 文档解析 API 密钥

**作用**：用于高精度文档（PDF、Word、PPT 等）转 Markdown 的 API 密钥。MinerU 是一个专业的文档解析服务，特别擅长处理含表格、公式、图片的复杂文档。

**默认值**：`""`（空字符串，表示不使用该功能）

**免费额度**：MinerU 目前**不收费**，暂无商业化收费计划，可放心注册使用。

#### 如何获取 API Key

1. 访问 [MinerU API 管理页面](https://mineru.net/apiManage/token)
2. 注册账号并登录
3. 创建 API Token
4. 将获得的 Token 填入 `mineru_api_key` 字段

#### MinerU 提供两种 API 模式

| 对比维度 | 🎯 精准解析 API | ⚡ Agent 轻量解析 API |
|---------|----------------|---------------------|
| 是否需要 Token | ✅ 需要（填在此处） | ❌ 无需（IP 限频） |
| 接口地址 | `/api/v4/extract/task` 或 `/api/v4/file-urls/batch` | `/api/v1/agent/parse/url` 或 `/api/v1/agent/parse/file` |
| 模型版本 | pipeline（默认）/ vlm(推荐) / MinerU-HTML | 固定 pipeline 轻量模型 |
| 文件大小限制 | ≤ 200MB | ≤ 10MB |
| 页数限制 | ≤ 200 页 | ≤ 20 页 |
| 批量支持 | ✅ 支持（≤ 200 个） | ❌ 单文件 |
| 输出格式 | Zip 包（Markdown、JSON，可导出 docx/html/latex） | 仅 Markdown（CDN 链接） |
| 调用方式 | 异步（提交 → 轮询） | 异步（提交 → 轮询） |

**填写建议**：

- 如果你需要处理大文件、批量文件或高质量解析 → 注册并填写此处的 API Key
- 如果只是偶尔处理小文档 → 可以不填，Agent 会使用内置的普通解析方式

#### 使用限制（免费额度）

**提交任务接口**（创建解析任务、批量上传、URL 批量上传共用）：
- 频率限制：50 个文件 / 分钟
- 单用户每日上限：5000 个文件
- 其中 HTML 文件最多 100 个 / 天

**获取任务结果接口**（单个任务结果、批量任务结果共用）：
- 频率限制：1000 次 / 分钟

**温馨提示**：MinerU 保留了根据系统负载动态调整限流策略的权利。超出限制的请求会被系统拒绝。

#### 什么时候需要配置？

| 场景 | 是否需要配置 |
|------|------------|
| 处理普通纯文本 PDF | 不需要，内置解析够用 |
| 处理含复杂表格、公式的文档 | ✅ 建议配置 |
| 批量处理大量文档 | ✅ 建议配置 |
| 追求最佳解析质量 | ✅ 建议配置 |

---

### max_iterations — 最大工具调用次数

**作用**：限制 Agent 在单次会话中最多能调用多少次工具（读文件、执行命令、搜索等）。

**默认值**：`200`

**通俗解释**：

Agent 每次帮你做事时，可能会多次调用各种工具（比如读 10 个文件、执行 5 条命令、搜索 3 次……）。这个值限制了总的调用次数上限，防止 Agent 陷入无限循环。

**建议**：

| 场景 | 建议值 |
|------|--------|
| 日常使用 | `200`（默认，够用） |
| 复杂大型项目 | `500`（需要更多操作） |
| 节省 token | `100`（减少不必要的操作） |

**注意**：设得太小可能导致 Agent 在复杂任务中"半途而废"。一般情况下保持默认即可。

---

### max_concurrent_agents — 同时运行子代理数

**作用**：限制同一时刻最多有多少个**后台子代理**（通过 `agent` 工具以 `run_in_background=true` 委派）同时在运行。

**默认值**：`5`

**取值范围**：`1` ~ `10`（超出范围的值会被自动钳制到该区间）

**通俗解释**：

当你让 Agent 把一个大任务拆成很多小块，并同时委派给多个子代理并行处理时（例如大文件翻译的多个分块），这个值限制最多同时跑几个。超过上限的委派请求会自动**排队等待**，等有子代理完成后才开始执行，不会报错。

**建议**：

| 场景 | 建议值 |
|------|--------|
| 日常使用 | `5`（默认） |
| 机器性能好、任务分块多 | `8`~`10` |
| 想节省 API 费用、避免并发过高 | `1`~`3` |

**注意**：设为 `1` 表示同一时刻只允许 1 个后台子代理，其余全部排队。

### auto_update — 自动升级

**作用**：是否在后台线程自动检查 GitHub 新版本并下载升级包（`system.auto_update` 位于 system 下）。

**默认值**：`true`（未配置时默认开启）

**通俗解释**：

升级分两步，覆盖代码的逻辑只在启动时执行一处：

1. **下载**：服务启动约 5 秒后，后台线程读取本地 `web/static/version.json` 的 `version` 字段（格式 `vYYYYMMDD`），并与 GitHub 仓库（`one-leaf/cili`）的 version.json 比对。发现新版本即把代码包 zip 下载到脚本目录（项目根 `cili-update.zip`），不影响运行中的服务。设置页「关于」页签也可手动检查并下载升级包。下载优先 GitHub 直连，失败自动回退 ghfast.top / ghproxy.net / gh-proxy.com 国内镜像；远端拉取失败视为检查失败，跳过下载，不影响服务启动。
2. **应用**：下次启动时检测到脚本目录有升级包，先比对包内版本与本地版本，**包版本更高才解压覆盖**（保留 `data/`、`workspace/`、`.git` 目录），然后继续启动；包版本不高于本地或包损坏时自动删除升级包。

**建议**：

| 场景 | 建议值 |
|------|--------|
| 想始终使用最新版 | 保持默认 `true` |
| 生产环境或需要固定版本 | `false` |

**注意**：关闭后不再自动下载，仍可在设置页「关于」页签手动下载升级包，或运行 `scripts/upgrade.cmd`（同样只下载，重启后自动应用）。

---

## 配置优先级

配置的生效优先级从高到低：

1. **环境变量**（最高优先级）
   - `ANTHROPIC_API_KEY` — 覆盖 master 模型的 api_key
   - `ANTHROPIC_BASE_URL` — 覆盖 master 模型的 base_url
   - `ANTHROPIC_MODEL` — 覆盖 master 模型的模型 ID
2. **setting.json 文件**（中优先级）
3. **默认值**（最低优先级）

环境变量只作用于 **master** 模型，不影响 worker / lite。CLI `--model` 与 `ANTHROPIC_MODEL` 同级。

也就是说，如果你同时设置了环境变量和 setting.json，环境变量的值会优先生效。

---

## 配置示例

### 示例 1：使用 Anthropic Claude（官方直连）

```json
{
  "providers": {
    "anthropic": {
      "interface_type": "anthropic",
      "api_key": "sk-ant-api03-你的密钥",
      "models": {
        "claude-sonnet-4-6": { "max_output_tokens": 8192, "max_context_tokens": 200000 },
        "claude-haiku-4-5": { "max_output_tokens": 8192 }
      }
    }
  },
  "agents": {
    "master": { "model": "anthropic/claude-sonnet-4-6" },
    "worker": { "model": "anthropic/claude-sonnet-4-6" },
    "lite":   { "model": "anthropic/claude-haiku-4-5" }
  },
  "system": {
    "pip_mirror": "https://repo.huaweicloud.com/repository/pypi/simple/",
    "browser_path": "",
    "search_engine": "bing",
    "allowed_ips": [],
    "mineru_api_key": "",
    "max_iterations": 200,
    "max_concurrent_agents": 5
  }
}
```

直连官方 API 时模型条目可以省略 `name`（键名即模型 ID）。

### 示例 2：使用国内 API 代理服务

```json
{
  "providers": {
    "proxy": {
      "interface_type": "openai",
      "api_key": "sk-你的代理服务密钥",
      "base_url": "https://你的代理服务地址.com/v1",
      "models": {
        "main": { "name": "claude-sonnet-4-6", "max_context_tokens": 200000 }
      }
    }
  },
  "agents": { "master": { "model": "proxy/main" } },
  "system": {
    "pip_mirror": "https://mirrors.aliyun.com/pypi/simple/",
    "browser_path": "",
    "search_engine": "bing",
    "allowed_ips": [],
    "max_iterations": 200,
    "max_concurrent_agents": 5
  }
}
```

### 示例 3：使用本地部署的模型（Ollama）

```json
{
  "providers": {
    "local": {
      "interface_type": "openai",
      "api_key": "ollama",
      "base_url": "http://localhost:11434/v1",
      "models": {
        "qwen2.5:14b": { "max_context_tokens": 32768, "model_type": "chat" }
      }
    }
  },
  "agents": { "master": { "model": "local/qwen2.5:14b" } },
  "system": {
    "pip_mirror": "",
    "browser_path": "",
    "search_engine": "bing",
    "allowed_ips": [],
    "max_iterations": 200,
    "max_concurrent_agents": 5
  }
}
```

### 示例 4：允许局域网内其他设备访问

```json
{
  "providers": { ... },
  "system": {
    "pip_mirror": "",
    "browser_path": "",
    "search_engine": "bing",
    "allowed_ips": ["192.168.1.100", "192.168.1.101"],
    "max_iterations": 200,
    "max_concurrent_agents": 5
  }
}
```

---

## 常见问题

### Q：配置改完后需要重启吗？

A：通过 Web UI 设置页面修改的配置会立即生效（会通知已创建的 Agent 重新加载配置）。直接编辑 setting.json 文件的话，对新创建的会话/Agent 生效；已在运行的会话不会自动重载，需要重启服务（或通过 Web UI 重新保存一次）。

### Q：api_key 填错了会怎样？

A：启动时会报错提示"No API key found"或连接失败。请检查密钥是否正确、是否复制完整。推荐用 `${环境变量}` 引用方式，避免密钥写进配置文件。

### Q：agents 里引用了不存在的模型会怎样？

A：启动失败并列出全部可用引用（fail-fast，不静默回退）。按报错信息改成正确的 `"provider键/模型键"` 即可。

### Q：不同角色必须用同一家提供商的模型吗？

A：不需要。providers 可以配多家（每个 provider 各自的凭据和模型表），agents 里各角色引用任意提供商的模型，完全自由组合。

### Q：worker / lite 不配会怎样？

A：不影响任何功能。对应角色（Worker/Lite 子代理）直接使用 master 的模型，只是无法通过更便宜的模型降低委派任务的成本。

### Q：max_context_tokens 设得比模型实际支持的大怎么办？

A：超出模型实际能力的部分会被忽略，不会报错，但也不会生效。建议根据模型的实际上下文窗口设置。

### Q：我不在国内，pip_mirror 需要改吗？

A：可以改成官方源 `https://pypi.org/simple/`，或者直接留空 `""`，让 pip 使用默认配置。

### Q：JSON 格式报错了怎么办？

A：JSON 格式很严格，常见错误包括：
- 最后一项后面多了逗号（不允许）
- 字符串没有用双引号（必须用 `"双引号"`，不能用 `'单引号'`）
- 反斜杠没有转义（Windows 路径要用 `\\` 而不是 `\`）

可以用在线 JSON 校验工具检查格式是否正确。
