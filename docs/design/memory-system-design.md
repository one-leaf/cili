# 记忆系统设计文档

本文档描述记忆系统（Memory）v3 设计。系统提供跨会话长期记忆能力：会话中产生的持久信息经**提取流水线**（后台线程 + Lite 模型）写入摄入日志（journal），再由**整合流水线**（cron 定时 + 手动触发）落为四类条目（fact / preference / skill / reference），并通过三层上下文注入与 `memory` 工具在会话中检索与使用。

v3 与旧版（knowledge / skills 目录 + source_ref + mtime 追踪）在设计上有本质差异：四类条目统一以 `entries/{type}/{name}.md` 平铺存储，`name` 为全局唯一 slug 定位键；引入 journal + 游标的「恰好一次」摄入/整合流水线；条目经 git 独立仓库做版本审计。

---

## 一、功能概述

### 1.1 概述

记忆系统为每个工作区（workspace）维护独立的长期记忆存储。核心职责：

1. **捕获**：会话回合结束后，后台线程用 Lite 模型从新增消息中提取「值得跨会话保留」的候选，写入 journal 摄入日志（结构化或 RAW 原文）。
2. **整合**：定期（cron 每 2 小时）或手动把 journal 中待整合记录落为条目，并刷新全局摘要、推进游标、git 提交。
3. **检索**：`memory(action="find")` 按关键词匹配条目 frontmatter，返回按使用频率排序的候选；`read` 读取全文并递增使用计数。
4. **注入**：每次会话的上下文自动注入常驻偏好、索引与全局摘要三层记忆。

记忆功能默认关闭（`memory_enabled=false`），需用户在记忆管理页显式开启。

### 1.2 记忆类型总览

| 记忆类型 | 说明 | 典型内容 | 检索方式 |
|---------|------|---------|---------|
| **fact** | 项目事实/决策/配置 | API 规范、部署流程、表结构 | find + read |
| **preference** | 用户偏好/工作风格/沟通偏好 | 「中文回复」「周报格式」 | 常驻注入 + find |
| **skill** | 可复用技术/工作流 | 异步编程指南、CORS 修复步骤 | find + read |
| **reference** | 外部来源线索 | 文档、URL、文章链接 | find + read |

四类条目存储格式完全一致（同一 frontmatter 结构），区别仅在 `type` 与语义。

### 1.3 关键设计决策

- **纯文件存储**：条目为 Markdown 文件，无数据库依赖，可读可审计。
- **name 全局唯一**：`name`（slug）是跨类型的唯一定位键，store 按 name 原地替换。
- **journal 摄入 + 游标消费**：提取端与整合端解耦，恰好一次、崩溃可重跑。
- **Lite 模型调用**：提取/整合两阶段调用 Lite 模型，**不挂任何工具**（无 bash/python/web），沙箱更强、成本更低。
- **git 版本审计**：memory 目录自管独立 git 仓库，提交信息取真实 diff 摘要。

---

## 二、存储设计

### 2.1 存储位置与目录布局

每个工作区记忆位于 `{workspace_directory}/.cili/memory/`（由 `get_workspace_data_dir(uuid)` 解析；空 uuid 时为 `workspace/.cili/memory/`）：

```
{workspace_directory}/.cili/
├── approvals.json            # 写/删越界审批记录
├── tmp/                      # 工作区临时目录
├── sessions/                 # 会话存储
└── memory/
    ├── entries/              # 活动条目（四类平铺）
    │   ├── fact/
    │   │   └── rest-api-design.md
    │   ├── preference/
    │   │   └── chinese-replies.md
    │   ├── skill/
    │   │   └── python-async.md
    │   └── reference/
    │       └── faiss-paper.md
    ├── archive/              # 归档条目（不参与检索）
    │   └── {type}/{name}.md
    ├── MEMORY.md             # 索引（MemoryStore 自动维护）
    ├── summary.md            # 全局摘要（整合时刷新）
    ├── journal.jsonl         # 摄入日志（append-only）
    ├── .cursor               # 整合游标（已整合的最大 cursor）
    └── .extract/             # 会话级提取指针
        └── {session_id}.json # last_msg_id / last_extract_ts
```

### 2.2 条目文件格式

条目为「frontmatter + Markdown 正文」文件。frontmatter 字段固定顺序：

| 字段 | 说明 | 约束 |
|------|------|------|
| `type` | 记忆类型 | 四类之一 |
| `name` | 全局唯一 slug 定位键 | 见 2.3 |
| `title` | 人类可读标题 | store 必填（或由 name 派生） |
| `description` | 一句话描述 | ≤200 字符，主要检索信号 |
| `tags` | 分类标签数组 | 内联数组或块状列表 |
| `source` | 来源 | session / user / web / file / python / derived |
| `refs` | 来源引用数组 | 如 `file:...`、`session:...`、`web:...` |
| `created` | 创建时间 | `yyyy-MM-dd HH:mm:ss` |
| `updated` | 最后更新时间 | 同上 |
| `usage_count` | 使用次数 | read 时递增 |
| `last_used` | 最近使用时间 | read 时更新 |
| `status` | 状态 | active / stale / archived |

**文件示例**（entries/skill/python-async.md）：

```markdown
---
type: "skill"
name: "python-async"
title: "Python 异步编程"
description: "使用 asyncio 进行并发编程，包括事件循环、协程、Task 管理"
tags:
  - "python"
  - "async"
source: "session"
refs:
  - "web:https://docs.python.org/3/library/asyncio.html"
created: "2026-09-01 10:30:00"
updated: "2026-09-02 15:00:00"
usage_count: 3
last_used: "2026-09-12 09:00:00"
status: "active"
---

asyncio 是 Python 的异步 I/O 库，使用 async/await 语法…

## 使用场景

- 需要并发处理多个 I/O 操作…
```

**序列化规则**：标量值一律双引号包裹并内联替换引号/换行（frontmatter 逐行解析无转义）；`tags`/`refs` 为数组，空数组不写出；`usage_count` 为整数。解析端兼容标量、内联数组 `[a, b]`、块状列表 `- "a"` 三种写法，无 frontmatter 时返回空。

### 2.3 命名规则

**slugify（标题 → name）**：

- ASCII 标题 → kebab-case（小写、去非字母数字、空格/连字符合并）。
- 非 ASCII（中文）标题 → `memory-{md5(title)[:8]}`。
- 超 96 字符 → 截断前 88 字符并附加哈希。
- 空标题 → `untitled`。

**validate_name（校验 name）**：

- 非空；拒绝 `.`/`..`、含路径分隔符、绝对路径。
- 必须为 ASCII kebab-case；中文等非 ASCII 标题应省略 `name`，由 title 派生。
- 拒绝无意义的 UUID 样式（`8-4-4-4-12` 或 `skill-{8hex}`）。

**冲突处理**：store 时派生 name 与既有**不同标题**条目冲突，自动追加 `-2`/`-3` 后缀，避免误覆盖；`name` 已被**不同类型**条目占用时直接报错（name 全局唯一）。

### 2.4 MEMORY.md 索引

由 `MemoryStore._rebuild_index()` 在每次写操作后自动重建，头部注释标明自动维护、请勿手改：

- 收录所有非 archived 条目，按 `updated` 倒序。
- 每行格式：`- [title](entries/{type}/{name}.md) — description`。
- **双截断上限**：≤200 行 / ≤25KB（常驻注入预算，仿 claude-code 验证过的控制手段）。
- 供提取/整合阶段的 LLM 作为「当前记忆索引」上下文，也作为注入层的第二层。

### 2.5 summary.md

整合成功时由模型生成的全局摘要，≤2KB，超出按整行截断丢弃尾部。语言随记忆内容（中文记忆写中文摘要）。每次整合刷新，供上下文注入的第三层。

### 2.6 journal.jsonl 摄入日志

append-only 日志，记录每次提取/手动写入的候选。记录字段：

`cursor`（单调递增 id）、`key`（去重键）、`ts`、`session_key`、`type_guess`、`title`、`description`、`content`、`tags`、`refs`、`source`、`integrated`（是否已整合）、`raw`（是否 RAW 原文）。

**追加端去重**：同 `key` 的记录只写一次，返回既有 cursor。key 规约：

- 结构化提取：`extract:{session_id}:{key_base}:{i}`（key_base 为首/尾消息 id）
- RAW 降级：`raw:{session_id}:{key_base}`
- 手动写入审计：`store:{name}`（integrated=true，整合时跳过但游标越过）

**防膨胀**：整合成功后 `compact(keep=500)` 删除已整合且超预算的旧记录。

### 2.7 .cursor 游标

消费端去重的载体。游标单调推进（不后退）：

- `read_pending(limit)` 只读「cursor 之后且 integrated!=true」的记录，按 cursor 升序。
- 仅当整批整合**正常完成**（op 覆盖全部待处理记录且无应用失败）才推进游标。
- 崩溃/失败不推游标 → 记录保持 pending，下次可安全重跑；store 幂等（同名原地替换），重放已成功的 op 无副作用。

### 2.8 archive/ 归档与老化

- **手动归档**：`archive(name)` 移入 `archive/{type}/{name}.md`，status 置 archived，从索引与检索中移除；`restore(name)` 移回并置 active。
- **自动老化（cron 每日）**：`archive_stale()` 将「updated > 90 天 且 usage_count=0」的条目自动归档。
- **时效标注**：>30 天未更新的条目为 stale，find/read 输出追加提示「⚠ 此为历史观察（>30 天未更新），使用前请对照当前代码/事实验证」（仿 claude-code memoryAge）。

---

## 三、记忆类型定义

### 3.1 fact（事实）

**用途**：项目事实、决策、配置、规范、文档摘要等客观信息。

**示例**：
- 「我们公司的 API 规范是 RESTful，端口 8080」
- 「数据库表 users 的字段有 id, name, email, created_at」
- 「项目部署流程：先 build，再 docker build，最后 kubectl apply」

### 3.2 preference（偏好）

**用途**：用户偏好、工作风格、沟通偏好。独特地位：**每次请求常驻注入**（见 §7），无需检索即生效。

**示例**：
- 「用户偏好中文回复」
- 「代码注释用中文」
- 「用户希望报告附带测试命令」

### 3.3 skill（技能）

**用途**：可复用的技术方法和工作流。按需检索，完整内容经 read 加载。

**示例**：
- 「Python 异步编程」：asyncio 并发编程指南
- 「K8s 部署」：部署步骤与注意事项
- 「CORS 错误修复」：标准排查方法

### 3.4 reference（参考）

**用途**：外部来源的线索（文档、URL、文章），不必把整篇内容抄入正文，记下出处即可。

**示例**：
- 「FAISS 官方论文：https://...」
- 「xmake 官方文档中 lua 配置一节：web:https://...」

### 3.5 source 来源与 status 状态

- `source` 枚举：`session`（会话提取）、`user`（用户/工具显式写入）、`web`、`file`、`python`、`derived`（整合生成）。
- `status` 枚举：`active`（活跃，参与检索）、`stale`（>30 天未更新，仅提示标注）、`archived`（已归档，仅 list(status=archived) 可见）。

---

## 四、提取流水线（Extraction）

### 4.1 触发时机

Web SSE 回合结束后，若该工作区 `memory_enabled` 且存在 session_manager，则在**后台守护线程**（`memory-extract`）触发 `schedule_extraction`：

```python
if sm is not None and memory_enabled(agent.workspace_uuid or ""):
    schedule_extraction(agent.workspace_uuid or "", agent.current_session_id or "", list(sm.messages))
```

后台线程不阻塞 SSE 流；失败只记日志，不影响正常对话。

### 4.2 恰好一次

两层去重保证每条消息只被提取一次：

1. **会话级指针**（`.extract/{session_id}.json`）：记录 `last_msg_id`，只挑指针之后的新增消息（含无 id 消息），崩溃后重读 messages 仍能接续。
2. **journal key 去重**：append 时同 key 只写一次，重试不产生重复记录。

**输入**：新增 user/assistant 文本消息（单条截断 4000 字符）+ 当前 MEMORY.md 索引。

**结构化输出**（`EXTRACTION_SCHEMA`）：

```json
{ "memories": [ { "type", "title", "description", "content", "tags", "refs" } ] }
```

系统提示词规则：四类互斥、只提取新增持久信息、title 为短语、description ≤200 字、content 完整不截断、refs 记 file:/web:/session: 来源；不保存可从代码/ git 推导的内容，不复制「记住这个 PR 列表」类原文。

### 4.3 RAW 降级

提取阶段任一异常（模型不可用、解析失败等）→ **绝不丢内容**：把新增消息原文经密钥掩蔽后追加为一条 `raw=true` 记录，留待整合阶段处理。

### 4.4 密钥掩蔽

`redact_secrets()` 在入库前掩蔽疑似密钥，三类 pattern：

- 带标签赋值：`api_key=`/`password=`/`secret`/`auth_token` 等后跟 8 位以上值 → 保留标签、掩蔽取值（`api_key=***REDACTED***`）。
- 密钥前缀整段：`sk-`/`sk-ant-`/`ghp_`/`gho_`/`xoxb`/`AKIA`/`AIza` 开头 12 位以上 → 整段掩蔽。
- `bearer` token → 保留前缀、掩蔽 token。

结构化提取的 title/description/content/tags 同样经掩蔽。

### 4.5 memory_enabled 开关

- 存储于 `data/cili/workspaces.json` 索引中对应工作区条目的 `memory_enabled` 字段（`find_workspace_entry()` 读取）。
- **缺省 False**——纯工作区隔离，除非用户在记忆管理页显式开启。
- 控制范围：回合后提取钩子（§4.1）、cron/manual 整合 `consolidate_all` 的工作区过滤（§5.1）。关闭时两者都不执行。

---

## 五、整合流水线（Consolidation）

### 5.1 触发方式

| 触发 | 入口 | 参数 |
|------|------|------|
| cron 每 2 小时 | `core/cron.d/memory_consolidation.json`（系统级任务，interval 120 分钟，启动 30 分钟后首次） | `limit=20, max_batches=1` |
| 手动立即整合 | Web 记忆管理页「立即整合」按钮 → `POST /memory/consolidate` | `limit=20, max_batches=4` |
| 手动工具触发 | `memory(action="consolidate")` → `consolidate_all()` | 全工作区 |

`consolidate_all()` 遍历 `data/cili/workspaces.json` 索引（`load_workspaces_index()`，排除 system），筛出带 `.cili/memory/` 的工作区，过滤掉 `memory_enabled=false` 的，逐个整合并返回每工作区结果。

### 5.2 输入与结构化输出

**输入**：待整合记录（每条标注 `[cursor N]` 及 type_guess/source/raw/session，正文截断 1000 字符）+ 当前 MEMORY.md 索引 + 匹配的既有条目快照（`_entries_context`：从待整合记录取关键词 find 已有条目，最多 10 条，只读 frontmatter 不增 usage，作为冲突消解上下文）。

**结构化输出**（`CONSOLIDATION_SCHEMA`）：

```json
{ "ops": [ { "op", "name", "type", "title", "description", "content", "tags", "refs", "reason" } ],
  "summary": "全局摘要" }
```

`op` 枚举与语义（系统提示词指导）：

| op | 语义 |
|----|------|
| `store` | 全新内容 → 提供完整字段新建 |
| `update` | 与既有条目重复/细化 → 按 name 合并，refs 累积 |
| `archive` | 过时但值得留史 |
| `delete` | 错误、已被取代、可推导 |
| `skip` | 暂留 pending（如缺上下文） |

模型给出的 `type` 不在白名单时回退 `fact`（`_op_type`）。

### 5.3 ops 应用与容错

`apply_ops()` 对模型常见的「目标错位」做确定性容错，避免某条 op 失败 → 整批不推游标 → 重跑产生同样 op 的死锁：

- `delete`/`archive` 目标条目不存在 → 已处于期望状态，**视为完成**（no-op）。
- `update` 目标不存在（模型幻觉 name）→ 有内容则**退化为 store 新建保留**；无内容视为完成。
- `store` 无 title 无 content → 无可保留内容，视为完成（empty op）。
- `store` name 与不同类型条目冲突 → **追加 `-2`/`-3` 后缀重试**，内容不丢。
- 其余意外失败进 `failed`，由调用方决定是否推进游标（失败不消费记录，避免「已整合但未写入」的静默丢失）。

description 一律截断到 200 字上限（journal 记录截断 300，直接透传会误报）。

### 5.4 游标推进与 git 提交

单批流程（成功后顺序执行）：

1. `journal.advance(max_cursor)`——单调推进，覆盖全部已处理记录。
2. `journal.compact(keep=500)`——清理已整合旧记录。
3. `store.archive_stale()`——自动归档 >90 天且零使用的条目。
4. `best_effort_commit(memory_dir, f"consolidate: {n} ops, {m} archived")`——git 提交真实 diff。

**不推游标的情形**：有 op 应用失败，或 op 数 < 待整合记录数（视为截断/模型少输出）→ 记录保持 pending 供重跑，报错注明原因。

### 5.5 拆半重试与批量

- **拆半重试**：整批异常，或 op 数不足（常见于 max_tokens 截断丢尾部）时，`_split_consolidation` 把记录拆两半递归整合再合并（小批次输出更短，截断概率骤降）。递归深度 ≤3；仍不完整则原样返回已解析 ops，由调用方按计数决定是否推游标。
- **多批循环**：`max_batches>1`（手动触发）时循环整合，每批最多 20 条，直至待整合清零、到达批次上限、或某批失败/无进展。返回聚合计数（processed/applied/failed/archived 求和，ops/summary/error 取最后一批）。

### 5.6 摘要刷新

`summary` 仅当该批无失败且 op 覆盖全部记录时写入（`_write_summary`，≤2KB 整行截断）。旧摘要被覆盖，不累积。

### 5.7 Lite 模型与沙箱

- 提取/整合两阶段均调用 Lite 模型（`config.lite_model` 或 `config.model`），max_tokens 分别 8000 / 16000（取模型配置上限的较小者）。
- **无工具**：两阶段 LLM 调用不挂 bash/python/web 等任何工具——比只读 agent 更严的沙箱、更便宜，测试可注入回调替换。

---

## 六、memory 工具设计

### 6.1 工具概览

`memory` 工具是检索与管理的唯一入口，委托 `MemoryStore`/`Journal`，不做 LLM 调用。定位键是 `name`（slug，全局唯一），不再按 title 全目录搜索。

### 6.2 action 与参数

`action` 枚举：`store` / `find` / `read` / `update` / `delete` / `list` / `stat` / `consolidate`。

| 参数 | 说明 |
|------|------|
| `action` | 操作类型（必填） |
| `type` | 记忆类型（store 必填；find/list 可选过滤） |
| `name` | 全局唯一 kebab-case slug（read/update/delete 必填；store 缺省由 title 派生） |
| `query` | find 关键词（必填，匹配 name/title/description/tags/refs） |
| `status` | find/list 状态过滤（active/stale/archived） |
| `title` | 人类可读标题 |
| `description` | 一句话描述 ≤200 字符（主要检索信号） |
| `content` | 正文（Markdown，须完整不截断） |
| `tags` | 标签数组（替代旧 topic） |
| `source` | 来源（默认 user） |
| `refs` | 来源引用数组，如 `['file:E:/docs/x.md', 'session:abc123', 'web:https://...']` |

### 6.3 各 action 行为

**store**：新建或按 name 原地替换；name 缺省由 title 派生，派生冲突自动 `-2`/`-3` 后缀；存在于 archive 且同类型 → 移回 entries（自动解除归档）；name 跨类型冲突报错。成功后写审计 journal（`store:{name}`，integrated=true）并 git 提交。

**find**：描述驱动召回——只匹配 frontmatter（name/title/description/tags/refs），**不读正文**、**不递增 usage**（真实使用以 read 为准）；返回 active/stale（不含 archived），按 `usage_count` 倒序（次按 updated），上限 20；结果附 stale 提示与 read 使用 hint。

**read**：读条目全文，递增 `usage_count` / 更新 `last_used`，输出附 stale 提示。

**update**：原地更新，preserve created/usage_count/last_used；refs 累积去重；status 仅允许 active/stale（归档经 archive/restore）。

**delete**：永久删除并清理空目录，git 提交（历史可恢复）。

**list**：列条目（默认 active+stale，按 updated 倒序；status=archived 时可含归档）。

**stat**：统计——各类条目数、stale/archived 数、索引行数/字节、journal 待整合数与游标位置、最近 5 次 git 提交。

**consolidate**：调用 `consolidate_all()`，报告每工作区处理记录数、store/update 数与归档数。

### 6.4 使用示例

```yaml
# 检索（按关键词匹配 frontmatter，按使用频率排序）
memory(action="find", query="asyncio")
memory(action="find", query="部署", type="skill", status="active")

# 读取全文（递增 usage_count）
memory(action="read", name="python-async")

# 存储（name 缺省由 title 派生）
memory(action="store", type="preference", title="中文回复",
       description="用户偏好使用中文回复", tags=["沟通"], source="user")

# 更新（refs 累积去重，created/usage_count 保留）
memory(action="update", name="python-async", content="…新正文…", tags=["python"])

# 列出与统计
memory(action="list", type="fact")
memory(action="stat")

# 手动触发全工作区整合
memory(action="consolidate")
```

---

## 七、上下文注入

### 7.1 三层注入

每次请求的 `build_environment_context` 中，`_build_memory_sections` 注入三层记忆（v3）：

1. **User Preferences（常驻）**：preference 类型条目，最多 10 条，格式 `- title: description`；>30 天未更新附加「⚠ stale, verify before applying」。无需检索即生效，保证沟通偏好始终在场。
2. **Memory Index**：MEMORY.md 索引全文（≤200 行 / 25KB 双截断），展示所有条目标题与描述，作为「有哪些记忆可查」的目录。
3. **Memory Summary**：summary.md 全局摘要（≤2KB）。

之后附检索提示：`memory(action="find", query="keyword")` 列匹配条目，`memory(action="read", name="<name>")` 读全文。

### 7.2 迁移回退

~~user-profile.md 回退已移除（v3.1 起）。~~ 用户画像完全由 preference 条目承载；preference 为空时不注入任何画像内容，直接跳过。

### 7.3 降级策略

记忆系统初始化异常时，注入层降级为提示语（记忆目录路径 + find/read 用法），**绝不阻塞请求**。

---

## 八、Web 管理界面

### 8.1 功能

顶部工具栏「记忆管理」按钮打开记忆模态框，提供完整管理能力：

- **启用/停用**：记忆开关（`memory_enabled`），控制提取钩子与 cron 整合。
- **立即整合**：手动触发整合（`max_batches=4`，清空待整合队列），显示「整合中（调用 lite 模型，可能需要几十秒）」。
- **待整合提示**：`待整合 N 条` 标签。
- **统计栏**：总条目数、四类计数、待验证数、已归档数、索引行数/KB。
- **过滤**：类型过滤（事实/偏好/技能/参考）、状态过滤（活跃/待验证/已归档）、关键词搜索（客户端匹配 name/title/description/tags）。
- **条目列表**：类型标签 + 标题 + 描述 + 状态标记（待验证/已归档）+ name/tags。
- **详情编辑**：编辑 title/description/tags/content；展示类型/来源/创建/更新/使用次数/最近使用/引用。
- **操作**：保存（PUT）、归档、恢复、永久删除（带确认），操作结果提示 git 提交状态。
- **git 记录**：最近 10 次提交（hash + subject，悬停显示日期）。

### 8.2 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/workspaces/{uuid}/memory` | 记忆总览：enabled + stats + 条目列表（≤500，可按 type/status/关键词过滤）+ 待整合数 + 最近 10 次提交 |
| GET | `/api/workspaces/{uuid}/memory/entries/{name}` | 查看条目全文（peek，只读不递增 usage） |
| PUT | `/api/workspaces/{uuid}/memory/entries/{name}` | 编辑条目（title/description/tags/content）+ git 提交 |
| POST | `/api/workspaces/{uuid}/memory/entries/{name}/archive` | 归档条目（移出索引与检索） |
| POST | `/api/workspaces/{uuid}/memory/entries/{name}/restore` | 从归档恢复 |
| POST | `/api/workspaces/{uuid}/memory/entries/{name}/delete` | 永久删除（不可恢复） |
| POST | `/api/workspaces/{uuid}/memory/consolidate` | 手动整合（max_batches=4） |
| PUT | `/api/workspaces/{uuid}/memory/settings` | 开/关 `memory_enabled`（写入 workspaces.json 条目） |

所有写操作经 `_csrf_protect` 防护；name 经正则校验。

---

## 九、git 版本控制

### 9.1 独立仓库

memory 目录自管**独立的 git 仓库**（best-effort，失败静默跳过）：

- 用 `data/deps/git` 内置 git 优先，其次系统 PATH。
- `_ensure_self_repo` 校验 `rev-parse --show-toplevel` 就是 memory 目录本身；否则重新 init，避免 `add -A` 误提交整个 cili 项目仓库。
- 损坏的 `.git` 目录先备份为 `.git.bak.{ts}` 再重建；本地配置 `user.name=cili`、`user.email=cili@localhost`，避免依赖全局配置。

### 9.2 提交时机与信息

- **写操作即提交**：工具 store/update/delete、整合成功、UI 编辑/归档/恢复/删除后均提交。
- **提交信息 = 真实 diff 摘要**（非 LLM 自述）：`git add -A` 后取 `diff --cached --stat -M` 摘要拼入 commit message，满足审计要求。
- **只读自身仓库**：`git_log_summary` 校验 `--show-toplevel` 后读取最近提交，防止泄漏父仓库全局日志。

---

## 十、并发与安全

### 10.1 线程安全

- **跨线程文件锁**：每个 memory 目录一把 `threading.Lock`（`_STORE_LOCKS`），多 agent 并发写同一 memory 目录时串行化（对应多工作区/多会话并发的兜底）。
- **提取指针锁**：每 (memory_dir, session_id) 一把锁，避免同会话并发提取竞争指针。

### 10.2 原子写

所有文件写入（条目、索引、cursor、journal compact、summary、提取指针）采用「写 `.tmp` + `os.replace`」原子替换，崩溃不产生半写文件；journal 追加本身为 append-only 单行 JSON。

### 10.3 密钥掩蔽

提取入库前 `redact_secrets` 掩蔽 API key / token / password（见 §4.4），防止密钥写入记忆库与 git 历史。

---

## 十一、风险与权衡

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 提取误判（垃圾入库） | 记忆噪声 | 结构化 schema + WHAT_NOT_TO_SAVE 提示（可从代码/git 推导的不存）；整合层可 delete/skip |
| 整合截断（max_tokens 丢尾部） | 部分记录滞留 | op 数 < 记录数即视为不完整：拆半重试 + 不推游标，记录保持 pending 可重跑 |
| 模型幻觉 op（目标错位） | 失败死锁 | apply_ops 容错：缺失目标 no-op / 退化为 store / name 加 -n 后缀 |
| 记忆膨胀 | 注入上下文超预算 | MEMORY.md ≤200 行/25KB 双截断、summary ≤2KB、preference 常驻 ≤10 条、journal compact(500)、90 天自动归档 |
| 密钥入库 | 泄漏风险 | redact_secrets 入库前掩蔽 |
| 并发写冲突 | 条目损坏 | 每 memory 目录线程锁 + 原子写 |
| git 误提交父仓库 | 污染项目历史 | `_ensure_self_repo` 校验独立仓库 |
| 记忆功能误开 | 消耗 Lite 模型 token | 默认关闭，用户显式开启；提取失败 RAW 降级不丢内容 |

---

## 十二、总结

### 核心设计

- **存储位置**：`{workspace_directory}/.cili/memory/`（`get_workspace_data_dir(uuid)` 解析），纯文件、无数据库。
- **存储类型**：fact / preference / skill / reference 四类，统一 `entries/{type}/{name}.md` frontmatter 格式。
- **定位键**：`name`（slug）全局唯一，跨类型不重复；store 按 name 原地替换。
- **摄入日志**：journal.jsonl append-only + `.cursor` 单调游标，两端去重实现「恰好一次」。
- **提取流水线**：回合结束后台线程 + Lite 模型结构化提取，失败 RAW 降级绝不丢内容，密钥入库前掩蔽。
- **整合流水线**：cron 每 2 小时 + 手动立即整合，Lite 模型输出 ops 应用，成功后推游标 + git 提交；容错齐全。
- **上下文注入**：三层——preference 常驻 + MEMORY.md 索引 + summary.md 摘要（user-profile.md 回退已移除）。
- **检索**：find 匹配 frontmatter 按 usage_count 排序，read 读全文并递增使用计数。
- **管理 UI**：Web 记忆管理页完整覆盖开关/整合/编辑/归档/恢复/删除/git 记录。
- **版本控制**：memory 目录自管独立 git 仓库，提交信息为真实 diff 摘要。

### 关键决策

1. **四类条目统一平铺** — 替代旧版 knowledge/skills 分目录设计，type 由 frontmatter 表达，存储/检索/管理逻辑统一。
2. **name 全局唯一 slug** — 确定性定位，杜绝「按 title 全目录搜索」的歧义。
3. **journal + 游标摄入模型** — 提取与整合解耦，恰好一次、崩溃可重跑、store 幂等重放无副作用。
4. **后台线程提取** — 不阻塞 SSE 流；整合 cron 定时不占用对话路径。
5. **Lite 模型 + 无工具沙箱** — 提取/整合成本可控、更安全、测试可注入。
6. **preference 常驻注入** — 沟通偏好始终在场，无需检索。
7. **MEMORY.md 双截断** — 常驻注入预算受控（200 行/25KB）。
8. **op 容错与拆半重试** — 整批失败不消费记录，绝不丢内容。
9. **git 独立仓库审计** — 所有写操作留痕，可回溯、可恢复。
10. **默认关闭** — 记忆功能由用户显式开启，纯工作区隔离。

---

*文档版本: v3.1*
*v3 更新: 2026-09-13 — 重写为 v3 实现：四类条目统一 entries/{type}/{name}.md 布局（替换旧 knowledge/{topic}/{date} 与 skills/{name}/skill.md）、journal + 游标提取/整合流水线、Lite 模型两阶段、git 独立仓库审计、三层上下文注入、Web 记忆管理 UI。*
*v3.1 更新: 2026-09-21 — 记忆目录迁移至 `{workspace}/.cili/memory/`（workspaces.json 索引解析）；memory_enabled 存入 workspaces.json；user-profile.md 回退移除。*
