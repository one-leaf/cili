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

`setting.json` 是一个 JSON 格式的文件，内容分为三大块：

```json
{
  "model": { ... },       // 主模型配置（必填）
  "llm_model": { ... },  // LLM 工具模型配置（可选）
  "system": { ... }      // 系统参数配置
}
```

---

## 一、主模型配置（model）

这是最核心的配置，决定了 Agent 使用哪个 AI 模型来回答你的问题。

```json
"model": {
  "name": "claude-sonnet-4-6",
  "interface_type": "anthropic",
  "api_key": "sk-xxx",
  "base_url": "https://api.anthropic.com",
  "max_tokens": 36000,
  "max_context_tokens": 256000,
  "multimodal": true,
  "temperature": 0.2,
  "reasoning_effort": ""
}
```

### name — 模型名称

**作用**：指定使用哪个 AI 模型。

**怎么填**：填你使用的模型提供商支持的模型 ID。

**常见示例**：

| 提供商 | 模型名称示例 |
|--------|-------------|
| Anthropic（Claude） | `claude-sonnet-4-6`、`claude-opus-4-20250514`、`claude-haiku-4-5` |
| OpenAI（GPT） | `gpt-4o`、`gpt-4o-mini`、`o3-mini` |
| 通义千问 | `qwen-plus`、`qwen-max` |
| 本地部署（Ollama 等） | 你部署时用的模型名 |

**注意**：模型名称必须和 `interface_type`、`base_url` 配套，不同提供商的接口格式不一样。

---

### interface_type — 接口类型

**作用**：告诉 Agent 用哪种 API 格式与模型通信。

**可选值**：

| 值 | 适用场景 |
|-----|---------|
| `"anthropic"` | Anthropic 官方 API，或兼容 Anthropic 格式的代理服务 |
| `"openai"` | OpenAI 官方 API，以及绝大多数兼容 OpenAI 格式的第三方服务（通义千问、DeepSeek、本地 Ollama 等） |

**怎么判断填哪个**：

- 如果你用的是 Anthropic 的 Claude 模型 → 填 `"anthropic"`
- 如果你用的是其他任何模型 → 大概率填 `"openai"`（因为现在大部分服务都兼容 OpenAI 格式）
- 如果不确定，看一下你的服务提供商的 API 文档，通常会说明用的是哪种格式

---

### api_key — API 密钥

**作用**：用于身份验证的密钥，相当于你的"通行证"。

**怎么获取**：

- **Anthropic**：登录 [console.anthropic.com](https://console.anthropic.com) → API Keys → Create Key
- **OpenAI**：登录 [platform.openai.com](https://platform.openai.com) → API Keys → Create new secret key
- **其他服务**：在你的服务提供商控制台里找"API Key"或"密钥"

**格式示例**：

- Anthropic：`sk-ant-api03-xxxxxxxxxxxx...`
- OpenAI：`sk-xxxxxxxxxxxx...`
- 第三方服务各不相同

**安全提醒**：

- ⚠️ **绝对不要**把 api_key 分享给别人或发到网上
- ⚠️ 如果你的 setting.json 不小心泄露了密钥，立即到控制台删除旧密钥并重新生成

**也可以通过环境变量设置**：设置环境变量 `ANTHROPIC_API_KEY` 可以覆盖配置文件中的值（优先级更高）。

---

### base_url — API 地址

**作用**：指定 API 请求发送到哪个服务器。

**常见值**：

| 场景 | base_url |
|------|----------|
| Anthropic 官方 | `https://api.anthropic.com` |
| OpenAI 官方 | `https://api.openai.com/v1` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| 本地 Ollama | `http://localhost:11434/v1` |
| 自建代理 | 你的代理服务器地址 |

**注意**：

- 末尾不要加 `/`（斜杠），比如写 `https://api.openai.com/v1` 而不是 `https://api.openai.com/v1/`
- 如果你用了 API 代理服务（中转站），填代理给你的地址

---

### max_tokens — 最大输出长度

**作用**：限制模型单次回复的最大长度（以 token 为单位）。

**默认值**：`16384`（约 1.6 万 token）

**通俗解释**：

1 个 token 大约等于 1~2 个汉字，或 3~4 个英文字母。max_tokens = 16384 意味着模型一次最多可以输出约 8000~16000 个汉字。

**建议值**：

| 场景 | 建议值 |
|------|--------|
| 日常对话 | `8192` |
| 写代码、写长文 | `16384`~`36000` |
| 需要非常长的输出 | 根据模型支持的上限设置 |

**注意**：这个值不能超过模型本身支持的最大输出长度。比如你的模型最多输出 8192 token，你设成 100000 也没用。

---

### max_context_tokens — 最大上下文长度

**作用**：指定模型能"记住"多少对话内容（包括你发的消息 + 模型的回复 + 工具调用结果，全部加起来）。

**默认值**：`256000`

**通俗解释**：

这个值决定了模型的"记忆容量"。值越大，Agent 能处理的内容越多（比如读取很长的文件、记住更长的对话历史）。

**建议值**：

| 模型 | 建议值 |
|------|--------|
| Claude Sonnet/Opus | `200000` |
| Claude Haiku | `200000` |
| GPT-4o | `128000` |
| 通义千问 | `131072` |
| 不确定 | `200000`（大部分现代模型都支持） |

**注意**：

- 这个值设得太大可能导致请求失败（超出模型的实际上下文窗口）
- 设得太小则 Agent 可能"忘记"之前的对话内容
- 如果不确定，保持默认 `256000` 通常没问题

---

### multimodal — 是否支持图片

**作用**：告诉 Agent 当前模型是否能看懂图片。

**可选值**：

- `true` — 模型支持图片输入（可以发截图、照片给 Agent 分析）
- `false` — 模型不支持图片（发图片会被忽略或报错）

**常见模型支持情况**：

| 模型 | multimodal |
|------|-----------|
| Claude Sonnet 4 / Opus 4 | `true` |
| GPT-4o | `true` |
| 通义千问 VL 系列 | `true` |
| 纯文本模型（如某些本地模型） | `false` |

**建议**：如果你不确定，可以设为 `true`，发图片试试看能不能正常处理。

---

### temperature — 创造性/随机性

**作用**：控制模型回答的"创意程度"。

**取值范围**：`0.0` ~ `1.0`

| 值 | 效果 | 适合场景 |
|-----|------|---------|
| `0.1` | 非常严谨、稳定，每次回答几乎一样 | 代码生成、精确任务 |
| `0.2` | 比较严谨，偶尔有点变化（**推荐默认值**） | 日常使用 |
| `0.5` | 适中，有时会有出乎意料的回答 | 创意写作 |
| `0.8`~`1.0` | 很跳脱，每次回答差异大 | 头脑风暴、创意发散 |

**建议**：保持默认的 `0.2` 就好。如果觉得 Agent 太死板，可以调到 `0.5`；如果觉得回答不够稳定，可以调到 `0.1`。

---

### reasoning_effort — 推理力度

**作用**：控制推理模型（如 o3-mini）的"思考深度"。

**可选值**：

| 值 | 效果 |
|-----|------|
| `""` （空字符串） | 使用模型默认值（**推荐**） |
| `"low"` | 快速回答，减少思考时间 |
| `"medium"` | 适中的思考深度 |
| `"high"` | 深度思考，适合复杂问题 |

**注意**：

- 只有推理模型（如 `o3-mini`、`o1` 等）支持这个参数
- 对于 Claude 系列模型，这个参数会被忽略，留空即可
- 推理力度越高，回答质量可能越好，但速度更慢、消耗更多 token

---

## 二、LLM 工具模型配置（llm_model）

这是一个**可选**的配置，用于 Agent 内部的一些"后台任务"，比如消息压缩、生成摘要、翻译等。

```json
"llm_model": {
  "name": "claude-haiku-4-5",
  "interface_type": "anthropic",
  "api_key": "sk-xxx",
  "base_url": "https://api.anthropic.com",
  "max_tokens": 8192,
  "max_context_tokens": 200000,
  "multimodal": false,
  "temperature": 0.1,
  "reasoning_effort": ""
}
```

**这些字段的含义和主模型完全一样**，这里不再重复。下面只说明几个区别：

### 和主模型的区别

| | 主模型（model） | LLM 工具模型（llm_model） |
|--|--------------|----------------------|
| 用途 | 和你对话、执行任务 | 后台处理（压缩、摘要等） |
| 调用方式 | 多轮对话 | 单次调用 |
| 要求 | 需要较好的能力 | 可以用更便宜/更快的模型 |
| 是否必填 | ✅ 必填 | ❌ 可选 |

### 使用建议

- **推荐搭配一个便宜快速的小模型**，比如 Claude Haiku、GPT-4o-mini 等
- 如果不想配置，直接删掉整个 `"llm_model": { ... }` 块即可，Agent 会用主模型来处理这些任务
- 如果配置了但没填 `api_key`，会自动复用主模型的 api_key

---

## 三、系统参数配置（system）

这一块配置的是 Agent 运行环境相关的参数，和 AI 模型无关。

```json
"system": {
  "pip_mirror": "https://repo.huaweicloud.com/repository/pypi/simple/",
  "browser_path": "",
  "search_engine": "bing",
  "allowed_ips": [],
  "mineru_api_key": "",
  "max_iterations": 200
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

1. Microsoft Edge（Program Files）
2. Microsoft Edge（Program Files x86）
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

## 配置优先级

配置的生效优先级从高到低：

1. **环境变量**（最高优先级）
   - `ANTHROPIC_API_KEY` — 覆盖 api_key
   - `ANTHROPIC_BASE_URL` — 覆盖 base_url
   - `ANTHROPIC_MODEL` — 覆盖 model name
2. **setting.json 文件**（中优先级）
3. **默认值**（最低优先级）

也就是说，如果你同时设置了环境变量和 setting.json，环境变量的值会优先生效。

---

## 配置示例

### 示例 1：使用 Anthropic Claude（官方直连）

```json
{
  "model": {
    "name": "claude-sonnet-4-6",
    "interface_type": "anthropic",
    "api_key": "sk-ant-api03-你的密钥",
    "base_url": "https://api.anthropic.com",
    "max_tokens": 16384,
    "max_context_tokens": 200000,
    "multimodal": true,
    "temperature": 0.2,
    "reasoning_effort": ""
  },
  "llm_model": {
    "name": "claude-haiku-4-5",
    "interface_type": "anthropic",
    "api_key": "sk-ant-api03-你的密钥",
    "base_url": "https://api.anthropic.com",
    "max_tokens": 8192,
    "max_context_tokens": 200000,
    "multimodal": false,
    "temperature": 0.1,
    "reasoning_effort": ""
  },
  "system": {
    "pip_mirror": "https://repo.huaweicloud.com/repository/pypi/simple/",
    "browser_path": "",
    "search_engine": "bing",
    "allowed_ips": [],
    "mineru_api_key": "",
    "max_iterations": 200
  }
}
```

### 示例 2：使用国内 API 代理服务

```json
{
  "model": {
    "name": "claude-sonnet-4-6",
    "interface_type": "openai",
    "api_key": "sk-你的代理服务密钥",
    "base_url": "https://你的代理服务地址.com/v1",
    "max_tokens": 16384,
    "max_context_tokens": 200000,
    "multimodal": true,
    "temperature": 0.2,
    "reasoning_effort": ""
  },
  "system": {
    "pip_mirror": "https://mirrors.aliyun.com/pypi/simple/",
    "browser_path": "",
    "search_engine": "bing",
    "allowed_ips": [],
    "max_iterations": 200
  }
}
```

### 示例 3：使用本地部署的模型（Ollama）

```json
{
  "model": {
    "name": "qwen2.5:14b",
    "interface_type": "openai",
    "api_key": "ollama",
    "base_url": "http://localhost:11434/v1",
    "max_tokens": 8192,
    "max_context_tokens": 32768,
    "multimodal": false,
    "temperature": 0.2,
    "reasoning_effort": ""
  },
  "system": {
    "pip_mirror": "",
    "browser_path": "",
    "search_engine": "bing",
    "allowed_ips": [],
    "max_iterations": 200
  }
}
```

### 示例 4：允许局域网内其他设备访问

```json
{
  "model": { ... },
  "system": {
    "pip_mirror": "",
    "browser_path": "",
    "search_engine": "bing",
    "allowed_ips": ["192.168.1.100", "192.168.1.101"],
    "max_iterations": 200
  }
}
```

---

## 常见问题

### Q：配置改完后需要重启吗？

A：通过 Web UI 设置页面修改的配置会立即生效。直接编辑 setting.json 文件的话，下次启动时生效。

### Q：api_key 填错了会怎样？

A：启动时会报错提示"No API key found"或连接失败。请检查密钥是否正确、是否复制完整。

### Q：model 和 llm_model 必须用同一家提供商吗？

A：不需要。你可以主模型用 Anthropic，llm_model 用 OpenAI，完全没问题。它们各自独立配置。

### Q：llm_model 不配会怎样？

A：不影响使用。Agent 会用主模型来处理压缩、摘要等后台任务，只是可能消耗更多 token。

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
