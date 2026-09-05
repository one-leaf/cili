<div align="center">

# 草履虫 Cili Agent

**自托管 Python 编程智能体 · 浏览器自动化 · 中文原生界面**

![草履虫](web/static/favicon.png)

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)](https://fastapi.tiangolo.com/)
[![Playwright](https://img.shields.io/badge/Playwright-Browser_Automation-00c4ff.svg)](https://playwright.dev/)
[![Tests](https://img.shields.io/badge/Tests-30+_Files-yellow.svg)](test/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

---

草履虫（Cili Agent）是一个**完全自托管的 Python 编程智能体**，拥有独立的 Web UI。它能在你的本地环境中自主阅读文件、执行命令、编写代码、操控浏览器、搜索网页，并通过大语言模型的工具调用循环编排这一切——数据不出你的机器。

## 为什么是草履虫

草履虫是自然界最简单的多细胞生物之一——结构精简，但功能完备。本项目以此为名，追求同样的哲学：**架构干净、依赖极少、开箱即用**。

- **零 SDK 依赖** — 原始 httpx 实现 SSE 流式与重试，不依赖 Anthropic / OpenAI 官方 SDK
- **双模型架构** — RootAgent 负责多轮对话，LLM 模型处理单轮摘要压缩，各司其职
- **浏览器自动化** — Playwright + Chrome CDP，全局单例管理，支持网页搜索与内容抓取
- **LaTeX 支持** — 内置 LaTeX 编译器，支持生成论文级别的 PDF 文档
- **中文原生** — UI、日志、工具输出均为中文，系统提示词为英文（最优 LLM 性能）
- **Windows 专属** — 针对 Windows 优化，Git Bash 执行 Shell，双击即可运行

## 快速开始

### 前置条件

- **Windows 10/11**（目前仅支持 Windows）
- 无需手动安装 Python 或 Git Bash — 启动脚本自动处理

### 启动

```bash
# 方式一：双击启动（自动检测/下载依赖）
start.cmd

# 方式二：PowerShell 指定端口
start.ps1 --port 8080

# 方式三：直接运行（需要 Python 在 PATH 中）
python main.py
python main.py --port 8080 --host 0.0.0.0
```

启动后浏览器自动打开 `http://localhost:8000`，即可开始对话。

### 配置

首次启动时通过 Web UI 配置 LLM 模型：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `model.name` | RootAgent 模型 | `claude-sonnet-4-6` |
| `model.interface_type` | API 协议 | `anthropic` |
| `model.api_key` | API 密钥 | — |
| `model.base_url` | API 端点 | `https://api.anthropic.com` |
| `llm_model.name` | 压缩/摘要模型（可选） | `claude-haiku-4-5` |

支持环境变量覆盖：`ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL`。

## 自动升级

### Web UI 升级（推荐）

1. 打开设置页面（点击左上角 ⚙ 按钮）
2. 切换到「升级」标签页
3. 选择镜像源：
   - **GitHub 直连** — 默认，适合海外用户
   - **ghproxy.net 镜像** — 国内加速
   - **ghfast.top 镜像** — 国内加速（备用）
   - **gh-proxy.com 镜像** — 国内加速（备用）
4. 点击「检查并升级」
5. 升级完成后重启服务

### 命令行升级

```batch
# 批处理版本
scripts\upgrade.cmd

# PowerShell 版本
.\scripts\upgrade.ps1
```

升级脚本会自动：
- 从 GitHub 或镜像下载最新代码 ZIP
- 解压并覆盖本地文件（保留 `data/`、`workspace/` 目录）
- 新增/更新的文件会用 `[+]` / `[~]` 标记
- 升级完成后重启服务即可

## 核心能力

### Agent 架构

```
RootAgent (主对话，流式输出)
  ├── 22 工具：读写文件、执行 Shell、Python、浏览器、搜索、记忆、定时任务、PDF转换...
  ├── 内置技能：代码审查、任务委派、研究、学习、技能创建...
  └── SubAgent (后台委派，非流式)
        ├── 独立消息历史（最多 200 轮迭代）
        └── 后台任务管理：启动、读取、终止
```

三层工具设计：

| 层级 | 工具数 | 说明 |
|------|--------|------|
| **shared** | 19~20 | RootAgent 与 SubAgent 共用（读写、Shell、浏览器、搜索、Python、记忆、PDF转换、loop 进度追踪等） |
| **root** | 3 | RootAgent 专属（技能系统、SubAgent 委派、用户提问） |
| **sub** | 1 | SubAgent 专属（技能系统） |

### LLM 底层架构

Provider 无关的 Adapter 分层设计：

```
Message → Adapter.serialize() → HTTP (httpx) → API → Adapter.parse() → ContentBlock
                                  ↓
                           HttpTransport (SSE / 重试 / 背压)
                                  ↓
                           BlockAssembler (流式累积)
```

- **统一流式协议**：8 种 StreamChunk 类型，所有 Provider 通过 Adapter 翻译
- **自动重试**：429/5xx 指数退避 + 抖动，支持 `Retry-After` 头
- **LiteLLM 代理自动检测**：通过 `/openapi.json` 端点识别，自动附加 `litellm_session_id`
- **延迟解析**：`ToolCallBlock.arguments` 保留原始 JSON，执行时才解析

### 浏览器自动化

```
BrowserService (模块级单例)
  ├── 专用工作线程（解决 Playwright greenlet 线程绑定）
  ├── Tab 池管理（自动清理空闲标签页）
  ├── 嵌套调用检测（避免死锁）
  └── Chrome 延迟启动（首次操作时才创建进程）
```

- 网页搜索：支持 Bing 和 Google 双引擎
- 网页抓取：Playwright 渲染 + 智能内容提取
- 全局唯一 Chrome 进程，会话间复用

### 会话与持久化

- 每个会话独立目录（8 位十六进制 ID）
- 原子写入（临时文件 + `Path.replace()`）
- 消息三级压缩：Microcompact → Full Compact → 紧急 Body Size
- SubAgent 执行日志实时保存，前端懒加载
- 支持多工作区隔离（sessions、cwd、配置）

### 定时任务 (Cron)

通过对话创建和管理定时任务：

```python
# 每 60 分钟执行
cron(action="create", schedule={"type": "interval", "minutes": 60}, task="检查服务器状态")

# Cron 表达式
cron(action="create", schedule={"type": "cron", "expr": "0 9 * * *"}, task="每日晨报")

# 限制执行次数（配合 loop 工具实现自循环任务）
cron(action="create", schedule={"type": "interval", "minutes": 5}, task="导入文件", max_executions=100)
```

- 系统级任务（`core/cron.d/`）+ 用户级任务（对话创建）
- 状态持久化，重启后自动恢复调度
- 同一 workspace 串行执行，避免并发冲突
- **remaining 计数器**：每次执行自动递减，到 0 时任务自动 disable（硬保证）
- **max_executions 参数**：创建任务时可设置最大执行次数（1-9999，默认 9999）

#### 自循环任务（Loop 工具）

配合 loop 工具可实现跨调度周期的渐进式任务，所有项处理完毕后自动停止：

```python
# 示例：渐进式导入大量文件到记忆系统
# 1. 扫描文件列表，写入文件（每行一个路径）
#    find E:/documents -name "*.md" > file_list.txt

# 2. 创建定时任务（每 5 分钟执行一次）
cron(action="create", schedule={"type": "interval", "minutes": 5}, 
     task="将 file_list.txt 中的文件逐个导入记忆系统", max_executions=9999)

# 3. SubAgent 每次执行时：
#    - loop(action="sync", source_file="file_list.txt")  # 从文件同步项列表
#    - loop(action="next", source_file="file_list.txt")  # 获取下一个待处理文件
#    - 读取并处理文件
#    - loop(action="done", source_file="file_list.txt", item=当前文件)  # 标记完成
```

- **文件驱动**：`source_file` 参数指定项列表文件（每行一个），同时作为任务标识符
- **崩溃恢复**：当前项仍为 pending，下次执行自动重试
- **动态新增**：更新 file_list.txt 后，sync 会自动发现新增项

### 技能系统

Markdown + YAML frontmatter 格式，渐进式加载：

| 技能 | 所属层级 | 说明 |
|------|---------|------|
| code-review | root | 代码审查 |
| task-delegation | root | 任务拆分与委派 |
| research | root | 深度研究 |
| learning | root | 知识学习 |
| create-skill | root | 创建新技能 |
| grilling | root | 深度追问 |
| context-bounded-processing | sub | 上下文受限处理 |
| file-processing | shared | 文件处理 |

## 项目结构

```
cili/
├── start.cmd / start.ps1       # 启动入口（自动环境检测）
├── main.py                     # Python 入口（uvicorn web server）
── scripts/
│   ├── upgrade.cmd             # Windows 批处理升级脚本
│   ── upgrade.ps1             # PowerShell 升级脚本
├── core/
│   ├── base_agent.py         # Agent 基类（消息管理、压缩、LLM 调用）
│   ├── root_agent.py         # RootAgent（流式输出、用户交互）
│   ├── sub_agent.py          # SubAgent（后台委派、非流式）
│   ├── config.py             # 配置加载（环境变量 > 文件 > 默认值）
│   ├── session.py            # 会话管理（持久化、过滤、缓存）
│   ├── compression.py        # 消息压缩（Microcompact / Full Compact）
│   ├── prompts.py            # 系统提示词构建
│   ├── browser_service.py    # 浏览器服务（Playwright 单例）
│   ├── cron.py               # Cron 调度器
│   ├── message_bus.py        # 跨会话消息总线
│   ├── llm/                  # LLM 底层架构
│   │   ├── types.py          # ContentBlock / Message / StreamChunk
│   │   ├── adapter.py        # Adapter 抽象基类
│   │   ├── anthropic.py      # Anthropic Adapter
│   │   ├── openai.py         # OpenAI Adapter
│   │   ├── transport.py      # HTTP 传输（SSE / 重试）
│   │   ├── assembler.py      # 流式数据块累积
│   │   └── client.py         # 统一 API（chat / chat_stream）
│   ├── tools/                # 工具层
│   │   ├── shared/           # 共用工具（19~20 个，含 pdf2markdown、loop 进度追踪）
│   │   ├── root/             # RootAgent 专属（3 个）
│   │   └── sub/              # SubAgent 专属（1 个）
│   └── skills/               # 技能层
│       ├── root/             # RootAgent 技能（7 个）
│       ├── shared/           # 共用技能
│       └── sub/              # SubAgent 技能
├── web/
│   ├── web_api.py            # FastAPI 服务（REST + SSE 流式）
│   └── static/               # 前端静态文件
│       ├── index.html        # 单页应用
│       ├── app.js            # 前端逻辑
│       └── style.css         # 样式
├── docs/                     # 设计文档（14 篇）
── test/                     # 测试（30+ 文件）
└── workspace/                # 默认工作目录
```

## 测试

```bash
# 运行全部测试
python -m pytest test/ -v

# 运行单个测试文件
python -m pytest test/test_bash_tool.py -v

# 运行带覆盖率的测试
python -m pytest test/ --cov=core --cov-report=term-missing
```

测试覆盖：BaseAgent、RootAgent、SubAgent、LLM Adapter、Transport、工具系统、Session、Compression、Config、BrowserService、Cron、MessageBus、Web API 等核心模块。

## 设计文档

完整的架构设计文档见 [docs/](docs/) 目录：

| 文档 | 内容 |
|------|------|
| [agent-design.md](docs/design/agent-design.md) | Agent 架构、循环机制、消息管理 |
| [content-block-design.md](docs/design/content-block-design.md) | LLM 底层：Adapter 分层、ContentBlock 类型、StreamChunk 协议 |
| [tool-system-design.md](docs/design/tool-system-design.md) | 三层工具架构、执行流程、全部工具说明 |
| [session-management-design.md](docs/design/session-management-design.md) | 会话存储、消息过滤、自动压缩 |
| [browser-service-design.md](docs/design/browser-service-design.md) | BrowserService 单例、工作线程、Tab 池 |
| [cron-scheduler-design.md](docs/design/cron-scheduler-design.md) | Cron 调度器、任务配置、执行机制 |
| [memory-system-design.md](docs/design/memory-system-design.md) | 长期记忆：知识存储与技能复用 |
| [web-api-design.md](docs/design/web-api-design.md) | REST API、SSE 流式、前端架构 |
| [user-profile-design.md](docs/design/user-profile-design.md) | 用户画像：5 维度、Markdown 格式、Cron 提取 |
| [todo-write-design.md](docs/design/todo-write-design.md) | TodoWrite 任务规划：整表替换、三态状态 |
| [system-prompt-design.md](docs/design/system-prompt-design.md) | 系统提示词构建逻辑 |
| [environment-setup-design.md](docs/design/environment-setup-design.md) | Python 环境初始化、embeddable 模式 |
| [llm-api-reference.md](docs/design/llm-api-reference.md) | LLM API 参考文档 |

## 技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| Web 框架 | FastAPI + Uvicorn | 异步 ASGI 服务器 |
| HTTP 客户端 | httpx | 连接池、SSE、自动重试 |
| 浏览器自动化 | Playwright | CDP 连接 Chrome |
| 前端 | 原生 HTML/CSS/JS | 零框架依赖，轻量快速 |
| 数学渲染 | MathJax | LaTeX 公式支持 |
| Markdown | marked.js | 消息 Markdown 渲染 |

## 许可证

本项目基于 [MIT License](LICENSE) 开源。

---

<div align="center">

**草履虫** — 结构精简，功能完备

*by OneLeaf*

</div>
