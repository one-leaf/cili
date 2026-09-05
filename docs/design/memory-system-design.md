# 记忆系统设计文档

本文档描述记忆系统（Memory），涵盖 knowledge 和 skill 两种记忆类型。用户属性（User Profile）为独立系统，见 [user-profile-design.md](user-profile-design.md)。

## 一、功能概述

为 Cili Agent 添加长期记忆能力，让 Agent 能够跨会话记住知识和技能，提升任务完成效率。

### 记忆类型总览

| 记忆类型 | 说明 | 存储位置 | 文件组织 | 特点 |
|---------|------|---------|---------|------|
| **knowledge** | 知识（长期） | `memory/knowledge/` | 按主题+日期分目录 | 事实性，按主题检索，Markdown 格式 |
| **skill** | 技能（长期） | `memory/skills/` | 每个技能一个目录 | 可复用技术，渐进式加载，Markdown 格式 |

**工具结果自动入库**：web_search、browser 抓取的工具结果可由 Agent 判断后存入 knowledge，避免重复搜索。

---

## 二、存储设计

### 2.1 目录结构

```
data/agents/{uuid}/
├── sessions/
├── setting.json
├── user-profile.json           # 用户属性（独立系统）
└── memory/
    ├── knowledge/              # 知识库（按主题分目录，再按日期）
    │   ├── api-design/         # API 设计主题
    │   │   ├── 2024-01-15/     # 按日期分子目录
    │   │   │   ├── restful.md
    │   │   │   └── graphql.md
    │   │   └── 2024-01-16/
    │   │       └── ...
    │   ├── database/           # 数据库主题
    │   │   └── 2024-01-15/
    │   │       └── mysql.md
    │   ├── python/             # Python 主题
    │   │   └── 2024-01-15/
    │   │       └── asyncio.md
    │   └── misc/               # 杂项（无法归类的知识）
    │       └── 2024-01-15/
    │           └── markdown.md
    └── skills/                 # 技能库（每个技能一个目录）
        ├── python-async/       # Python 异步编程技能
        │   └── skill.md        # 技能定义（Markdown + frontmatter）
        ├── k8s-deploy/         # Kubernetes 部署技能
        │   └── skill.md
        └── ...
```

**Knowledge 文件示例**（api-design/2024-01-15/restful.md）：
```markdown
---
title: RESTful API 规范
source: manual
references:
  - "file:E:/docs/api-spec.md"
  - "session:abc123"
time: 2024-01-15 14:30:00
tags: [api, 规范, restful]
---

RESTful 设计，端口 8080，使用 JWT 认证，统一返回格式
```

**Knowledge 文件示例**（api-design/2024-01-15/naming.md）：
```markdown
---
title: 接口命名规范
source: manual
time: 2024-01-15 15:00:00
tags: [api, 命名]
---

使用 camelCase 命名，复数名词表示集合，如 /users, /orders
```

**misc 目录示例**（misc/2024-01-15/markdown.md，存放无法归类的知识）：
```markdown
---
title: Markdown 语法
source: web_search
references:
  - "web:https://example.com/markdown-guide"
time: 2024-01-17 09:00:00
tags: [markdown, 语法]
---

使用 # 表示标题，** 表示粗体...
```

**references 字段说明**：
- 类似论文引用，支持多条来源累积
- 格式：`file:路径`（文件）、`session:id`（对话会话）、`web:url`（网页）
- 每次更新同名知识时，新的 source_ref 会追加到列表（自动去重）
- 便于追溯知识来源，支持后续验证

**为什么放在 data 目录下？**
- 与 sessions/config 统一，属于工作区的"数据目录"
- memory 是 Agent 的数据，不是用户的工作文件，隔离更清晰
- 避免污染工作区，用户不会误删
- 符合现有架构（sessions 就在 data 下面）

### 2.2 文件命名规则

- **Knowledge**: 按主题分目录，再按日期分子目录
  - 主题目录名使用英文 kebab-case（如 `api-design`, `database`）
  - 日期子目录格式 `YYYY-MM-DD`
  - 文件名描述内容（如 `restful.md`, `asyncio.md`）
  - 无法归类的知识存入 `misc/` 目录
  - 路径格式：`knowledge/{topic}/{date}/{filename}.md`
- **Skill**: 每个技能一个目录，目录内包含 skill.md 文件
  - 技能目录名使用英文 kebab-case（如 `python-async`, `k8s-deploy`）
  - 技能文件固定命名为 `skill.md`
  - 路径格式：`skills/{skill-name}/skill.md`
  - **支持渐进式加载**：系统提示词中仅注入摘要，完整内容按需读取

**文件数量控制**：
- Knowledge 主题数量无硬限制，由 Agent 根据实际需要管理
- Agent 可在适当时候合并相关小主题或归档旧知识
- Knowledge 所有主题统一按日期组织：`knowledge/{topic}/{YYYY-MM-DD}/`

### 2.3 记忆条目格式

#### Knowledge（知识）— 按主题分目录，Markdown 格式

每个主题一个目录，目录下可有多个内容文件，支持丰富的知识结构。

**目录结构**：
```
knowledge/
├── api-design/         # 主题目录
│   ├── 2024-01-15/     # 日期子目录
│   │   ├── restful.md  # 内容文件
│   │   └── graphql.md
│   └── 2024-01-16/
│       └── ...
├── python/
│   └── 2024-01-15/
│       └── asyncio.md
└── misc/               # 杂项目录
    └── 2024-01-15/
        └── markdown.md
```

**内容文件格式**（Markdown with frontmatter）：
```markdown
---
title: {标题}
source: {manual / web_search / browser / python}
references:
  - "file:E:/docs/config.yaml"
  - "session:abc123"
time: 2024-01-15 14:30:00
tags: [标签1, 标签2]
---

具体知识内容...
```

**要素说明**：
| 字段 | 作用 | 检索方式 |
|------|------|----------|
| 目录名 | 主题分类 | `find` 列出主题 |
| 文件名 | 内容描述 | 直接 `read` |
| source | 知识来源（用户/搜索/浏览器/代码） | `grep "source: web_search"` |
| references | 知识引用来源列表（文件/会话/网页） | `grep "references:"` |
| tags | 细粒度分类 | `grep "tags:"` |
| 内容 | 具体知识 | `grep "关键词"` |

**示例**（api-design/restful.md）：
```markdown
---
title: RESTful API 规范
source: manual
references:
  - "file:E:/docs/api-spec.md"
time: 2024-01-15 14:30:00
tags: [api, 规范, restful]
---

RESTful 设计，端口 8080，使用 JWT 认证，统一返回格式
```

---

#### Skill（技能）— 可复用技术

每个技能一个目录，Agent 在任务前通过搜索发现相关技能。

**目录结构**：
```
skills/
├── python-async/           # 技能目录
│   └── skill.md            # 技能定义文件
├── k8s-deploy/             # 另一个技能
│   └── skill.md
└── cors-fix/
    └── skill.md
```

**skill.md 文件格式**（Markdown with frontmatter）：
```markdown
---
name: {技能名称（最多64字符）}
description: {技能描述（最多200字符，用于搜索匹配）}
tags: [标签1, 标签2]
created: 2024-01-15 10:30:00
updated: 2024-01-15 10:30:00
---

## 概述

技能详细说明...

## 使用场景

什么情况下使用这个技能...

## 步骤

1. 第一步...
2. 第二步...

## 示例

具体示例...
```

**要素说明**：
| 字段 | 作用 | 检索方式 |
|------|------|----------|
| skill_name | 技能标识（目录名） | `find` 列出所有技能 |
| name | 技能显示名称 | `grep` 搜索 |
| description | 技能描述 | `grep` 搜索匹配 |
| tags | 技能分类 | `grep "tags:"` |
| created/updated | 时间戳 | 排序 |

**技能发现流程**：
1. Agent 收到任务请求时，先用 `grep` 搜索 skills 目录
2. 搜索技能名称、描述、标签中的关键词
3. 返回匹配的技能名称和文件路径
4. Agent 判断是否需要使用 `read` 工具加载完整 skill.md 文件

**示例**（skills/python-async/skill.md）：
```markdown
---
name: Python 异步编程
description: 使用 asyncio 进行并发编程的技术，包括事件循环、协程、Task 管理
tags: [python, async, 并发]
created: 2024-01-15 10:30:00
updated: 2024-01-15 10:30:00
---

## 概述

asyncio 是 Python 的异步 I/O 库，使用 async/await 语法...

## 使用场景

- 需要并发处理多个 I/O 操作
- 网络请求、文件读写等 I/O 密集型任务

## 核心概念

### 协程
使用 `async def` 定义协程函数...

### 事件循环
使用 `asyncio.run()` 启动事件循环...
```

---

## 三、记忆类型定义

### 3.1 Knowledge（知识库）

**用途**: 存储用户提供的知识、事实、规范、文档摘要等。工具搜索结果（web_search、browser 抓取）也可存入此类型。

**示例**:
- "我们公司的 API 规范是 RESTful，端口用 8080"
- "数据库表 users 的字段有 id, name, email, created_at"
- "项目部署流程：先 build，再 docker build，最后 kubectl apply"
- "Python asyncio 用法：使用 async/await 关键字..."（来自 web_search）

**特点**: 客观事实，Markdown 格式，可被引用，需要时可检索

### 3.2 Skill（技能）

**用途**: 存储可复用的技术和工作流，替代原有的 experience 类型

**示例**:
- "Python 异步编程"：使用 asyncio 进行并发编程的完整指南
- "K8s 部署"：将 Node.js 项目部署到 Kubernetes 的步骤和注意事项
- "CORS 错误修复"：解决跨域问题的标准方法

**特点**: 
- 任务导向，包含完整的解决方案和步骤
- 支持渐进式加载：系统提示词中仅显示摘要，完整内容按需读取
- Markdown 格式，便于阅读和维护
- 每个技能一个目录，便于扩展资源文件

---

## 四、触发机制

### 4.1 存储触发

#### 类型一：显式存储触发

用户明确要求存储：
- "记下来"、"记录下来"
- "记住这个"、"保存这个"
- "这个很重要"、"以后还会用到"

#### 类型二：会话结束时的主动记忆

**Agent 主动判断**：
- 会话结束时（Agent 完成所有工具调用）
- 自动判断是否有值得记忆的内容
- 如果有，主动调用 memory 工具并告知用户

**示例**：
```
Agent: 任务完成。我发现你使用了特定的部署方式，要记录为技能吗？
用户: 好的
[Agent 调用 memory 存储 skill]
```

#### 类型四：工具结果自动入库

当 Agent 使用 `web_search`、`browser`、`python` 等工具获取结果后，自动判断是否有值得保存的知识：

**触发条件**：
- 工具返回了有价值的信息（搜索结果、网页内容、代码输出等）
- Agent 判断该信息在当前或未来对话中可能被再次使用
- 用户明确要求"记住这个结果"

**处理逻辑**：
1. 工具执行完成后，Agent 判断结果的价值
2. 如果有价值，判断是否有明确主题
   - 有主题：topic 设为对应值（如 `python`, `api-design`）
   - 无主题：topic 不填，存入 `misc/` 目录
3. 调用 memory 工具存储，source 设为对应来源
4. 告知用户已保存（"已将搜索结果保存到知识库"）

**文件数量管理**：
- Knowledge 主题数量无硬限制
- Agent 可在适当时候整理主题：
  - 合并相关小主题（如 `flask/` + `django/` → `web-framework/`）
  - 将零散知识集中到 `misc/`

**示例**：
```
用户: 帮我搜一下 Python asyncio 怎么用
Agent: [调用 web_search 搜索 "Python asyncio"]
Agent: [获取到搜索结果]
Agent: Python asyncio 的核心用法如下...
Agent: 已将这条知识保存到知识库，下次可以直接查阅。
[Agent 调用 memory store, topic="python", source="web_search"]

用户: 帮我搜一下 Markdown 表格怎么写
Agent: [调用 web_search 搜索]
Agent: [获取到结果]
Agent: Markdown 表格语法如下...
Agent: 已保存。
[Agent 调用 memory store, 不填 topic → 存入 misc/]
```

**注意**：
- 不是所有工具结果都入库，Agent 需要判断价值
- 重复的知识不重复存储
- source 字段标记来源，方便后续按来源检索
- 无法归类的知识存入 misc/，避免目录爆炸

### 4.2 检索触发

#### 方案 A：关键词触发（原始需求）

触发词：
- "查一下记忆"、"回忆一下"
- "之前说过"、"我记得"
- "根据我的偏好"

**问题**:
- 用户需要记住这些触发词
- 不够自然

#### 方案 B：上下文自动检索（推荐）

**自动检索策略**:
1. **Knowledge 类型**: 当用户提到"之前说的"、"那个规范"等指代性词汇时，自动检索
2. **Skill 类型**: Agent 在收到任务时主动搜索 skills 目录，找到匹配的技能后加载完整内容

**实现**:
- 在 system prompt 中说明记忆检索规则
- Agent 在收到任务时先用 `grep` 搜索 skills 和 knowledge
- 返回匹配项的名称和文件路径，判断是否需要加载完整内容

---

## 五、工具设计

### 5.1 工具概览

| 工具 | 职责 | 说明 |
|------|------|------|
| `memory` | 长期记忆存储 | 存储 knowledge/skill，支持 source 标记来源 |
| `grep` + `read` | 长期记忆检索 | 搜索 memory 目录下的知识/技能 |

**设计原则**：
- memory 工具负责**存储和管理**长期记忆，检索交给 grep/read
- knowledge 可标记来源（manual/web_search/browser/python），工具结果可由 Agent 判断后存入
- Agent 在收到任务时先用 `grep` 搜索 skills 和 knowledge，返回匹配项后再判断是否加载完整内容

### 5.2 memory 工具参数

```json
{
  "name": "memory",
  "description": "长期记忆工具，用于存储跨会话的知识和技能。Knowledge 使用 Markdown 格式存储，Skill 使用带 frontmatter 的 Markdown 格式。",
  "parameters": {
    "type": "object",
    "properties": {
      "action": {
        "type": "string",
        "enum": ["store", "update", "delete"],
        "description": "操作类型：store（创建）、update（修改）、delete（删除）。检索使用 grep/read/find 工具完成。"
      },
      "memory_type": {
        "type": "string",
        "enum": ["knowledge", "skill"],
        "description": "记忆类型：knowledge=知识（Markdown），skill=技能（Markdown）"
      },
      "topic": {
        "type": "string",
        "description": "【knowledge 专用】主题目录名（英文 kebab-case），如 'api-design'。不填则存入 misc/ 目录"
      },
      "skill_name": {
        "type": "string",
        "description": "【skill 专用】技能目录名（英文 kebab-case），如 'python-async'。skill 类型必填"
      },
      "filename": {
        "type": "string",
        "description": "【knowledge 专用】内容文件名（如 'restful.md'）。不填则根据 title 自动生成"
      },
      "title": {
        "type": "string",
        "description": "【knowledge 专用】记忆标题"
      },
      "name": {
        "type": "string",
        "description": "【skill 专用】技能显示名称（最多 64 字符）"
      },
      "description": {
        "type": "string",
        "description": "【skill 专用】技能描述（最多 200 字符，用于渐进式加载）"
      },
      "content": {
        "type": "string",
        "description": "内容正文（knowledge 或 skill 使用）。内容必须完整：如果源文档被截断，应先用 offset/limit 获取剩余部分再存储，不要存储带'内容已截取'等占位符的不完整内容"
      },
      "tags": {
        "type": "array",
        "items": {"type": "string"},
        "description": "标签列表，用于分类和检索"
      },
      "source": {
        "type": "string",
        "enum": ["manual", "web_search", "browser", "python"],
        "description": "知识来源：manual=用户主动提供，web_search=搜索结果，browser=浏览器抓取，python=代码执行结果。仅 knowledge 类型使用"
      },
      "source_ref": {
        "type": "string",
        "description": "知识引用来源。格式：'file:E:/docs/config.yaml'（文件）、'session:abc123'（对话会话）、'web:https://...'（网页）。添加到 references 列表。仅 knowledge 使用。"
      }
    },
    "required": ["action", "memory_type"]
  }
}
```

**参数补充说明**：
- `topic`：不填时默认为 `"misc"`，知识存入 `knowledge/misc/` 目录。**注意**：代码中参数描述写为 "Required for knowledge"，但实际验证并未强制要求（`kwargs.get("topic", "misc")`），不填时自动归入 `misc/`
- `skill_name`：必须是有意义的 kebab-case 名称，代码会拒绝 UUID 格式的命名（如 `skill-a1b2c3d4` 或 36 位 UUID）
- `source`：不填时默认为 `"manual"`
- `source_ref`：支持多种来源格式，类似论文引用。每次更新同名知识时，新的 source_ref 会追加到 references 列表（自动去重）

**自动查重行为**：
- `store` knowledge 时，自动搜索所有 topic 下是否有相同 title 的知识
- 如果找到，自动转为 update 并合并 references
- skill 同理：同 skill_name 已存在时自动 update，保留 created 时间

### 5.3 使用示例

```yaml
# ===== Knowledge 操作 =====

# 存储知识（有明确主题和文件名）
# topic="api-design", filename="restful.md" -> knowledge/api-design/2024-01-15/restful.md
memory(action="store", memory_type="knowledge", title="RESTful API 规范", 
       topic="api-design", filename="restful.md",
       content="RESTful 设计，端口 8080，使用 JWT 认证，统一返回格式",
       source="manual",
       tags=["api", "规范", "restful"])

# 存储知识（有主题，自动生成文件名）
# topic="python", 不填 filename -> knowledge/python/2024-01-15/{自动}.md
memory(action="store", memory_type="knowledge", title="Python asyncio 用法", 
       topic="python",
       content="asyncio 使用 async/await 关键字，事件循环，Task 并发...",
       source="web_search",
       tags=["python", "asyncio", "并发"])

# 存储知识（无法归类，存入 misc/）
# 不填 topic -> knowledge/misc/2024-01-15/{自动}.md
memory(action="store", memory_type="knowledge", title="Markdown 语法",
       content="使用 # 表示标题，** 表示粗体...",
       source="web_search",
       tags=["markdown", "语法"])

# 更新知识
memory(action="update", memory_type="knowledge", topic="api-design", 
       title="RESTful API 规范",
       content="RESTful 设计，端口 8080，使用 JWT 认证...（更新内容）")

# 删除知识
memory(action="delete", memory_type="knowledge", topic="api-design", 
       title="RESTful API 规范")

# ===== Skill 操作 =====

# 存储技能
# skill_name="python-async" -> skills/python-async/skill.md
memory(action="store", memory_type="skill",
       skill_name="python-async",
       name="Python 异步编程",
       description="使用 asyncio 进行并发编程的技术，包括事件循环、协程、Task 管理",
       content="## 概述\n\nasyncio 是 Python 的异步 I/O 库...\n\n## 使用场景\n\n- 需要并发处理多个 I/O 操作...",
       tags=["python", "async", "并发"])

# 读取技能完整内容（使用 read 工具直接读取）
read(file_path="data/agents/{uuid}/memory/skills/python-async/skill.md")

# 列出所有技能（使用 find 工具）
find(path="data/agents/{uuid}/memory/skills/", pattern="*/skill.md")

# 更新技能（未提供的字段保留原值，created 时间自动保留）
memory(action="update", memory_type="skill",
       skill_name="python-async",
       name="Python 异步编程进阶",
       description="使用 asyncio 进行高级并发编程...",
       content="## 概述\n\nasyncio 高级用法...")

# 删除技能
memory(action="delete", memory_type="skill", skill_name="python-async")
```

**topic/filename 到路径映射示例**（日期假设为 2024-01-15）：
| 类型 | topic/skill_name | filename | 最终路径 |
|------|------------------|----------|----------|
| knowledge | api-design | restful.md | knowledge/api-design/2024-01-15/restful.md |
| knowledge | api-design | graphql.md | knowledge/api-design/2024-01-15/graphql.md |
| knowledge | python | asyncio.md | knowledge/python/2024-01-15/asyncio.md |
| knowledge | python | （不填） | knowledge/python/2024-01-15/{自动}.md |
| knowledge | （不填） | markdown.md | knowledge/misc/2024-01-15/markdown.md |
| knowledge | （不填） | （不填） | knowledge/misc/2024-01-15/{自动}.md |
| skill | python-async | （固定 skill.md） | skills/python-async/skill.md |
| skill | k8s-deploy | （固定 skill.md） | skills/k8s-deploy/skill.md |

**自动生成文件名规则**：
- Knowledge: 根据 title 转换为 kebab-case（如 "Python asyncio 用法" → "python-asyncio.md"）
- 如果同名文件已存在，追加计数器（如 "python-asyncio-2.md"）
- Knowledge 所有主题目录统一按日期组织：`knowledge/{topic}/{YYYY-MM-DD}/{filename}.md`

### 5.4 检索方式

Agent 使用现有工具检索不同类型的记忆：

```yaml
# ===== Knowledge 检索 =====
# 列出所有主题（查看有哪些主题目录）
find(path="data/agents/{uuid}/memory/knowledge/", pattern="*")

# 列出特定主题下的所有日期目录
find(path="data/agents/{uuid}/memory/knowledge/api-design/", pattern="*")

# 列出特定日期下的所有文件
find(path="data/agents/{uuid}/memory/knowledge/api-design/2024-01-15/", pattern="*.md")

# 读取特定主题特定日期的内容文件
read(file_path="data/agents/{uuid}/memory/knowledge/api-design/2024-01-15/restful.md")

# 列出所有日期下特定主题的所有文件
find(path="data/agents/{uuid}/memory/knowledge/misc/", pattern="*/*.md")

# 按来源检索（如只看 web_search 存入的知识）
grep(pattern="source: web_search", path="data/agents/{uuid}/memory/knowledge/")

# 按标签检索
grep(pattern="- restful", path="data/agents/{uuid}/memory/knowledge/")

# 按关键词检索内容
grep(pattern="JWT", path="data/agents/{uuid}/memory/knowledge/")

# ===== Skill 检索 =====

# 列出所有技能（使用 find 工具）
find(path="data/agents/{uuid}/memory/skills/", pattern="*/skill.md")

# 按标签检索技能
grep(pattern="- async", path="data/agents/{uuid}/memory/skills/")

# 按关键词检索技能内容
grep(pattern="asyncio", path="data/agents/{uuid}/memory/skills/")

# 读取技能完整内容（直接使用 read 工具）
read(file_path="data/agents/{uuid}/memory/skills/python-async/skill.md")
```

**检索策略建议**：
- Knowledge: 先 `find` 看有哪些主题目录，再 `find` 看日期子目录（按日期倒序），再 `find` 看文件，再 `read`；或按标签/来源/关键词 `grep`
- Skill: Agent 在收到任务时，先用 `grep` 搜索 skills 目录中的技能名称、描述、标签，找到匹配的技能后使用 `read` 工具加载完整 skill.md 文件
- **grep 自动按 mtime 倒序**：搜索结果按文件最后修改时间倒序排列，最近访问/修改的文件排在最前

**任务前搜索流程**：
当用户提出任务请求时，Agent 应：
1. 先用 `grep` 搜索 skills 目录，查找相关技能
2. 返回匹配的技能名称和文件路径
3. 判断是否需要使用 `read` 工具加载完整技能内容（skill.md）
4. 同时搜索 knowledge 目录，查找相关知识
5. 返回匹配的知识标题和文件路径
6. 判断是否需要使用 `read` 加载完整知识内容

**时间排序规则**：
- 所有检索结果按时间戳倒序排列（最新的在前）
- 冲突时以最新记忆为准
- Agent 在 system prompt 中被明确告知"以最新的记忆为准"

**最后访问时间追踪**：
- 实现：`read` 工具读取 `memory/knowledge/` 下的文件时，自动调用 `os.utime()` 更新文件 mtime 为当前时间
- 追踪目的：支持未来遗忘处理（长时间未访问的知识可自动归档）
- grep 工具按 mtime 倒序排列结果，最近访问/修改的排在前面
- Skill 不受影响（技能不按日期组织，同样按 mtime 排序）

### 5.5 存储规则

**Knowledge 存储规则**：
- 同一主题下按日期组织
- 文件名冲突时追加计数器
- Markdown 格式，包含 frontmatter
- 更新时按 title 查找（跨所有日期目录，最新优先），整体重写文件
- 删除后自动清理空的日期目录和主题目录

**Skill 存储规则**：
- 每个技能一个目录，目录名为 kebab-case，拒绝 UUID 格式的 skill_name
- 技能文件固定命名为 `skill.md`
- Markdown 格式，包含 frontmatter
- 更新时保留已有的 `created` 时间和未提供的字段（name、description、tags），仅更新显式提供的参数
- `updated` 时间戳自动更新为当前时间
- 删除时移除整个技能目录及其内容

---

## 六、系统 Prompt 集成

### 6.1 初始上下文加载

用户属性由独立系统自动加载，不在记忆系统中处理（见 [user-profile-design.md](user-profile-design.md)）。

**注意**：技能摘要不自动注入到系统提示词中，而是由 Agent 在收到任务时主动搜索。这样可以减少上下文大小，避免不必要的 token 消耗。

### 6.2 System Prompt 指导

```markdown
## 记忆系统

你可以使用 `memory` 工具存储长期记忆。

### 何时记忆
- 当用户明确说"记住这个"、"记下来"时
- 当你认为当前对话中有重要信息（如用户偏好、关键知识、技能方法）
- 会话结束前，如果有值得记录的内容

### 记忆类型选择
- **knowledge**: 客观事实、规范、文档
- **skill**: 可复用的技术和工作流

### 任务前搜索（重要）

**收到任务请求时，必须先搜索记忆**：

1. **搜索技能**：使用 `grep` 搜索 `data/agents/{uuid}/memory/skills/` 目录
   - 按技能名称、描述关键词或标签搜索
   - 返回匹配的技能名称和文件路径
   - 判断是否需要使用 `read` 工具加载完整 skill.md 文件

2. **搜索知识**：使用 `grep` 搜索 `data/agents/{uuid}/memory/knowledge/` 目录
   - 按主题、关键词或标签搜索
   - 返回匹配的知识标题和文件路径
   - 判断是否需要使用 `read` 加载完整内容

**示例搜索流程**：
```
# 搜索相关技能
grep(pattern="关键词|keyword", path="data/agents/{uuid}/memory/skills/")

# 搜索相关知识
grep(pattern="关键词|keyword", path="data/agents/{uuid}/memory/knowledge/")

# 如果找到匹配的技能，用 read 工具加载完整内容
read(file_path="data/agents/{uuid}/memory/skills/matched-skill-name/skill.md")

# 如果找到匹配的知识，加载完整内容
read(file_path="data/agents/{uuid}/memory/knowledge/topic/date/file.md")
```

### 检索时机
- 用户提出新任务 → 先搜索 skills 和 knowledge
- 用户提到"之前说过"、"我记得" → 搜索 knowledge
```

---

## 七、待解决问题

### 7.1 记忆冲突

**问题**: 如果新记忆与旧记忆矛盾怎么办？

**示例**:
- 旧记忆："用户偏好中文回复"
- 新记忆："用户希望用英文回复"

**决策**: 方案 A - 追加新记忆，保留旧记忆

**实现**:
- 所有记忆条目必须包含时间戳
- 检索时按时间倒序排列，优先使用最新记忆
- Agent 在系统 prompt 中被告知"以最新的记忆为准"

### 7.2 记忆检索质量

**决策**: 方案 A - 关键词匹配 + 标签过滤（简单）

**实现**:
- 使用 grep 按关键词、标签、来源检索
- 使用 find 列出目录结构
- 复用现有工具，无需额外依赖

### 7.3 Knowledge 主题数量控制

**问题**: 长期积累后，knowledge 目录可能产生大量主题目录

**决策**: 方案 C - 自动合并，无硬限制

**实现**:
- 无硬限制，由 Agent 根据需要管理主题数量
- Agent 定期自动整理：合并相关小主题
- 合并策略：如 `flask/` + `django/` → `web-framework/`

### 7.4 记忆大小控制

**问题**: 长期积累后，记忆文件可能过大

**决策**: 方案 C - 暂不限制，观察使用

**实现**:
- MVP 阶段不对单个文件大小做限制
- 观察实际使用情况，收集数据
- 如后续发现问题，可考虑：
  - 定期归档旧记忆（移到 archive/）
  - 限制单文件最大条目数

### 7.5 记忆更新与删除

**问题**: 如何修改或删除错误的记忆？

**决策**: Phase 1 就实现 update/delete 功能

**实现**:
- memory 工具添加 `update` 和 `delete` action
- 用户说"忘掉这个"、"更新一下"时触发
- 支持通过标题、标签或内容匹配来定位要更新/删除的记忆

### 7.6 跨工作区记忆

**问题**: 记忆是否应该跨工作区共享？

**决策**: 方案 A - 每个工作区独立记忆

**实现**:
- 每个工作区的记忆存储在 `data/agents/{uuid}/memory/`
- 不同工作区的记忆完全独立
- 不支持跨工作区共享记忆

### 7.7 记忆触发时机

**问题**: "会话结束时自动记忆" 的时机如何判断？

**决策**: 方案 A - Agent 自动判断

**实现**:
- Agent 完成所有工具调用后，自动判断是否需要记忆
- 不依赖用户主动触发（如"结束"、"记下来"）
- Agent 根据对话内容智能识别值得记忆的信息

### 7.8 最后访问时间追踪

**问题**: 如何追踪知识的"最后访问时间"，支持遗忘处理？

**决策**: 读取时更新文件 mtime（而非移动文件）

**实现**:
- **更新时机**: `read` 工具读取 `memory/knowledge/` 下的文件时，自动调用 `os.utime()` 更新 mtime
- **更新规则**: 将文件 mtime 设为当前时间
- **排序**: `grep` 工具按 mtime 倒序返回结果，最近访问/修改的文件排在最前
- **目的**:
  - **支持遗忘机制**：通过 mtime 追踪最后访问/修改时间，长时间没访问的知识可被识别和归档
  - grep 检索结果自然按时间倒序，最新记忆在前
- **跨平台**：mtime 在 Windows/Linux/macOS 上一致工作，不依赖 atime（Windows 默认禁用 atime 更新）

**遗忘机制设计**（后续实现）:
- 基于 mtime 判断知识的重要性
- 例如：mtime 超过 30 天的知识可以自动归档
- 遗忘规则可在 system prompt 中配置

---

## 八、实现优先级

### Phase 1: 核心功能（MVP）

- [x] memory 工具基础框架（store/update/delete 操作）
- [x] 两种类型存储（knowledge, skill）
- [x] Knowledge 按主题+日期分目录存储（Markdown 格式）
- [x] Skill 支持 store/update/delete 操作（检索使用 grep/read/find）
- [x] System prompt 指导 Agent 在任务前主动搜索 skills 和 knowledge
- [ ] 读取知识后自动移动文件到最新日期目录（方法已定义，集成到 root_agent.py 待实现）
- [x] 用户属性系统独立（见 [user-profile-design.md](user-profile-design.md)）

### Phase 2: 智能触发（可选）

- [ ] Agent 主动判断记忆时机
- [ ] 会话结束时自动总结并记忆

### Phase 3: 高级功能（可选）

- [ ] 标签过滤
- [ ] 知识归档（旧知识移到 archive/）

---

## 九、风险与权衡

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 记忆误触发 | 用户体验差 | 使用明确的触发词，避免 "ok" 这种模糊词 |
| 记忆膨胀 | 占用磁盘空间 | 暂不限制，观察使用情况，后续可加归档 |
| 记忆冲突 | Agent 行为不一致 | 追加不删除，优先使用最新记忆 |
| 触发词太生硬 | 用户需要学习 | 后续考虑自然语言理解 |
| grep 检索不够智能 | 检索效果依赖关键词 | Agent 可自行组合多个 grep 调用 |

---

## 十、开放问题

1. **是否需要记忆优先级？** 某些记忆是否比其他记忆更重要？
2. **是否需要记忆过期机制？** 某些记忆是否应该随时间衰减？
3. **是否需要记忆共享？** 多个工作区之间是否应该共享某些记忆？
4. **是否需要记忆版本控制？** 是否保留记忆的修改历史？
5. **是否需要导出/导入功能？** 是否支持记忆备份和迁移？

---

## 十一、总结

### 核心设计

- **存储位置**: `data/agents/{uuid}/memory/`，与 sessions/config 统一
- **存储类型**: knowledge、skill 两种类型（用户属性为独立系统）
- **文件格式**: 
  - Knowledge: Markdown with frontmatter（.md 文件）
  - Skill: Markdown with frontmatter（skill.md 文件）
- **文件组织**: 
  - Knowledge 按主题分目录，再按日期分子目录：`knowledge/{topic}/{YYYY-MM-DD}/{file}.md`
  - Skill 每个技能一个目录：`skills/{skill-name}/skill.md`
  - 无法归类的知识存入 `misc/` 目录（同样按日期组织）
- **日期组织优势**: 便于未来做遗忘处理，按日期归档旧知识
- **最后访问时间追踪**: `read` 读取 knowledge 文件时自动更新 mtime，grep 按 mtime 倒序返回结果
- **文件数量控制**: Knowledge 主题数量无硬限制，由 Agent 根据需要管理
- **知识来源**: Knowledge 支持 source 字段（manual/web_search/browser/python）
- **工具结果入库**: web_search/browser/python 的有价值结果由 Agent 判断后存入 knowledge
- **工具职责**: memory 负责存储和管理，检索用 grep/read/find
- **触发机制**: Agent 主动判断 + 用户关键词触发 + 工具结果自动入库
- **上下文加载**: 技能按需检索，用户属性由独立系统自动加载
  - **Skill 摘要不自动注入**，Agent 在收到任务时主动搜索 skills 目录
- **任务前搜索**: Agent 收到任务请求时，先用 grep 搜索 skills 和 knowledge，返回匹配项的文件名和路径，判断是否需要加载完整内容

### 关键决策

1. **存储放在 data 目录** — 与 sessions/config 统一，隔离清晰，避免污染工作区
2. **memory 工具负责存储和管理** — 检索用 grep/read/find，复用现有工具，更灵活
3. **Knowledge 使用 Markdown 格式** — 便于阅读和维护，支持 frontmatter 存储元数据
4. **Skill 替代 Experience** — 技能系统更灵活和可扩展
5. **Knowledge 按主题+日期分目录** — 主题目录 + 日期子目录，便于检索和遗忘处理
6. **Skill 不自动注入摘要** — 避免上下文膨胀，Agent 在任务前主动搜索
7. **任务前搜索流程** — 收到任务时先搜索 skills 和 knowledge，返回匹配项再判断是否加载
8. **misc/ 兜底 + 日期子目录** — 无法归类的知识按日期分层，避免单目录文件过多
9. **文件数量管理** — 无硬限制，Agent 根据需要合并主题或归档
10. **最后访问时间追踪** — `read` 读取 knowledge 文件时自动更新 mtime，grep 按 mtime 倒序排列结果
11. **不做会话检索** — 会话历史不单独检索，有价值的工具结果存入知识库
12. **工具结果自动入库** — web_search/browser/python 的结果可存入 knowledge，标记 source
13. **避免使用 "ok" 作为触发词** — 太常见，容易误触发
14. **Agent 主动记忆** — 不完全依赖用户触发
15. **追加不删除** — 简单且保留历史
16. **记忆按时间排序** — 所有记忆带时间戳，检索时倒序排列，冲突时以最新为准
17. **记忆冲突处理** — 追加新记忆，不删除旧记忆，按时间排序
18. **检索方式** — MVP 使用关键词匹配 + 标签过滤，复用 grep/find

---

*文档版本: v2.3*
*最后更新: 2026-08-28*