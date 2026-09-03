# 用户画像系统设计文档

## 概述

用户画像（User Profile）系统独立于记忆系统（Memory），用于存储和管理用户的个人特征、表达风格和行为模式。这些信息在每次对话开始时自动加载到 Agent 上下文中，使 Agent 能够个性化地回应用户。

## 与记忆系统的关系

用户画像和记忆系统是两个独立的持久化系统，各司其职：

| 特性 | 用户画像 | 记忆系统 |
|------|---------|---------|
| 职责 | 描述"谁在使用" | 记录"做了什么" |
| 存储位置 | `data/agents/{uuid}/user-profile.md` | `data/agents/{uuid}/memory/` |
| 文件数量 | 单文件 | 多文件（按类型/主题组织） |
| 格式 | Markdown（带 YAML frontmatter） | Markdown |
| 加载方式 | 每次对话自动加载到上下文 | 按需检索 |
| 更新方式 | cron 任务自动提取 | Agent 主动存储 |
| 维度 | 5 个（身份、表达风格、决策、边界、压力行为） | knowledge、skill |

## 存储设计

### 文件位置

```
data/agents/{uuid}/
├── setting.json            # 工作区配置
├── user-profile.md         # 用户画像（本系统设计对象）
├── sessions/               # 会话存储
└── memory/                 # 记忆系统（knowledge、skill）
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
- **全量扫描**：每次 cron 执行时全量扫描所有对话，直接生成最新结果

## 自动提取机制

### Cron 任务配置

用户画像由 cron 任务自动从会话记录中提取，配置位于 `core/cron.d/extract_user_info.json`：

```json
{
  "name": "extract-user-info",
  "description": "每天扫描工作区，从对话中提取用户画像到 user-profile.md",
  "enabled": true,
  "schedule": {
    "type": "cron",
    "expr": "0 2 * * *"
  },
  "content": {
    "task": "扫描 data/agents/ 下的所有工作区（排除 system）...",
    "plan": [
      "列出 data/agents/ 下的所有目录",
      "对每个目录（排除 system）：",
      "  1. 检查 user-profile.md 的 updated_at",
      "  2. 扫描 sessions/ 下最新的 updated_at",
      "  3. 如果需要更新：读取所有 session → 提取用户消息 → 生成 markdown → 写入 profile",
      "  4. 如果不需要更新：跳过",
      "完成后报告处理的工作区数量"
    ]
  }
}
```

**特点**：
- 每天凌晨 2 点执行（cron 表达式 `0 2 * * *`）
- 全量扫描所有对话，直接生成最新结果，无需合并历史
- 通过 SubAgent 执行（在 System workspace 的 "[Cron] 任务描述" session 中）
- 结果保存在 System workspace 的 session 中（UI 可见）

### 提取流程

```
1. Cron 触发（每天凌晨 2 点）
2. SubAgent 在 System workspace 执行任务
3. SubAgent 扫描工作区（排除 system）
4. 对每个工作区：
   a. 比较 session 的 metadata.updated_at vs user-profile.md 的 updated_at
   b. 如果 session 更新 → 提取用户消息 → 分析 5 个维度 → 生成 markdown
   c. 如果无更新 → 跳过
5. 结果保存在 System workspace 的 "[Cron] 任务描述" session（UI 可见）
```

> **更新判断**：直接比较 session 的 `metadata.updated_at` 和 profile 的 `updated_at`。

## 上下文加载

### 自动加载流程

用户画像在 `build_root_context()` 中自动加载，注入到每次对话的上下文中：

```python
# core/prompts.py

def build_root_context(workspace_uuid: str = "", cwd: str = "") -> str:
    # ... Workspace 和 Memory 部分 ...

    # User Profile（自动从 user-profile.md 加载）
    profile_path = get_user_profile_path(workspace_uuid)
    if profile_path.exists():
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 解析 YAML frontmatter（如果有）
            if content.startswith("---"):
                parts_end = content.find("---", 3)
                if parts_end != -1:
                    content = content[parts_end + 3:].strip()

            if content:
                parts.extend([
                    "",
                    "## User Profile",
                    "",
                    "The following describes the person you are currently chatting with, "
                    "inferred from their past conversations. "
                    "Use these insights naturally to personalize your responses — "
                    "match their communication style, anticipate their needs, and adapt to their preferences. "
                    "Never recite, echo, or explicitly mention these observations unless they bring it up first.",
                    "",
                    content,
                ])
        except Exception:
            pass  # 文件损坏时静默跳过

    # ... Current Time 部分 ...
```

### 加载格式

加载后的上下文格式示例：

```markdown
## User Profile

The following describes the person you are currently chatting with, inferred from their past conversations. Use these insights naturally to personalize your responses — match their communication style, anticipate their needs, and adapt to their preferences. Never recite, echo, or explicitly mention these observations unless they bring it up first.

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

## 路径工具函数

`core/config.py` 提供统一的路径获取函数：

```python
def get_user_profile_path(workspace_uuid: str) -> Path:
    """Get the user profile path: data/agents/{uuid}/user-profile.md or workspace/user-profile.md if empty."""
    if not workspace_uuid:
        return PROJECT_ROOT / "workspace" / "user-profile.md"
    return AGENTS_DIR / workspace_uuid / "user-profile.md"
```

## 设计原则

### Markdown 格式

- 人类可直接阅读和编辑
- prompts.py 无需渲染逻辑，直接读取内容插入上下文
- cron 任务的 LLM 直接生成 markdown，无需转换为 JSON

### 不在提示词中强调保存

用户画像的保存完全由 cron 任务自动处理，不需要在系统提示词中强调特定关键词来触发保存。这与记忆系统不同：

- **记忆系统**：Agent 根据用户指令（"记住这个"）或自主判断主动存储
- **用户画像**：由后台 cron 任务定期从会话记录中自动提取

### 轻量级设计

- 单文件存储，无需复杂的目录结构
- 5 个提取维度，按需写入（无信息的维度不写）
- 文件控制在 500 字以内（约 200-300 token）

### 静默失败

- 文件不存在时不报错（跳过 User Profile 部分）
- 文件损坏时静默跳过（不中断对话）

## 相关文件

| 文件 | 职责 |
|------|------|
| `core/config.py` | 提供 `get_user_profile_path()` 路径函数 |
| `core/prompts.py` | `build_root_context()` 自动加载用户画像 |
| `core/cron.d/extract_user_info.json` | Cron 任务配置（内联 task/plan，cron 表达式每天 2 点） |
| `core/tools/shared/memory.py` | 仅处理 knowledge 和 skill（不涉及用户画像） |

## 参考文档

- [记忆系统设计文档](memory-system-design.md)
- [Agent 架构设计](agent-design.md)

---

*文档版本: v2.0*
*最后更新: 2026-09-02*
