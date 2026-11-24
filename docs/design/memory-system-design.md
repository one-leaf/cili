# 记忆系统设计

> 按当前实现重写（2026-11-25）。代码：`core/memory_store.py`、`core/memory_journal.py`、
> `core/memory_pipeline.py`、`core/memory_service.py`、`core/tools/memory.py`。

## 一、定位

工作区隔离的长期记忆（v3）：条目文件 + 索引 + 摄入日志 + 提取/整合流水线。
记忆功能按工作区开关（`workspaces.json` 的 `memory_enabled`），默认关闭。

## 二、存储布局

```
{workspace_dir}/.cili/memory/
  MEMORY.md                 索引（常驻注入；按行数 200 / 25KB 双截断）
  entries/{type}/{name}.md  条目（YAML frontmatter + 正文）
  archive/{type}/{name}.md  归档条目
  journal.jsonl             摄入日志（append-only）
  .cursor                   已整合游标（恰好一次消费）
```

- `type`：`fact / preference / skill / reference`；`source`：`session / user / web / file / python / derived`。
- 状态：`active / stale(30 天) / archived(90 天)`；名称经 `validate_name` 校验。
- 写入统一原子写，按目录加锁；frontmatter 字段顺序固定（`_FM_ORDER`）。
- 旧版本遗留的 `memory/.git` 由 `cleanup_all_memory_git` 尽力清理（升级时）。

## 三、模块

| 模块 | 职责 |
|---|---|
| `memory_store.MemoryStore` | 条目 CRUD、MEMORY.md 索引维护、老化/归档、检索 `find` |
| `memory_journal.Journal` | journal.jsonl 追加（按 key 去重）+ 游标推进（崩溃可重跑） |
| `memory_pipeline` | 提取 `run_extraction`、整合 `run_consolidation`、`apply_ops`、密钥脱敏 |
| `memory_service` | 回合后提取调度 `schedule_extraction`、开关 `memory_enabled`、全工作区整合 `consolidate_all` |
| `tools/memory` | 模型侧工具（store/find/read/update/delete/list/stat/consolidate） |

## 四、写路径

1. 回合结束：`core.turn_hooks.schedule_after_turn_hooks` → `memory_service.schedule_extraction`
   （仅 `memory_enabled` 的工作区，后台线程不阻塞）。
2. 提取：把新消息（按会话指针去重）交给 lite 模型，产出结构化条目 → 写 journal。
3. 整合：`run_consolidation` 消费 journal（游标之后），合并/更新/归档条目，
   成功后推进游标；失败不推进（幂等可重跑）。
4. 手动：memory 工具的 `consolidate` 动作触发单工作区整合；全局由 cron 种子
   `memory-consolidation` 定期执行。

## 五、读路径

- system prompt 注入 `MEMORY.md`（索引）+ 偏好条目前 N 条（见 `prompt_sections._build_memory_sections`）。
- 工具检索：`memory(action="find", query=...)` 关键词匹配条目（`MemoryStore.find`）。
- 会话搜索工具 `session_search` 不读记忆，属会话历史。

## 六、密钥与安全

- `redact_secrets` 在提取输入侧脱敏（token/api key/密码模式）。
- 青少年模式策略可禁止记忆删除/归档（`deny_memory_delete`）。
- 记忆文件位于工作区 `.cili/` 内，受受保护路径与工作区边界约束。

## 七、配置

- 开关：工作区 `memory_enabled`（记忆管理页或 `PUT …/memory/settings`）。
- 模型：提取/整合使用角色 `lite` 模型（`config.model_for_role("lite")`）。
- 索引注入上限：`INDEX_MAX_LINES` / `INDEX_MAX_BYTES`。
