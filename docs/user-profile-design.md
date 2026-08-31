# 用户属性系统设计文档

## 概述

用户属性（User Profile）系统独立于记忆系统（Memory），专门用于存储和管理用户的个人偏好、习惯和特征。这些信息在每次对话开始时自动加载到 Agent 上下文中，使 Agent 能够个性化地回应用户。

## 与记忆系统的关系

用户属性和记忆系统是两个独立的持久化系统，各司其职：

| 特性 | 用户属性 | 记忆系统 |
|------|---------|---------|
| 职责 | 描述"谁在使用" | 记录"做了什么" |
| 存储位置 | `data/agents/{uuid}/user-profile.json` | `data/agents/{uuid}/memory/` |
| 文件数量 | 单文件 | 多文件（按类型/主题组织） |
| 格式 | JSON | Markdown |
| 加载方式 | 每次对话自动加载到上下文 | 按需检索 |
| 更新方式 | cron 任务自动提取 | Agent 主动存储 |
| 类型 | 6 个预定义类别 | knowledge、skill |

## 存储设计

### 文件位置

```
data/agents/{uuid}/
├── setting.json            # 工作区配置
├── user-profile.json       # 用户属性（本系统设计对象）
├── sessions/               # 会话存储
└── memory/                 # 记忆系统（knowledge、skill）
```

### JSON 格式

```json
{
  "user_profile": {
    "age": "30",
    "gender": "男",
    "occupation": "后端开发工程师",
    "family": "",
    "nickname": "老王"
  },
  "reply_style": [
    "偏好简洁回复，不要过多解释",
    "模仿用户的表达风格"
  ],
  "expression_traits": [
    "提问简洁，喜欢用短句",
    "常用技术术语，不解释基础概念",
    "喜欢用列表/要点形式"
  ],
  "work_habits": [
    "变量命名用 snake_case",
    "提交信息用中文"
  ],
  "project_preferences": [
    "后端端口 8000",
    "数据库用 PostgreSQL"
  ],
  "common_location": [
    "上海"
  ]
}
```

### 6 个预定义类别

| 类别 | 说明 | 存储格式 |
|------|------|---------|
| `user_profile` | 用户画像（称呼、年龄、性别、职业等） | 结构化 dict（字段：nickname, age, gender, occupation, family） |
| `reply_style` | Agent 回复风格 | 字符串列表（≤10 条） |
| `expression_traits` | 用户表达特征（用于模仿） | 字符串列表（≤10 条） |
| `work_habits` | 工作习惯 | 字符串列表（≤10 条） |
| `project_preferences` | 项目偏好配置 | 字符串列表（≤10 条） |
| `common_location` | 常驻地点 | 字符串列表（≤10 条） |

## 自动提取机制

### Cron 任务配置

用户属性由 cron 任务自动从会话记录中提取，配置位于 `core/cron.d/extract_user_info.json`：

```json
{
  "name": "extract-user-info",
  "description": "每天扫描工作区，提取用户信息到 user-profile.json",
  "enabled": true,
  "schedule": {
    "type": "cron",
    "expr": "0 2 * * *"
  },
  "config": {
    "max_iterations": 50
  },
  "content": {
    "task": "扫描 data/agents/ 下的所有工作区（排除 system），检查是否需要更新用户画像...",
    "plan": [
      "列出 data/agents/ 下的所有目录",
      "对每个目录（排除 system）：",
      "  1. 检查 user-profile.json 的 updated_at",
      "  2. 扫描 sessions/ 下最新的 updated_at",
      "  3. 如果需要更新：读取所有 session → 提取用户消息 → 分析 → 写入 profile",
      "  4. 如果不需要更新：跳过",
      "完成后报告处理的工作区数量"
    ]
  }
}
```

**特点**：
- 每天凌晨 2 点执行（cron 表达式 `0 2 * * *`）
- 通过 RootAgent 执行（在 System workspace 的 "[Cron] 任务描述" session 中）
- RootAgent 自主决定执行策略，可能委派 SubAgent
- 内联 task/plan，不依赖 Python 脚本
- 比较 session 更新时间 vs profile 更新时间，按需提取

### 提取流程

```
1. Cron 触发（每天凌晨 2 点）
2. RootAgent 在 System workspace 执行任务
3. RootAgent 扫描工作区（排除 system）
4. 对每个工作区：
   a. 比较 session 的 metadata.updated_at vs user-profile.json 的 updated_at
   b. 如果 session 更新 → 提取用户消息 → 分析 6 个类别 → 合并写入 profile
   c. 如果无更新 → 跳过
5. 结果保存在 System workspace 的 "[Cron] 任务描述" session（UI 可见）
```

> **更新判断**：直接比较 session 的 `metadata.updated_at` 和 profile 的 `updated_at`，无需额外变量。

### 合并策略

- **列表类型**（reply_style 等）：去重追加新条目
- **结构化类型**（user_profile）：按字段覆盖更新
- **空值处理**：跳过空字符串和空列表

## 上下文加载

### 自动加载流程

用户属性在 `build_root_context()` 中自动加载，注入到每次对话的上下文中：

```python
# core/prompts.py

def build_root_context(workspace_uuid: str = "", cwd: str = "") -> str:
    # ... Workspace 和 Memory 部分 ...

    # User Profile（自动从 user-profile.json 加载）
    profile_path = get_user_profile_path(workspace_uuid)
    if profile_path.exists():
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                profile_data = json.load(f)
            if profile_data:
                parts.extend([
                    "",
                    "## User Profile",
                    "",
                    "The following was inferred from the user's past conversations. "
                    "Use it naturally to personalize your tone — never recite, "
                    "echo, or explicitly reference these items unless the user raises them first.",
                    "",
                ])
                for category, items in profile_data.items():
                    parts.append(f"**{category}**:")
                    if isinstance(items, dict):
                        for k, v in items.items():
                            if v:
                                parts.append(f"- {k}: {v}")
                    elif isinstance(items, list):
                        for item in items:
                            parts.append(f"- {item}")
                    parts.append("")
        except Exception:
            pass  # 文件损坏时静默跳过

    # ... Current Time 部分 ...
```

### 加载格式

加载后的上下文格式示例：

```markdown
## User Profile

The following was inferred from the user's past conversations. Use it naturally to personalize your tone — never recite, echo, or explicitly reference these items unless the user raises them first.

**user_profile**:
- age: 30
- gender: 男
- occupation: 后端开发工程师
- nickname: 老王

**reply_style**:
- 偏好简洁回复，不要过多解释
- 模仿用户的表达风格

**expression_traits**:
- 提问简洁，喜欢用短句
- 常用技术术语，不解释基础概念

**work_habits**:
- 变量命名用 snake_case
```

## 路径工具函数

`core/config.py` 提供统一的路径获取函数：

```python
def get_user_profile_path(workspace_uuid: str) -> Path:
    """Get the user profile path: data/agents/{uuid}/user-profile.json or workspace/user-profile.json if empty."""
    from core.config import get_workspace_data_dir
    return get_workspace_data_dir(workspace_uuid) / "user-profile.json"
```

## 设计原则

### 不在提示词中强调保存

用户属性的保存完全由 cron 任务自动处理，不需要在系统提示词中强调特定关键词来触发保存。这与记忆系统不同：

- **记忆系统**：Agent 根据用户指令（"记住这个"）或自主判断主动存储
- **用户属性**：由后台 cron 任务定期从会话记录中自动提取

### 轻量级设计

- 单文件存储，无需复杂的目录结构
- 6 个预定义类别，每个类别最多保留 10 条
- 文件控制在 500 字以内（约 200-300 token）

### 静默失败

- 文件不存在时不报错（从空对象开始）
- 文件损坏时静默跳过（不中断对话）

## 相关文件

| 文件 | 职责 |
|------|------|
| `core/config.py` | 提供 `get_user_profile_path()` 路径函数 |
| `core/prompts.py` | `build_root_context()` 自动加载用户属性 |
| `core/cron.d/extract_user_info.json` | Cron 任务配置（内联 task/plan，cron 表达式每天 2 点） |
| `core/tools/shared/memory.py` | 仅处理 knowledge 和 skill（不涉及用户属性） |

## 参考文档

- [记忆系统设计文档](memory-system-design.md)
- [Agent 架构设计](agent-design.md)

---

*文档版本: v1.3*
*最后更新: 2026-08-30*
