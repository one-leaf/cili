# 系统提示词设计文档

本文档描述 Cili Agent 系统提示词（System Prompt）的设计原则、构建流程和结构。

> **Agent 架构**请参考 [Agent 设计文档](agent-design.md)
>
> **技能系统**请参考 [工具系统设计文档](tool-system-design.md)

---

## 目录

- [一、设计原则](#一设计原则)
- [二、提示词结构](#二提示词结构)
- [三、构建流程](#三构建流程)
- [四、静态模板](#四静态模板)
- [五、动态环境变量](#五动态环境变量)
- [六、RootAgent 与 SubAgent 差异](#六rootagent-与-subagent-差异)
- [七、提示缓存策略](#七提示缓存策略)
- [八、项目指令文件注入](#八项目指令文件注入)
- [九、关键设计决策](#九关键设计决策)

---

## 一、设计原则

### 1.1 分层架构

系统提示词采用**静态 + 动态**分层设计，便于 API 缓存：

```
静态部分（可永久缓存）          动态部分（每次请求变化）
┌────────────────────────┐    ┌─────────────────────┐
│ 角色定义               │    │ 工作目录             │
│ 工具列表               │    │ Memory 目录路径       │
│ 技能列表               │    │ 用户 Profile          │
│ 行为规则（模板）        │    │ 当前日期              │
└────────────────────────┘    └─────────────────────┘
         ↓                              ↓
    作为 system prompt            作为独立 user 消息段
```

### 1.2 核心原则

| 原则 | 说明 |
|------|------|
| **缓存友好** | 静态规则与动态变量分离，静态部分可被 API 永久缓存 |
| **动态注入** | 工具列表和技能列表从实例动态生成，不硬编码在模板中 |
| **最小必要** | 只包含必要信息，避免冗余描述浪费 token |
| **英文为主** | 系统提示词使用英文（发送给 LLM），UI 文本使用中文 |

---

## 二、提示词结构

完整系统提示词由 `_build_prompt()` 通用函数拼接：

```
[1] 角色定义（Header）
    ↓
[2] 工具列表（Tools Section）         ← 动态生成
    ↓
[3] 技能列表（Skills Section）        ← 动态生成
    ↓
[4] 行为规则（Template）              ← 静态模板
    ↓
[5] 动态环境变量（Context）           ← 每次请求不同，作为独立 user 消息段
```

---

## 三、构建流程

### 3.1 通用构建函数

```python
def _build_prompt(header, tools_fn, skills_dir, template, env_context_fn, workspace_uuid, cwd):
    """通用 prompt 构建：角色 + 动态工具 + 动态技能 + 静态规则 + 动态环境变量"""
    parts = [header.strip()]
    tools_section = tools_fn()
    skills_section = _build_skills_section(skills_dir)
    if tools_section:
        parts.extend(["", tools_section])
    if skills_section:
        parts.extend(["", skills_section])
    parts.extend(["", template.strip()])
    # 动态环境变量作为独立 user 消息段发送
    env_context = env_context_fn(workspace_uuid, cwd)
    parts.extend(["", env_context])
    return "\n".join(parts)
```

### 3.2 工具列表生成

从工具实例动态生成，使用缓存（描述是静态的，只需创建一次）：

```python
def _build_tools_section(tools: list) -> str:
    """从工具实例生成工具列表段落"""
    lines = ["## Tools", "", "You have access to the following tools:"]
    for tool in tools:
        desc = tool.description.split("\n")[0].strip()
        lines.append(f"- **{tool.name}** — {desc}")
    return "\n".join(lines)
```

**注意**：这里只生成一行摘要描述，完整的工具 schema（含参数）通过 `tool.to_schema()` 发送给 API，由 API 侧的 tool definition 承载。

### 3.3 技能列表生成

从 `core/skills/` 目录扫描，生成技能摘要：

```python
def _build_skills_section(skills_dir: str) -> str:
    """从 skills 目录扫描生成技能列表段落"""
    skills = list_skills(skills_dir)
    lines = ["## Available Skills", "", ...]
    for s in skills:
        lines.append(f"- **{s['name']}** (`{s['id']}`): {s['description']}")
    return "\n".join(lines)
```

Agent 通过 `skill(action='read', skill_id='...')` 按需读取完整技能内容，避免一次性加载所有技能占用上下文。

---

## 四、静态模板

### 4.1 RootAgent 模板（ROOT_PROMPT_TEMPLATE）

角色定义（`_ROOT_HEADER`）：
```
You are Cili, an AI assistant powered by a large language model.
Your job is to understand the user's intent and help with any kind of task...
Identity: You are Cili — an AI assistant, not merely a programming assistant.
```

行为规则主要章节：

| 章节 | 内容 |
|------|------|
| **Critical Rules** | 包含以下子节：Skills - Proactive Usage、Tool Result Re-reading、Python、大文件处理 |
| **Coding Workflow** | inspect → understand → modify → verify → fix → verify |
| **Verification** | 修改后必须验证（测试/lint/build） |
| **Errors** | 将错误视为调试信号，不要盲目重试 |
| **Repository Safety** | 谨慎使用破坏性命令，优先用 edit 而非 write |
| **Workspace Files and Images** | 相对路径规则、文件 URL 格式（/api/workspace/files/）、图片生成规则（禁用 GUI API） |
| **Memory** | 任务前搜索 memory 目录、技能命名规范 |
| **Web** | web_search 优先，browser 备用 |
| **Communication** | 简洁、同语言回复、完成后汇报变更 |
| **Agent Principles** | 优先执行、增量修改、验证优先 |

### 4.2 SubAgent 模板（SUB_PROMPT_TEMPLATE）

角色定义（`_RUNTIME_HEADER`）：
```
You are an autonomous task-execution agent running inside Cili.
Your responsibility is to complete the assigned task reliably and verifiably.
You are not managing the user's conversation — you are executing a specific task.
```

行为规则主要章节：

| 章节 | 内容 |
|------|------|
| **Core Objective** | 完成任务，不只是给出答案 |
| **Skills - Proactive Usage** | 检查可用技能（Research、File Processing、Learning） |
| **Autonomous Execution** | 自主执行，不请求确认 |
| **Execution Loop** | 7 步执行循环 |
| **Tool Usage** | 工具是执行能力，不是推理替代 |
| **Tool Result Re-reading** | 截断/压缩场景下的工具结果重读规则 |
| **File and Workspace Rules** | 工作目录边界、最小修改 |
| **Verification** | 验证是任务完成的必要部分 |
| **Error Recovery** | 可恢复 vs 不可恢复错误分类 |
| **Task Completion** | 4 项完成标准 |
| **Blocked State** | 无法完成时返回清晰报告 |
| **No User-Level Conversation** | 不管理用户对话，输出是执行结果 |
| **Behavioral Priority** | 7 级优先级链 |

---

## 五、动态环境变量

### 5.1 RootAgent 环境变量（build_root_context）

每次请求不同，作为独立 user 消息段发送，不影响 system prompt 缓存：

```
## Workspace
Workspace directory (CWD): `{cwd}`
This directory is the CWD for all tool executions.

## Shell Environment
All shell commands run in **Git Bash** (MSYS2 environment).

**Path format conversion**: Windows paths must be converted for bash:
- `E:\path\to\file` → `/e/path/to/file`
- `C:\Users\name` → `/c/Users/name`

## Python Environment
`python` and `pip` are pre-configured in PATH, available directly in bash.

## Temporary Files
Temporary directory: `{tmp_dir}`
Environment variables TEMP, TMP, TMPDIR are all set to this directory.
Use this directory for all intermediate files, temp outputs, downloads, and program state files.
In bash: use `$TEMP` or `$TMPDIR`. In Python: `tempfile` module is auto-configured.
Agent can also use `CILI_TMP` env var to reference this path.

## Memory
Memory directory: `{memory_dir}`
Search examples:
  grep(pattern="keyword", path="{memory_dir}/skills/")
  grep(pattern="keyword", path="{memory_dir}/knowledge/")

## User Profile（可选，存在时加载）
The following was inferred from the user's past conversations...
**工作角色**: ...
**编程经验**: ...

## Current Time
{current_date}
Use this time when interpreting relative or time-sensitive requests.

Context received. Please confirm briefly and await my task.
```

### 5.2 SubAgent 环境变量（build_sub_context）

轻量版本，不含 User Profile：

```
## Execution Environment
Workspace: `{workspace_uuid}`
Working Directory: `{cwd}`
Operating System: `{os_info}`

**Shell**: Git Bash (MSYS2). Convert Windows paths: `E:\path` → `/e/path`

`python` and `pip` are pre-configured in PATH.

**Temporary directory**: `{tmp_dir}` (TEMP/TMP/TMPDIR env vars set)

**Current Date: {current_date}**

Context received. Please confirm briefly and await my task.
```

### 5.3 User Profile 自动加载

若 `user-profile.md` 存在，自动注入到环境变量中：

```markdown
---
工作角色: 后端工程师
编程经验:
  语言: Python, Go
  框架: FastAPI, Django
沟通偏好: 简洁直接
---

（正文内容）
```

支持 YAML frontmatter（`---` 包裹的元数据），加载时自动跳过 frontmatter 只取正文。

加载后作为 `## User Profile` 段注入，指示 LLM 自然使用这些信息调整语气，但不要主动复述。

---

## 六、RootAgent 与 SubAgent 差异

| 特性 | RootAgent | SubAgent |
|------|-----------|----------|
| 角色定义 | 通用 AI 助手 | 自主任务执行 Agent |
| 工具列表 | shared + root（含 subagent/skill） | shared + sub（含 skill，无 subagent） |
| 技能目录 | `core/skills/root/` + `shared/` | `core/skills/sub/` + `shared/` |
| 环境变量 | 完整（Workspace/OS/Shell/Python/Memory/User Profile/Time） | 与 RootAgent 一致 |
| 任务信息 | 无（多轮对话） | 首条 pinned user 消息（免疫压缩） |
| 执行流程 | 多轮交互 | 目标→计划→执行→检查 |
| 对话模式 | 用户交互，管理对话 | 不管理用户对话，输出是执行结果 |

### SubAgent 任务信息注入

SubAgent 的任务目标和执行计划不再追加到系统提示，而是作为第一条 **user 消息**注入，带 `_meta.pinned=True` 标记。压缩时 pinned 消息始终保留，效果等同于在系统提示中。

SubAgent 采用四阶段执行流程：**目标→计划→执行→检查**。主执行循环结束后自动注入检查提示（同样 pinned），LLM 验证结果、修复问题、确认完成后才返回最终结果。

---

## 七、提示缓存策略

Anthropic API 支持 prompt caching，Cili 的设计充分利用这一点：

### 7.1 可缓存部分

```
┌─ system prompt ──────────────────────────────┐
│  角色定义（Header）                    [永久缓存] │
│  工具列表（Tools Section）             [启动时固定] │
│  技能列表（Skills Section）            [启动时固定] │
│  行为规则（Template）                  [代码固定]   │
└─────────────────────────────────────────────┘
```

### 7.2 不可缓存部分

```
┌─ user message (context 段) ────────────────┐
│  Workspace / Memory 路径             [启动时固定] │
│  User Profile                        [Profile变化时] │
│  Current Date                        [每次不同]     │
└─────────────────────────────────────────────┘
```

动态环境变量作为**独立 user 消息**发送，而不是嵌入 system prompt，这样：
- System prompt 整体可被 API 永久缓存
- 只有 user message 部分因日期变化需要重新处理

### 7.3 缓存效果

| 组件 | 缓存状态 | 原因 |
|------|----------|------|
| 角色定义 | 永久命中 | 代码固定 |
| 工具列表 | 进程生命周期内命中 | 工具实例缓存 |
| 技能列表 | 进程生命周期内命中 | 扫描结果缓存 |
| 行为规则 | 永久命中 | 模板常量 |
| 环境变量 | 不缓存（user message） | 日期每次不同 |

---

## 八、项目指令文件注入

### 8.1 设计目标

允许用户在工作区根目录放置项目级指令文件（如 `agent.md`、`CLAUDE.md`），Cili 启动时自动读取并注入到对话上下文中，使 Agent 了解项目特定的规则和约定。

### 8.2 文件搜索规则

按优先级搜索工作区根目录（`cwd`）下的文件，找到第一个即停止：

1. `agent.md`
2. `CLAUDE.md`
3. `claude.md`

若均不存在，不注入任何内容。

### 8.3 注入位置

项目指令作为**独立 user 消息**注入到 `messages` 的**最前面**（第一条消息）：

```
messages = [
  {role: "user", content: "[Project Instructions]\n\n{文件内容}"},  ← 注入位置
  {role: "user", content: "用户的第一条消息"},
  {role: "assistant", content: "..."},
  ...
]
```

**为什么不放入 system prompt？**
- 保持 system prompt 可缓存（静态部分不变）
- 项目指令内容因工作区而异，属于动态内容
- CLAUDE.md 作为 user message 注入

### 8.4 注入范围

| Agent 类型 | 是否注入 | 原因 |
|------------|----------|------|
| RootAgent | ✅ 注入 | 用户交互需要项目上下文 |
| SubAgent | ❌ 不注入 | 任务执行者，不需要项目级指令 |

### 8.5 幂等注入

注入前检查 `messages[0]` 是否已是 `[Project Instructions]` 开头的 user 消息，避免重复注入：

```python
def has_instructions_message(messages: list[dict]) -> bool:
    if not messages:
        return False
    first = messages[0]
    return (first.get("role") == "user"
            and isinstance(first.get("content"), str)
            and first["content"].startswith("[Project Instructions]"))
```

### 8.6 实现函数

位于 `core/prompts.py`：

```python
_PROJECT_INSTRUCTION_FILES = ["agent.md", "CLAUDE.md", "claude.md"]

def find_project_instructions(cwd: str) -> str | None:
    """在工作区根目录搜索项目指令文件。"""

def build_instructions_message(cwd: str) -> dict | None:
    """构建项目指令消息（作为第一条 user 消息注入）。"""

def has_instructions_message(messages: list[dict]) -> bool:
    """检查消息列表开头是否已有项目指令消息。"""
```

调用位置：`core/root_agent.py` 的 `run()` 方法，在添加用户消息前调用 `_inject_project_instructions()`。

---

## 九、关键设计决策

### 9.1 为什么工具描述只有一行摘要？

完整工具定义（含参数 schema）通过 API 的 `tools` 字段发送，这是 API 原生支持的机制。
系统提示中只需列出工具名称和一行描述，供 LLM 快速识别可用工具。

### 9.2 为什么技能只列摘要，不加载全文？

技能全文（Markdown）通常较长。全部加载会占用大量上下文窗口。
采用**渐进加载**策略：
1. 系统提示只列技能 ID 和描述
2. Agent 根据任务判断需要哪个技能
3. 通过 `skill(action='read')` 按需加载完整内容

### 9.3 为什么环境变量作为 user message 而不是 system prompt？

Anthropic API 的 prompt caching 以 system prompt 为粒度。
环境变量包含 `Current Date`（每次请求不同），如果嵌入 system prompt，每次都要重新计算缓存。
分离后，system prompt 可永久缓存，只有 user message 部分需要处理。

### 9.4 SubAgent 与 RootAgent 环境一致性

SubAgent 和 RootAgent 的环境上下文注入保持一致（Workspace、OS、Shell、Python、Memory、User Profile、Current Time）。
SubAgent 作为任务执行者，同样需要了解用户偏好以提供更个性化的响应。

### 9.5 为什么系统提示词使用英文？

系统提示词发送给 LLM（Claude/GPT 等），英文指令对模型更友好，遵循效果更好。
用户界面文本和工具输出使用中文。

---

## 十、llm_tool 批处理提示词

`llm_tool` 用于批处理文本（翻译、摘要、提取），使用独立的轻量系统提示词：

```python
def build_llm_tool_system_prompt() -> str:
    return (
        "You are a text processing assistant. "
        "Process the given text according to the instructions. "
        "Return only the processed content, without commentary or explanations. "
        "Preserve the original formatting unless instructed otherwise."
    )
```

不加载工具/技能描述，仅处理文本。

批处理模式额外添加指令前缀：
```
## Non-Interactive Batch Processing Mode

You are running in a non-interactive batch environment.
You cannot ask questions or wait for user confirmation.
Execute the task autonomously to completion.
```

---

*文档版本: v1.0*
*最后更新: 2026-08-28*
