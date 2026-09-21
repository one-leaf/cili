# 用户画像系统设计文档

## 概述

用户画像（User Profile）描述用户的个人特征、表达风格和行为模式。当前实现中，画像内容由 **preference 类型记忆条目**承载，与记忆系统共用存储与提取/整合流水线，并在每次对话开始时自动加载到 Agent 上下文中，使 Agent 能够个性化地回应用户。

## 与记忆系统的关系

用户画像和记忆系统各司其职：画像内容以 **preference 类型记忆条目**承载，与记忆系统共用存储与提取/整合流水线。两者对比：

| 特性 | 用户画像 | 记忆系统 |
|------|---------|---------|
| 职责 | 描述"谁在使用" | 记录"做了什么" |
| 存储位置 | `{directory}/.cili/memory/entries/preference/`（preference 条目） | `{directory}/.cili/memory/` |
| 文件数量 | 多条目（每维一条 preference） | 多文件（按类型/主题组织） |
| 格式 | Markdown | Markdown |
| 加载方式 | 每次对话自动加载到上下文 | 按需检索 |
| 更新方式 | 经记忆提取/整合流水线维护 | Agent 主动存储 |
| 维度 | 5 个（身份、表达风格、决策、边界、压力行为） | fact、preference、skill、reference |

> **状态说明**：`user-profile.md` 单文件方案已废弃（v3.1 起移除，不再生成、不再读取）。用户画像完全由 preference 类型记忆条目承载，经记忆提取/整合流水线维护。

## 存储设计

### 文件位置

```
{directory}/.cili/
├── approvals.json          # 写/删越界审批记录
├── tmp/                    # 工作区临时目录
├── sessions/               # 会话存储
└── memory/                 # 记忆系统（fact、preference、skill、reference）
    └── entries/preference/ # 用户画像条目（preference 类型）
```

### Markdown 格式

```markdown
---
updated_at: "2026-09-02 04:30:00"
---

## 身份
- **花名**: OneLeaf
- **基本信息**: 学生 后端工程师 男
- **性格**: INTJ 摩羯座 甩锅高手
- **地点**: 深圳

## 表达风格
- **语气**: 直接简洁的指令式表达
- **口头禅**: 查一下, 帮我看看
- **句式**: 短句为主，开门见山
- **Emoji**: 不用 emoji
- **正式程度**: 非常口语化

## 决策与判断
效率优先，果断，直接否定不认可的方案

## 边界与雷区
- 不喜欢过度封装
- 拒绝照搬外部材料

## 压力下行为
deadline 前会抱怨但执行力强
```

### 5 个提取维度

| 维度 | 说明 | 格式 |
|------|------|------|
| `身份` | 花名、基本信息、性格、地点 | 结构化列表 |
| `表达风格` | 语气、口头禅、句式、emoji、正式程度 | 结构化列表 |
| `决策与判断` | 优先考量、果断程度、拒绝方式 | 一句话概括 |
| `边界与雷区` | 明确拒绝的场景、抵触的方案、回避的话题 | 列表 |
| `压力下行为` | 被催时的反应、焦虑表达方式 | 一句话概括 |

### 设计要点

- **被动识别**：从对话中捕捉，不主动询问
- **无信息不写**：如果某维度在对话中完全没有信息，不写该维度
- **经记忆流水线维护**：画像由 preference 条目承载，经记忆提取/整合流水线维护，无需独立 cron 任务

## 提取与维护机制

用户画像不再由独立的 cron 任务提取（`core/cron.d/extract_user_info.json` 已移除），而是作为 **preference 类型记忆条目**承载，经记忆提取/整合流水线维护：

- **记忆提取**：Agent 通过 `memory(action="store", type="preference")` 将对话中捕捉到的用户特征、表达风格、决策偏好写入工作区记忆（`memory/entries/preference/{name}.md`），同时追加到 journal.jsonl 审计日志
- **记忆整合**：系统级 cron 任务 `core/cron.d/memory_consolidation.json`（每 2 小时）调用 `memory(action="consolidate")`，把各工作区 journal 中待整合的记录整合为条目、刷新 summary、推进游标并 git 提交
- **上下文注入**：各工作区 preference 条目常驻注入 Agent 上下文（详见「上下文加载」）

### 数据流向

```
用户对话 → 提取（memory store → journal.jsonl） → 整合（memory consolidate）
                                                       ↓
                context 注入（preference 常驻 + MEMORY.md 索引 + summary.md 摘要）
```

> **更新判断**：以 memory journal 的游标和条目的 created/updated 为准，由整合流水线负责增量合并；不再对比 profile 文件的 updated_at。

## 上下文加载

### 自动加载流程

用户画像在 `build_environment_context()` 中自动加载（所有角色相同），作为 `context` user 层注入到每次对话的上下文中。当前实现为 v3 三层记忆注入：preference 常驻 + MEMORY.md 索引 + summary.md 摘要（preference 为空时不注入任何画像内容，不再有 user-profile.md 回退）：

```python
# core/prompts.py

def build_environment_context(workspace_uuid: str = "", cwd: str = "") -> str:
    # ... Workspace 和 Memory 部分 ...

    # 记忆注入（v3 三层：preference 常驻 + MEMORY.md 索引 + summary.md 摘要）
    parts.extend(_build_memory_sections(memory_dir, workspace_uuid))

    # ... Current Time 部分 ...


def _build_memory_sections(memory_dir: str, workspace_uuid: str = "") -> list[str]:
    """构建记忆注入段：preference 常驻 + MEMORY.md 索引 + summary.md 摘要。"""
    # 1) preference 常驻（最多 _MEMORY_PREFERENCE_CAP=10 条，stale 标注）
    prefs = store.list(type_="preference")
    if prefs:
        lines.append("### User Preferences (always-on)")
        for p in prefs[:10]:
            stale = " ⚠ stale, verify before applying" if store.is_stale(p) else ""
            lines.append(f"- {p.get('title', p['name'])}: {p.get('description', '')}{stale}")

    # 2) MEMORY.md 索引（"### Memory Index (descriptions of all entries)"）
    # 3) summary.md 摘要（"### Memory Summary"，截断 2KB）
    # 4) 检索提示：memory(action="find") / memory(action="read")
```

### 加载格式

加载后的上下文格式示例（`## Memory` 段，preference 常驻 + 索引 + 摘要）：

```markdown
## Memory

The workspace's persisted memory is injected below: always-on preferences, the MEMORY.md index, and the global summary. Apply relevant facts, preferences and skills instead of relying on model memory.

### User Preferences (always-on)
- 表达风格: 直接简洁的指令式表达，短句为主，不用 emoji
- 花名: OneLeaf
- 性格: INTJ 摩羯座
- 地点: 深圳

### Memory Index (descriptions of all entries)
- preference/表达风格: 直接简洁的指令式表达
- preference/基本信息: 学生 后端工程师 男

### Memory Summary
（summary.md 摘要内容，截断 2KB）

Search/recall: `memory(action="find", query="keyword")` lists matching entries with descriptions; then `memory(action="read", name="<name>")` reads the full body.
```

## 路径工具函数

`core/config.py` 提供统一的路径获取函数：

```python
def get_workspace_data_dir(workspace_uuid: str) -> Path:
    """Get the .cili data directory for a workspace: {directory}/.cili/."""
    ...
```

`get_user_profile_path()` 已删除——user-profile.md 不再存在，画像由 preference 条目承载（`{directory}/.cili/memory/entries/preference/{name}.md`，经 `memory(action="read", name=...)` 读取）。

## 设计原则

### 条目化存储格式

- 人类可直接阅读和编辑（每条一个 markdown 文件）
- preference 条目写入 `memory/entries/preference/{name}.md`，由记忆整合流水线生成
- MEMORY.md 索引 + summary.md 摘要由整合流水线维护，prompts.py 负责拼装注入

### 不在提示词中强调保存

用户画像以 preference 条目承载，与其余记忆共用同一套提取/整合流水线，不需要在系统提示词中强调特定关键词来触发保存：

- **记忆系统**：Agent 根据用户指令（"记住这个"）或自主判断主动存储，写入 memory entries
- **用户画像**：同样是 preference 类型条目，经记忆整合流水线定期整合维护

### 轻量级设计

- 与记忆系统统一，按 preference 条目存储，无需独立的单文件目录结构
- 5 个提取维度，按需写入（无信息的维度不写）
- 条目短小精炼，常驻注入有上限（`_MEMORY_PREFERENCE_CAP = 10` 条）

### 静默失败

- 文件不存在时不报错（跳过 User Profile 部分）
- 文件损坏时静默跳过（不中断对话）

## 相关文件

| 文件 | 职责 |
|------|------|
| `core/config.py` | 提供 `get_workspace_data_dir()` 路径解析（`{directory}/.cili/`） |
| `core/prompts.py` | `build_environment_context()` 三层记忆注入（preference 常驻 + MEMORY.md 索引 + summary.md 摘要，context user 层） |
| `core/memory_store.py` | `MEMORY_TYPES` 含 preference，preference 条目存储/检索 |
| `core/tools/memory.py` | memory 工具（store/find/read/.../consolidate），preference 类型写入与整合 |
| `core/cron.d/memory_consolidation.json` | 记忆整合 cron 任务（每 2 小时，整合 journal 到条目） |

## 参考文档

- [记忆系统设计文档](memory-system-design.md)
- [Agent 架构设计](agent-design.md)

---

*文档版本: v3.1*
*最后更新: 2026-09-21*（v3.1：user-profile.md 废弃移除，画像统一由 preference 条目承载）
