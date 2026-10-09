# 系统提示词设计文档

本文档描述 Cili Agent 系统提示词（System Prompt）的设计原则、构建流程和结构。

> **SessionRunner 架构**请参考 [SessionRunner 设计文档](session-runner-design.md)
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

系统提示词采用**静态区 + 动态区 + user 层**的可配置架构：

- **system prompt 块（blocks）**：按角色 JSON（`core/agents/{role}.json`）的 `system_prompt.blocks` 顺序拼装，构成 system prompt 本体。块类型由 `SYSTEM_BLOCK_GENERATORS` 注册（text/tools/skills/context），角色间可组合；`dynamic_boundary` 块标记静态区与动态区的分界。
- **user 层（layers）**：按角色 JSON 的 `user_layers` 声明注入到 user 消息的层（claude_md/task/runtime），不进入 system prompt。

```
system prompt（按 dynamic_boundary 分区）      user 消息（注入层）
┌────────────────────────────────┐     ┌─────────────────────────┐
│ 静态区（scope=global，可缓存）   │     │ claude_md（项目指令）      │
│   role 块（text，JSON 固定文案） │     │ task（pinned 任务消息）    │
│   tools 块（工具列表，动态生成）  │     │ runtime（预算/检查/超时）  │
│   skills 块（技能列表，动态生成） │     └─────────────────────────┘
├── dynamic_boundary ────────────┤                ↓
│ 动态区（session 内缓存）         │        注入层合并到消息最前
│   context 块（环境上下文）       │
└────────────────────────────────┘
```

### 1.2 核心原则

| 原则 | 说明 |
|------|------|
| **配置化** | 块与层的启用/顺序/内容由角色 JSON 声明，角色差异不再散落在代码中 |
| **缓存友好** | system prompt 静态区（`dynamic_boundary` 之前，`scope=global`）跨用户共享；动态内容（context）置于动态区并按 session 缓存 |
| **动态注入** | 工具列表/技能列表从实例动态生成；环境上下文（context 块）session 内缓存；项目指令（claude_md）每次调用重读 |
| **角色交替** | 注入层与消息历史合并时自动合并连续 user 消息（OpenAI/Bedrock 约束） |
| **英文为主** | 系统提示词使用英文（发送给 LLM），UI 文本使用中文 |

---

## 二、提示词结构（块与层）

### 2.1 system prompt 块

| 块类型 | 生成器 | 内容 | 来源 |
|--------|--------|------|------|
| `text` | `_gen_text` | 固定文案（角色定义 + 行为规则） | 角色 JSON 中块的 `content` |
| `tools` | `_gen_tools` | 工具列表段（含延迟工具摘要） | 从 `agent._active_tools` 实例生成 |
| `skills` | `_gen_skills` | 技能列表段 | 从角色可见技能生成 |
| `context` | `_gen_context` | 动态环境上下文（日期/workspace/内存等） | `core.prompt_sections.build_environment_context()`，session 内缓存 |
| `dynamic_boundary` | —（无生成器） | 静态区/动态区分界哨兵 | 不产出内容，仅切换分区 |

### 2.2 user 层

| 层类型 | 生成器 | 说明 |
|--------|--------|------|
| `claude_md` | `_gen_claude_md` | 每次从磁盘重读项目指令文件（AGENTS.md/CLAUDE.md），不持久化 |
| `task` | —（无生成器） | autonomous 运行时写入历史：pinned 任务消息（`_build_task_message`） |
| `runtime` | —（无生成器） | autonomous 运行时写入历史：预算预警/检查阶段/超时总结 |

> `task` 与 `runtime` 不在 `USER_LAYER_GENERATORS` 表中：它们由 SessionRunner 在 autonomous 执行过程中直接写入消息历史（带 pinned/预算标记），而非每次 LLM 调用时注入。

> `context` **不再是 user 层**：已迁移到 system prompt 的 `context` 块（`dynamic_boundary` 之后的动态区，session 内缓存），`USER_LAYER_GENERATORS` 现仅含 `claude_md`。

---

## 三、构建流程

### 3.1 块拼装（build_system_prompt）

`SessionRunner._build_system_prompt()`（`core/session_runner.py`）委托给 `core/prompt_builder.build_system_prompt()`：

```python
def build_system_prompt(agent) -> list[str]:
    """按角色配置的 blocks 顺序拼装 system prompt，返回字符串列表。

    列表元素按 DYNAMIC_BOUNDARY 分割：
    - boundary 之前：静态区（跨用户共享，scope='global'）
    - boundary 之后：动态区（session 特定，不 global 缓存）
    """
    static_parts: list[str] = []
    dynamic_parts: list[str] = []
    past_boundary = False

    for block in agent.role_cfg.system_prompt.get("blocks", []):
        if not block.get("enabled", True):
            continue
        block_type = block.get("type")
        if block_type == "dynamic_boundary":   # 切换分区
            past_boundary = True
            continue
        gen = SYSTEM_BLOCK_GENERATORS.get(block_type)
        if gen is None:
            continue
        content = gen(block, agent)
        if not content:
            continue
        (dynamic_parts if past_boundary else static_parts).append(str(content).strip())

    if dynamic_parts:
        return static_parts + [DYNAMIC_BOUNDARY] + dynamic_parts
    return static_parts
```

要点：

- 块按 JSON 中声明的顺序拼装，用空行分隔；`dynamic_boundary` 不产出内容，仅把其后内容划入动态区；
- 仅拼装 `enabled != false`、生成器存在且输出非空的块；
- 返回 `list[str]`：静态块在前，动态块在 `DYNAMIC_BOUNDARY` 之后（无动态块时不插入哨兵）；adapter 层按 boundary 切分并设置 `cache_control`（详见 [Prompt Cache 设计文档](prompt-cache-design.md)）；
- interactive（master）每轮循环重建 system prompt；autonomous（worker/lite）在 `__init__` 时拼装一次并缓存（`self._system_prompt`）。

### 3.2 块生成器（SYSTEM_BLOCK_GENERATORS）

定义于 `core/prompt_builder.py`：

```python
SYSTEM_BLOCK_GENERATORS: dict[str, Callable[[dict, Any], str]] = {
    "text": _gen_text,
    "tools": _gen_tools,
    "skills": _gen_skills,
    "context": _gen_context,
}
```

- `_gen_text(block, agent)`：直接返回块的 `content`；`content` 为字符串或字符串数组（按行拼装）。
- `_gen_tools(block, agent)`：从 `agent._active_tools`（启用工具，排除延迟工具 `_deferred_names`）调用 `core.prompt_sections._build_tools_section` 生成工具列表段，再叠加 `core.prompt_sections._build_deferred_tools_section` 的延迟工具摘要。
- `_gen_skills(block, agent)`：调用 `core.prompt_sections._build_skills_section(agent.role)`。
- `_gen_context(block, agent)`：调用 `core.prompt_sections.build_environment_context(agent.workspace_uuid, agent.cwd)`，结果缓存在 `agent._prompt_section_cache["env_context"]`（session 内复用，`/clear`、`/compact`、切换会话时清除）。
- `dynamic_boundary` 不是生成器：`build_system_prompt()` 在循环中特判该类型以切换分区。

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

角色定义位于 `core/agents/{role}.json`，由 `core/role_config.load_role()` 加载为 `RoleConfig`。与提示词相关的两个字段：

### 4.1 system_prompt.blocks

声明 system prompt 的拼装块（有序）：

```json
"system_prompt": {
  "blocks": [
    {"id": "role",   "type": "text",   "enabled": true, "content": ["...", "..."]},
    {"id": "tools",  "type": "tools",  "enabled": true},
    {"id": "skills", "type": "skills", "enabled": true},
    {"type": "dynamic_boundary", "enabled": true},
    {"type": "context", "enabled": true}
  ]
}
```

字段含义：

- `id`：块标识（仅命名，不参与拼装逻辑）；
- `type`：块类型，对应 `SYSTEM_BLOCK_GENERATORS` 的键（`dynamic_boundary` 为分区哨兵，不在生成器表中）；
- `enabled`：是否启用（默认 true）；
- `content`：仅 text 块需要，固定文案（字符串或字符串数组）。

> 原 `ROOT_PROMPT_TEMPLATE` / `SUB_PROMPT_TEMPLATE` 常量内容已整体迁入各角色 JSON 的 text 块 `content`，代码中不再有硬编码模板。

### 4.2 user_layers

声明需要注入的 user 消息层：

```json
"user_layers": [
  {"id": "project_instructions", "type": "claude_md", "enabled": true}
]
```

`id` 仅命名；`type` 对应层类型；`enabled` 控制启用。

### 4.3 三份角色 JSON 的块与层

| 角色 | 文件 | blocks | user_layers | mode |
|------|------|--------|-------------|------|
| master | `master.json` | role(text) / tools / skills / security(text) / dynamic_boundary / context | project_instructions(claude_md) | interactive |
| worker | `worker.json` | role(text) / tools / skills / security(text) / dynamic_boundary / context | task_message(task) / runtime_prompts(runtime) | autonomous |
| lite | `lite.json` | role(text) / tools / security(text) / dynamic_boundary / context | task_message(task) | autonomous |

- **master**：完整交互 Agent，注入项目指令（claude_md）；system prompt 动态区（`dynamic_boundary` 之后）注入环境上下文（context 块）；
- **worker**：完整自主 Agent，注入任务消息（task）与运行时提示（runtime：预算/检查/超时）；system prompt 动态区注入环境上下文（context 块）；
- **lite**：极简自主 Agent（仅 read/write/edit/bash/python/message_bus/clock），只注入任务消息（task），无 claude_md/runtime；环境上下文同样由 system prompt 动态区的 context 块注入。

---

## 五、user 层注入

### 5.1 生成器表（USER_LAYER_GENERATORS）

定义于 `core/prompt_builder.py`：

```python
USER_LAYER_GENERATORS: dict[str, Callable[[Any], dict | None]] = {
    "claude_md": _gen_claude_md,
}
```

每个生成器接收 agent，返回一条 `{"role": "user", "content": ...}` 消息 dict，或返回 `None`（不注入）。

> `context` 已不在生成器表中——它迁移到 system prompt 的 `context` 块（动态区，session 内缓存），不再是 user 层（见 §3.2 与[八、动态环境上下文](#八动态环境上下文)）。

### 5.2 claude_md 层（项目指令）

`_gen_claude_md(agent)` 调用 `core.prompt_sections.build_instructions_message(agent.cwd)`，每次调用从磁盘重读工作区根目录的项目指令文件（优先级 AGENTS.md > agent.md > CLAUDE.md > claude.md），以 `<system-reminder>` 包装为注入消息。未找到文件时返回 `None`（不注入）。详见[九、项目指令注入](#九项目指令注入)。

### 5.3 task 层（pinned 任务消息）

`task` 层不在生成器表中。autonomous（worker/lite）启动时由 `SessionRunner._build_task_message()` 构建任务消息，作为**第一条 user 消息**写入历史并带 `_meta.pinned=True`（免疫压缩），内容为：

- `## Assigned Task` 标题；
- `### Objective`：任务目标（`self.task`）；
- `### Execution Plan`：执行计划（`self.plan` 的编号步骤，有则添加）；
- 迭代额度预算说明（`max_iterations`）；
- 下放主代理会话级批准的命令（`build_approved_commands_section`，与主代理共享 `ApprovalStore` 时）。

### 5.4 runtime 层（运行时提示）

`runtime` 层同样不在生成器表中。autonomous 执行过程中由 SessionRunner 直接写入历史（均为 user 消息）：

| 提示 | 触发时机 | 阈值/说明 |
|------|----------|-----------|
| `_BUDGET_WARN_PROMPT`（额度预警） | `_inject_budget_notice` | 迭代 ≥ `max_iterations × 0.8`，各触发一次 |
| `_BUDGET_FINAL_PROMPT`（额度即将耗尽） | `_inject_budget_notice` | 迭代 ≥ `max_iterations × 0.95`，各触发一次 |
| `_CHECK_PROMPT`（检查阶段） | 主执行循环结束 | pinned 注入，进入检查阶段（`role_cfg.check_phase` 启用时） |
| `_TIMEOUT_WRAPUP_PROMPT`（超时总结） | 额度耗尽兜底 | `_wrapup_timeout_summary` 中注入，直接调 client.chat（不传 tools）生成总结 |

---

## 六、防连续合并

注入型 user 层与消息历史在 `SessionRunner._get_messages_with_header()` 中合并：

```python
messages = self.context.get_messages_with_header()
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
| 工具白名单 | shared + agent/ask_user | shared（去 cron/ask_user） | read/write/edit/bash/python/message_bus/clock |
| system prompt 块 | role/tools/skills/security/dynamic_boundary/context | role/tools/skills/security/dynamic_boundary/context | role/tools/security/dynamic_boundary/context |
| user 层 | claude_md | task / runtime | task |
| 项目指令注入 | ✅ | ❌ | ❌ |
| 环境上下文注入 | ✅（context 块，动态区） | ✅（context 块，动态区） | ✅（context 块，动态区） |
| 任务消息 | ❌（多轮对话） | ✅ pinned 首消息 | ✅ pinned 首消息 |
| 检查阶段 | — | ✅（check_phase） | ❌ |
| 预算预警 | — | ✅（budget_notice） | ❌ |
| 执行流程 | 多轮交互 | 目标→计划→执行→检查 | 目标→计划→执行 |

**worker 四阶段执行流程**：**目标→计划→执行→检查**。

- 启动时注入 pinned 任务消息（目标 + 计划）；
- 主执行循环结束后自动注入 `_CHECK_PROMPT`（同样 pinned），LLM 重新阅读任务目标与计划，**用工具取证**逐项验证结果、发现问题立即修复，最终总结须列出已验证/未验证项及修复内容，并将非显然的可复用知识用 `memory` 工具主动沉淀；
- 检查阶段有独立的迭代上限（`role_cfg.check_iterations`，默认 10；worker 设为 `null` 即不设上限，仅由总迭代额度兜底），超出后强制收尾；
- 检查阶段/预算预警由 `role_cfg.check_phase` / `role_cfg.budget_notice` 开关控制（lite 均关闭）。

---

## 八、动态环境上下文（build_environment_context）

原 `build_root_context` / `build_sub_context` 合并为单一函数 `core/prompt_sections.build_environment_context(workspace_uuid, cwd)`，各角色一致：

```python
def build_environment_context(workspace_uuid: str = "", cwd: str = "") -> str:
    """构建动态环境上下文（由 system prompt 的 context 块调用，session 内缓存）。"""
```

> 注：`core/prompt_sections.py` 中该函数的 docstring 仍写作"作为独立 user 消息段注入"，是 context 迁移到 system prompt 动态区之前的遗留描述。

包含段落：

| 段落 | 内容 |
|------|------|
| **Workspace** | 工作目录（CWD），所有工具执行的相对路径基准 |
| **Operating System** | `platform.system() + platform.release()` |
| **Shell Environment** | bash / pwsh / python 三工具分工表、路径格式转换规则 |
| **Python Environment** | 必须使用 python 工具，虚拟环境自动激活 |
| **Temporary Files** | 工作区临时目录（`{workspace}/.cili/tmp`，经 `get_workspace_data_dir()` 解析），写/删限工作区、越界审批；temp 工具创建 `{workspace}/.cili/tmp/{session_id}/`；子进程 `$CILI_TMP` 指向该工作区 tmp |
| **Memory** | 内存目录（workspace 的 `.cili/memory/`），v3 三层注入实际内容（preference 常驻 + MEMORY.md 索引 + summary.md 摘要），`memory(action='find')` 检索示例 |
| **Current Time** | 当前日期，提示用于解释相对/时效性请求 |

该内容由 system prompt 的 `context` 块承载，位于 `dynamic_boundary` 之后的动态区：生成结果按 session 缓存（`agent._prompt_section_cache["env_context"]`），`/clear`、`/compact`、切换会话时清除重算。动态区不参与跨用户（`scope=global`）缓存，因此其中的当前时间变化不会破坏静态区的前缀缓存。

**记忆注入（v3 三层）**：`_build_memory_sections()` 注入记忆目录的实际内容——① **preference 常驻段**（`### User Preferences (always-on)`，最多 10 条，过期条目附 stale 警告）；② **MEMORY.md 索引**（`### Memory Index`，全部条目的描述）；③ **summary.md 摘要**（`### Memory Summary`，截断至 2KB）。preference 为空时不注入画像内容（user-profile.md 回退已移除）。记忆系统不可用时降级为提示语，绝不阻塞请求。

---

## 九、项目指令注入

### 9.1 设计目标

允许用户在工作区根目录放置项目级指令文件（如 `AGENTS.md`、`CLAUDE.md`），Cili 在每次 LLM 调用时自动读取并注入到对话上下文中，使 Agent 了解项目特定的规则和约定。

### 9.2 文件搜索规则

`core/prompt_sections.find_project_instructions(cwd)` 按优先级搜索工作区根目录：

1. `AGENTS.md`
2. `agent.md`
3. `CLAUDE.md`
4. `claude.md`

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

位于 `core/prompt_sections.py`：

```python
_PROJECT_INSTRUCTION_FILES = ["AGENTS.md", "agent.md", "CLAUDE.md", "claude.md"]

def find_project_instructions(cwd: str) -> str | None:
    """在工作区根目录搜索项目指令文件。"""

def build_instructions_message(cwd: str) -> dict | None:
    """构建项目指令消息（<system-reminder> 包装，作为注入 user 消息）。"""
```

`build_instructions_message` 由 `_gen_claude_md`（claude_md 层）调用，仅在角色 JSON 的 user_layers 中声明了该层时生效（当前仅 master）。

---

## 十、提示缓存策略

system prompt 以 `dynamic_boundary` 分为静态区（`scope=global`，可跨用户缓存）与动态区（session 特定），user 层独立注入，缓存策略如下（详见 [Prompt Cache 设计文档](prompt-cache-design.md)）：

### 10.1 静态区（可缓存部分）

```
┌─ system prompt 静态区（dynamic_boundary 之前）──┐
│  role 块（text）                        [JSON 固定] │
│  tools 块（工具列表）                    [进程内固定] │
│  skills 块（技能列表）                   [进程内固定] │
└────────────────────────────────────────────────┘
```

- master（interactive）每轮重建 system prompt，但静态区块内容（角色文案/工具/技能）在进程生命周期内稳定；
- worker/lite（autonomous）在 `__init__` 时拼装一次并缓存，整个执行过程复用；
- 静态区设置 `scope='global'`，内容不变时跨会话永久命中。

### 10.2 动态部分

```
┌─ system prompt 动态区（dynamic_boundary 之后）──┐
│  context 块（环境上下文）               [session 内缓存] │
└────────────────────────────────────────────────┘
┌─ user 消息层（注入，不进入 system prompt）──────┐
│  claude_md（项目指令）                  [磁盘变化时] │
│  task（pinned 任务消息）                [执行固定]   │
│  runtime（预算/检查/超时）              [阶段触发]   │
└────────────────────────────────────────────────┘
```

动态区（context）与 user 层都放在静态区之后，因此静态区前缀保持逐字节稳定：

- 有利于 API 的前缀缓存命中（静态区 `scope='global'` 跨用户共享）；
- 环境上下文中的日期等变化落在动态区/session 缓存内，不会破坏静态区缓存。

### 10.3 缓存效果

| 组件 | 状态 | 原因 |
|------|------|------|
| role 块 | 静态（进程生命周期内不变） | JSON 固定文案 |
| tools 块 | 静态（进程生命周期内不变） | 工具实例缓存 |
| skills 块 | 静态（进程生命周期内通常不变） | 每次构建时重新扫描 |
| claude_md 层 | 动态（磁盘变化时） | 每次调用重读指令文件 |
| context 块 | 动态区（session 内缓存） | 含当前时间，`/clear`、`/compact`、切换会话时重算 |

---

## 十一、关键设计决策

### 11.1 为什么用块/层架构代替硬编码模板？

原 `ROOT_PROMPT_TEMPLATE` / `SUB_PROMPT_TEMPLATE` 常量把角色文案硬编码在代码中，角色差异靠分支逻辑实现。块/层架构下：

- 角色文案迁入 `core/agents/*.json` 的 text 块，改文案不用改代码；
- 块与层的启用/顺序由 JSON 声明，新增角色只需新增 JSON；
- 动态内容按 `dynamic_boundary` 分区：静态区跨用户共享缓存，动态区（context）session 内缓存。

### 11.2 为什么工具描述只有一行摘要？

完整工具定义（含参数 schema）通过 API 的 `tools` 字段发送，这是 API 原生支持的机制。系统提示中只需列出工具名称和一行描述，供 LLM 快速识别可用工具。

### 11.3 为什么技能只列摘要，不加载全文？

技能全文（Markdown）通常较长。全部加载会占用大量上下文窗口。采用**渐进加载**策略：

1. system prompt 的 skills 块只列技能 ID 和描述
2. Agent 根据任务判断需要哪个技能
3. 通过 `skill(action='read')` 按需加载完整内容

### 11.4 环境上下文为何放在 system prompt 的动态区？

环境上下文含当前日期等变化内容，若放在静态区（`scope='global'`）会破坏跨用户共享缓存。因此它作为 `context` 块置于 `dynamic_boundary` 之后，由 adapter 排除在 `scope='global'` 之外，并按 session 缓存（`_prompt_section_cache`），`/clear`、`/compact`、切换会话时才重算。早期版本曾将其作为 user 层注入 messages[0]，会因每次请求变化而破坏 messages 层的前缀缓存，现已迁移到 system prompt 动态区。

### 11.5 worker 与 master 环境一致性

所有角色的环境上下文都由同一个 `build_environment_context()` 生成（Workspace、OS、Shell、Python、Temporary Files、Memory、Current Time），内容一致。三个角色的 system prompt 都在 `dynamic_boundary` 之后放置 `context` 块，因此 master/worker/lite 均能获得一致的环境上下文（含用户偏好），同时不破坏静态区的跨用户缓存。

### 11.6 为什么系统提示词使用英文？

系统提示词发送给 LLM（Claude/GPT 等），英文指令对模型更友好，遵循效果更好。用户界面文本和工具输出使用中文。

---

*文档版本: v2.2*
*最后更新: 2026-10-09*
