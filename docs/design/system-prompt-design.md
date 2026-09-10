# 系统提示词设计文档

本文档描述 Cili Agent 系统提示词（System Prompt）的设计原则、构建流程和结构。

> **Agent 架构**请参考 [Agent 设计文档](agent-design.md)
>
> **技能系统**请参考 [工具系统设计文档](tool-system-design.md)

---

## 目录

- [一、设计原则](#一设计原则)
- [二、提示词结构（块与层）](#二提示词结构块与层)
- [三、构建流程](#三构建流程)
- [四、角色 JSON 结构](#四角色-json-结构)
- [五、user 层注入](#五user-层注入)
- [六、防连续合并](#六防连续合并)
- [七、角色差异](#七角色差异)
- [八、动态环境上下文](#八动态环境上下文)
- [九、项目指令注入](#九项目指令注入)
- [十、提示缓存策略](#十提示缓存策略)
- [十一、关键设计决策](#十一关键设计决策)

---

## 一、设计原则

### 1.1 块与层架构

系统提示词采用**静态块 + 动态层**的可配置架构：

- **system prompt 块（blocks）**：按角色 JSON（`core/agents/{role}.json`）的 `system_prompt.blocks` 顺序拼装，构成 system prompt 本体。块类型由 `SYSTEM_BLOCK_GENERATORS` 注册（text/tools/skills），角色间可组合。
- **user 层（layers）**：按角色 JSON 的 `user_layers` 声明注入到 user 消息的层（claude_md/context/task/runtime），动态内容不进入 system prompt。

```
system prompt（静态块，可缓存）             user 消息（动态层，每次注入）
┌──────────────────────────────┐     ┌─────────────────────────┐
│ role 块（text，JSON 固定文案）  │     │ claude_md（项目指令）      │
│ tools 块（工具列表，动态生成）  │     │ context（环境上下文）      │
│ skills 块（技能列表，动态生成）  │     │ task（pinned 任务消息）    │
└──────────────────────────────┘     │ runtime（预算/检查/超时）  │
           ↓                         └─────────────────────────┘
      system prompt 本体                    注入层合并到消息最前
```

### 1.2 核心原则

| 原则 | 说明 |
|------|------|
| **配置化** | 块与层的启用/顺序/内容由角色 JSON 声明，角色差异不再散落在代码中 |
| **缓存友好** | system prompt 仅由静态块构成；动态内容走 user 层注入，不破坏前缀缓存 |
| **动态注入** | 工具列表/技能列表从实例动态生成；环境上下文与项目指令每次调用重新生成 |
| **角色交替** | 注入层与消息历史合并时自动合并连续 user 消息（OpenAI/Bedrock 约束） |
| **英文为主** | 系统提示词使用英文（发送给 LLM），UI 文本使用中文 |

---

## 二、提示词结构（块与层）

### 2.1 system prompt 块

| 块类型 | 生成器 | 内容 | 来源 |
|--------|--------|------|------|
| `text` | `_gen_text` | 固定文案（角色定义 + 行为规则） | 角色 JSON 中块的 `content` |
| `tools` | `_gen_tools` | 工具列表段 | 从 `agent.tools` 实例生成 |
| `skills` | `_gen_skills` | 技能列表段 | 从角色可见技能生成 |

### 2.2 user 层

| 层类型 | 生成器 | 说明 |
|--------|--------|------|
| `claude_md` | `_gen_claude_md` | 每次从磁盘重读项目指令文件（agent.md/CLAUDE.md），不持久化 |
| `context` | `_gen_context` | 动态环境上下文（日期/workspace/内存等），不持久化 |
| `task` | —（无生成器） | autonomous 运行时写入历史：pinned 任务消息（`_build_task_message`） |
| `runtime` | —（无生成器） | autonomous 运行时写入历史：预算预警/检查阶段/超时总结 |

> `task` 与 `runtime` 不在 `USER_LAYER_GENERATORS` 表中：它们由 Agent 在 autonomous 执行过程中直接写入消息历史（带 pinned/预算标记），而非每次 LLM 调用时注入。

---

## 三、构建流程

### 3.1 块拼装（build_system_prompt）

`Agent._build_system_prompt()`（`core/agent.py`）委托给 `core/prompt_builder.build_system_prompt()`：

```python
def build_system_prompt(agent) -> str:
    """按角色配置的 blocks 顺序拼装 system prompt（仅启用且非空的块）。"""
    parts = []
    for block in agent.role_cfg.system_prompt.get("blocks", []):
        if not block.get("enabled", True):
            continue
        gen = SYSTEM_BLOCK_GENERATORS.get(block.get("type"))
        if gen is None:
            continue
        content = gen(block, agent)
        if content:
            parts.append(str(content).strip())
    return "\n\n".join(parts)
```

要点：

- 块按 JSON 中声明的顺序拼装，用空行分隔；
- 仅拼装 `enabled != false`、生成器存在且输出非空的块；
- interactive（master）每轮循环重建 system prompt；autonomous（worker/lite）在 `__init__` 时拼装一次并缓存（`self._system_prompt`）。

### 3.2 块生成器（SYSTEM_BLOCK_GENERATORS）

定义于 `core/prompt_builder.py`：

```python
SYSTEM_BLOCK_GENERATORS: dict[str, Callable[[dict, Any], str]] = {
    "text": _gen_text,
    "tools": _gen_tools,
    "skills": _gen_skills,
}
```

- `_gen_text(block, agent)`：直接返回块的 `content`；`content` 为字符串或字符串数组（按行拼装）。
- `_gen_tools(block, agent)`：调用 `core.prompts._build_tools_section(agent.tools)`。
- `_gen_skills(block, agent)`：调用 `core.prompts._build_skills_section(agent.role)`。

### 3.3 工具列表生成（_build_tools_section）

```python
def _build_tools_section(tools: list) -> str:
    """从工具实例生成工具列表段落。"""
    lines = ["## Tools", "", "You have access to the following tools:"]
    for tool in tools:
        desc = tool.description.split("\n")[0].strip()
        lines.append(f"- **{tool.name}** — {desc}")
    return "\n".join(lines)
```

**注意**：这里只生成一行摘要描述，完整的工具 schema（含参数）通过 `tool.to_schema()` 发送给 API，由 API 侧的 tool definition 承载。

### 3.4 技能列表生成（_build_skills_section）

```python
def _build_skills_section(role: str) -> str:
    """按角色可见技能生成技能列表段落（通用）。"""
    from core.tools.skill import list_skills
    skills = list_skills(role)
    ...
```

从 `list_skills(role)` 获取该角色可见的技能（角色 JSON 的 `skills` 字段，`["*"]` 表示全部可见），生成技能摘要列表。

Agent 通过 `skill(action='read', skill_id='...')` 按需读取完整技能内容，避免一次性加载所有技能占用上下文。

---

## 四、角色 JSON 结构

角色定义位于 `core/agents/{role}.json`，由 `core/agent_config.load_agent_role()` 加载为 `AgentRoleConfig`。与提示词相关的两个字段：

### 4.1 system_prompt.blocks

声明 system prompt 的拼装块（有序）：

```json
"system_prompt": {
  "blocks": [
    {"id": "role",   "type": "text",   "enabled": true, "content": ["...", "..."]},
    {"id": "tools",  "type": "tools",  "enabled": true},
    {"id": "skills", "type": "skills", "enabled": true}
  ]
}
```

字段含义：

- `id`：块标识（仅命名，不参与拼装逻辑）；
- `type`：块类型，对应 `SYSTEM_BLOCK_GENERATORS` 的键；
- `enabled`：是否启用（默认 true）；
- `content`：仅 text 块需要，固定文案（字符串或字符串数组）。

> 原 `ROOT_PROMPT_TEMPLATE` / `SUB_PROMPT_TEMPLATE` 常量内容已整体迁入各角色 JSON 的 text 块 `content`，代码中不再有硬编码模板。

### 4.2 user_layers

声明需要注入的 user 消息层：

```json
"user_layers": [
  {"id": "project_instructions", "type": "claude_md", "enabled": true},
  {"id": "context",              "type": "context",   "enabled": true}
]
```

`id` 仅命名；`type` 对应层类型；`enabled` 控制启用。

### 4.3 三份角色 JSON 的块与层

| 角色 | 文件 | blocks | user_layers | mode |
|------|------|--------|-------------|------|
| master | `master.json` | role(text) / tools / skills | project_instructions(claude_md) / context | interactive |
| worker | `worker.json` | role(text) / tools / skills | task_message(task) / context / runtime_prompts(runtime) | autonomous |
| lite | `lite.json` | role(text) / tools | task_message(task) | autonomous |

- **master**：完整交互 Agent，注入项目指令（claude_md）与环境上下文（context）；
- **worker**：完整自主 Agent，注入任务消息（task）、环境上下文（context）与运行时提示（runtime：预算/检查/超时）；
- **lite**：极简自主 Agent（仅 read/write/edit/bash），只注入任务消息（task），无 context/运行时提示。

---

## 五、user 层注入

### 5.1 生成器表（USER_LAYER_GENERATORS）

定义于 `core/prompt_builder.py`：

```python
USER_LAYER_GENERATORS: dict[str, Callable[[Any], dict | None]] = {
    "claude_md": _gen_claude_md,
    "context": _gen_context,
}
```

每个生成器接收 agent，返回一条 `{"role": "user", "content": ...}` 消息 dict，或返回 `None`（不注入）。

### 5.2 claude_md 层（项目指令）

`_gen_claude_md(agent)` 调用 `core.prompts.build_instructions_message(agent.cwd)`，每次调用从磁盘重读工作区根目录的项目指令文件（优先级 agent.md > CLAUDE.md > claude.md），以 `<system-reminder>` 包装为注入消息。未找到文件时返回 `None`（不注入）。详见[九、项目指令注入](#九项目指令注入)。

### 5.3 context 层（动态环境上下文）

`_gen_context(agent)` 调用 `core.prompts.build_environment_context(agent.workspace_uuid, agent.cwd)`，生成 Workspace/OS/Shell/Python/临时目录/内存/User Profile/当前时间等动态上下文，作为独立 user 消息注入。详见[八、动态环境上下文](#八动态环境上下文)。

### 5.4 task 层（pinned 任务消息）

`task` 层不在生成器表中。autonomous（worker/lite）启动时由 `Agent._build_task_message()` 构建任务消息，作为**第一条 user 消息**写入历史并带 `_meta.pinned=True`（免疫压缩），内容为：

- `## Assigned Task` 标题；
- `### Objective`：任务目标（`self.task`）；
- `### Execution Plan`：执行计划（`self.plan` 的编号步骤，有则添加）；
- 迭代额度预算说明（`max_iterations`）；
- 下放主代理会话级批准的命令（`build_approved_commands_section`，与主代理共享 `ApprovalStore` 时）。

### 5.5 runtime 层（运行时提示）

`runtime` 层同样不在生成器表中。autonomous 执行过程中由 Agent 直接写入历史（均为 user 消息）：

| 提示 | 触发时机 | 阈值/说明 |
|------|----------|-----------|
| `_BUDGET_WARN_PROMPT`（额度预警） | `_inject_budget_notice` | 迭代 ≥ `max_iterations × 0.8`，各触发一次 |
| `_BUDGET_FINAL_PROMPT`（额度即将耗尽） | `_inject_budget_notice` | 迭代 ≥ `max_iterations × 0.95`，各触发一次 |
| `_CHECK_PROMPT`（检查阶段） | 主执行循环结束 | pinned 注入，进入检查阶段（`role_cfg.check_phase` 启用时） |
| `_TIMEOUT_WRAPUP_PROMPT`（超时总结） | 额度耗尽兜底 | `_wrapup_timeout_summary` 中注入，直接调 client.chat（不传 tools）生成总结 |

---

## 六、防连续合并

注入型 user 层与消息历史在 `Agent._get_messages_with_header()` 中合并：

```python
messages = super()._get_messages_with_header()
inject: list[dict] = []
for layer in self.role_cfg.user_layers:
    if not layer.get("enabled", True):
        continue
    gen = USER_LAYER_GENERATORS.get(layer.get("type"))
    if gen is None:
        continue  # task/runtime 层运行时写入历史，不在此注入
    msg = gen(self)
    if msg:
        inject.append(msg)
return assemble_context(messages, inject)
```

`core/prompt_builder.assemble_context(messages, inject_messages)`：

```python
def assemble_context(messages, inject_messages):
    """把注入型 user 消息与消息历史合并，并合并连续 user 消息。"""
    if not inject_messages:
        return messages
    result = list(inject_messages) + list(messages)
    merged = []
    for msg in result:
        if merged and merged[-1].get("role") == "user" and msg.get("role") == "user":
            merged[-1] = _merge_user_messages(merged[-1], msg)
        else:
            merged.append(msg)
    return merged
```

- 注入消息恒排在消息历史最前；
- 若注入的 user 消息与历史第一条 user 消息（如 pinned 任务消息）连续，二者合并为一条，保证**角色交替**（OpenAI/Bedrock 约束）；
- `_merge_user_messages(a, b)`：两条 content 均为 str 时以 `"\n\n"` 拼接；否则归一化为 block 列表后拼接，并保留第一条消息的 `_meta`（id/pinned 等）；
- 返回新列表，不修改入参；注入内容不持久化到 `self.messages`（会话文件保持干净）。

---

## 七、角色差异

| 特性 | master | worker | lite |
|------|--------|--------|------|
| mode | interactive | autonomous | autonomous |
| 角色定义 | 通用交互助手 | 自主任务执行 Agent | 极简自主 Agent |
| 工具白名单 | shared + agent/ask_user | shared（含 agent，去 todo/cron/message_bus/latex/ask_user） | read/write/edit/bash |
| system prompt 块 | role/tools/skills | role/tools/skills | role/tools |
| user 层 | claude_md / context | task / context / runtime | task |
| 项目指令注入 | ✅ | ❌ | ❌ |
| 环境上下文注入 | ✅（context 层） | ✅（context 层） | ❌ |
| 任务消息 | ❌（多轮对话） | ✅ pinned 首消息 | ✅ pinned 首消息 |
| 检查阶段 | — | ✅（check_phase） | ❌ |
| 预算预警 | — | ✅（budget_notice） | ❌ |
| 执行流程 | 多轮交互 | 目标→计划→执行→检查 | 目标→计划→执行 |

**worker 四阶段执行流程**：**目标→计划→执行→检查**。

- 启动时注入 pinned 任务消息（目标 + 计划）；
- 主执行循环结束后自动注入 `_CHECK_PROMPT`（同样 pinned），LLM 重新阅读任务目标与计划，逐项验证结果、发现问题立即修复，确认全部达标后输出最终总结；
- 检查阶段有独立的迭代上限（`_MIN_CHECK_ITERATIONS`，下限 10），超出后强制收尾；
- 检查阶段/预算预警由 `role_cfg.check_phase` / `role_cfg.budget_notice` 开关控制（lite 均关闭）。

---

## 八、动态环境上下文（build_environment_context）

原 `build_root_context` / `build_sub_context` 合并为单一函数 `core/prompts.build_environment_context(workspace_uuid, cwd)`，各角色一致：

```python
def build_environment_context(workspace_uuid: str = "", cwd: str = "") -> str:
    """构建动态环境变量，作为独立 user 消息段注入。"""
```

包含段落：

| 段落 | 内容 |
|------|------|
| **Workspace** | 工作目录（CWD），所有工具执行的相对路径基准 |
| **Operating System** | `platform.system() + platform.release()` |
| **Shell Environment** | bash / pwsh / python 三工具分工表、路径格式转换规则 |
| **Python Environment** | 必须使用 python 工具，虚拟环境自动激活 |
| **Temporary Files** | 临时目录（`CILI_TMP` 或 `data/tmp`），TEMP/TMP/TMPDIR 环境变量 |
| **Memory** | 内存目录（workspace 的 memory 目录），`memory(action='find')` 检索示例 |
| **User Profile** | 可选，存在 user-profile.md 时加载（跳过 YAML frontmatter 取正文） |
| **Current Time** | 当前日期，提示用于解释相对/时效性请求 |

每次调用重新生成（含当前时间），作为独立 user 消息（context 层）注入，不进入 system prompt，不影响其缓存。

**User Profile 自动加载**：若 workspace 的 `user-profile.md` 存在，自动读取正文（`---` 包裹的 YAML frontmatter 被跳过）注入为 `## User Profile` 段，指示 LLM 自然使用这些信息调整语气，但不要主动复述。文件损坏时静默跳过。

---

## 九、项目指令注入

### 9.1 设计目标

允许用户在工作区根目录放置项目级指令文件（如 `agent.md`、`CLAUDE.md`），Cili 在每次 LLM 调用时自动读取并注入到对话上下文中，使 Agent 了解项目特定的规则和约定。

### 9.2 文件搜索规则

`core/prompts.find_project_instructions(cwd)` 按优先级搜索工作区根目录：

1. `agent.md`
2. `CLAUDE.md`
3. `claude.md`

找到第一个即返回；若均不存在，返回 `None`（不注入）。

### 9.3 注入位置

作为 **claude_md 层**（`_gen_claude_md`）在每次 LLM 调用时动态注入到消息最前面，不持久化到会话消息中（会话文件保持干净）：

```
messages = [
  {role: "user", content: "<system-reminder>\nCodebase and user instructions are shown below...\n{文件内容}\n</system-reminder>"},  ← 动态注入
  {role: "user", content: "用户的第一条消息"},
  ...
]
```

若与消息历史第一条 user 消息连续，`assemble_context` 会合并成一条，避免连续同角色消息（OpenAI API / Bedrock 要求）。

**为什么不放入 system prompt？**
- 项目指令内容因工作区而异，属于动态内容
- 每次从磁盘重新读取，修改指令文件后立即生效（无需重启）

### 9.4 注入范围

| Agent 类型 | 是否注入 | 原因 |
|------------|----------|------|
| master | ✅ 注入 | 用户交互需要项目上下文 |
| worker / lite | ❌ 不注入 | 任务执行者，不需要项目级指令 |

### 9.5 实现函数

位于 `core/prompts.py`：

```python
_PROJECT_INSTRUCTION_FILES = ["agent.md", "CLAUDE.md", "claude.md"]

def find_project_instructions(cwd: str) -> str | None:
    """在工作区根目录搜索项目指令文件。"""

def build_instructions_message(cwd: str) -> dict | None:
    """构建项目指令消息（<system-reminder> 包装，作为注入 user 消息）。"""
```

`build_instructions_message` 由 `_gen_claude_md`（claude_md 层）调用，仅在角色 JSON 的 user_layers 中声明了该层时生效（当前仅 master）。

---

## 十、提示缓存策略

system prompt 由**静态块**构成（无动态内容），配合独立注入的 user 层，缓存策略如下：

### 10.1 静态块（可缓存部分）

```
┌─ system prompt（仅块拼装）──────────────┐
│  role 块（text）                   [JSON 固定] │
│  tools 块（工具列表）               [进程内固定] │
│  skills 块（技能列表）              [进程内固定] │
└────────────────────────────────────────────┘
```

- master（interactive）每轮重建 system prompt，但块内容（角色文案/工具/技能）在进程生命周期内稳定；
- worker/lite（autonomous）在 `__init__` 时拼装一次并缓存，整个执行过程复用。

### 10.2 动态部分（user 层注入）

```
┌─ user 消息层（注入，不进入 system prompt）──┐
│  claude_md（项目指令）                 [磁盘变化时] │
│  context（环境上下文）                 [每次不同]   │
│  task（pinned 任务消息）               [执行固定]    │
│  runtime（预算/检查/超时）             [阶段触发]    │
└──────────────────────────────────────────────┘
```

动态内容作为独立 user 消息注入，而非拼入 system prompt，因此 system prompt 前缀保持逐字节稳定：

- 有利于 API 的前缀缓存命中；
- 环境上下文中的日期等变化不会破坏 system prompt 的缓存。

### 10.3 缓存效果

| 组件 | 状态 | 原因 |
|------|------|------|
| role 块 | 静态（进程生命周期内不变） | JSON 固定文案 |
| tools 块 | 静态（进程生命周期内不变） | 工具实例缓存 |
| skills 块 | 静态（进程生命周期内通常不变） | 每次构建时重新扫描 |
| claude_md 层 | 动态（磁盘变化时） | 每次调用重读指令文件 |
| context 层 | 动态（每次调用） | 含当前时间 |

---

## 十一、关键设计决策

### 11.1 为什么用块/层架构代替硬编码模板？

原 `ROOT_PROMPT_TEMPLATE` / `SUB_PROMPT_TEMPLATE` 常量把角色文案硬编码在代码中，角色差异靠分支逻辑实现。块/层架构下：

- 角色文案迁入 `core/agents/*.json` 的 text 块，改文案不用改代码；
- 块与层的启用/顺序由 JSON 声明，新增角色只需新增 JSON；
- 动态内容统一走 user 层注入，system prompt 保持纯静态。

### 11.2 为什么工具描述只有一行摘要？

完整工具定义（含参数 schema）通过 API 的 `tools` 字段发送，这是 API 原生支持的机制。系统提示中只需列出工具名称和一行描述，供 LLM 快速识别可用工具。

### 11.3 为什么技能只列摘要，不加载全文？

技能全文（Markdown）通常较长。全部加载会占用大量上下文窗口。采用**渐进加载**策略：

1. system prompt 的 skills 块只列技能 ID 和描述
2. Agent 根据任务判断需要哪个技能
3. 通过 `skill(action='read')` 按需加载完整内容

### 11.4 环境上下文为何不放入 system prompt？

环境上下文每次调用都重新生成（含当前日期），若拼入 system prompt 会破坏其前缀缓存。作为独立 user 消息（context 层）注入，不影响 system prompt 的缓存命中。

### 11.5 worker 与 master 环境一致性

所有角色的环境上下文都由同一个 `build_environment_context()` 生成（Workspace、OS、Shell、Python、Memory、User Profile、Current Time），内容一致。worker 作为任务执行者，同样需要了解用户偏好以提供更个性化的结果；lite 则不注入 context 层以节省 token。

### 11.6 为什么系统提示词使用英文？

系统提示词发送给 LLM（Claude/GPT 等），英文指令对模型更友好，遵循效果更好。用户界面文本和工具输出使用中文。

---

*文档版本: v2.0*
*最后更新: 2026-09-10*
