# `.cili` 工作区数据布局设计

> 按当前实现整理（2026-11-28）。代码：`core/workspace_layout.py`、`core/layout_migration.py`、
> `core/memory_paths.py`、`core/workspace_relocation.py`、`core/org/workspaces.py`。

## 一、目标

把工作区 `.cili/` 按「共享 vs 本机运行态 vs 可丢弃缓存 vs 临时文件」分层，降低共享工作区的
Git 冲突，避免本机运行数据被同步；新建工作区直接写布局标记，不再走迁移。

| 层 | 目录 | 是否共享（Git remote-wins） | 内容 |
|---|---|---|---|
| 共享配置 | `.cili/config/` | 是 | `approval_rules/*.json`（逐规则）、`sensitive_words/{settings.json,rules/}`、`layout.json` |
| 共享内容 | `.cili/sessions/`、`.cili/memory/entries|archive/`、`.cili/workflows/{id}/workflow.json`、`.cili/cron/{id}/job.json`、`.cili/mcp/{server}.json` | 是 | 会话消息/索引/meta、记忆条目、工作流定义、任务定义、MCP 配置 |
| 本机运行态 | `.cili/runtime/` | 否 | `.layout-migrated`、`memory/sessions/`（journal/pointer/cursor）、`todo/`、`workflows/{id}/state.json+runs/`、`cron/{id}/state.json` |
| 可丢弃缓存 | `.cili/cache/` | 否 | `memory/{MEMORY.md,summary.md}`、`tool_output/`、`pdf/` 等 |
| 临时 | `.cili/tmp/` | 否（gitignore） | 会话临时目录、上传中间件、下载中间文件 |

用户可见产物不在 `.cili/` 内：工作流结果在 `{workspace}/workflows/`，课件成绩/错题在
`{workspace}/courseware/quiz-results/`，下载在 `{workspace}/downloads/`。

路径统一由 `core/workspace_layout.py` 提供：`workspace_runtime_dir / workspace_cache_dir /
workspace_config_dir / workspace_tmp_dir / memory_session_state_dir / todo_dir / tool_output_dir`，
调用方不手写目录名。

## 二、布局标记

- 本机标记：`.cili/runtime/.layout-migrated`：`{version, migrated_at}`；`LAYOUT_VERSION=4`；
- 共享标记：`.cili/config/layout.json`：`{version, migrated_at, migrated_by_node}`，
  让共享工作区的其他节点知道布局已迁移；
- 新建工作区（工作区向导、组织共享工作区）直接 `write_layout_marker()` 写标记，跳过迁移。

## 三、迁移（`core/layout_migration.py`）

启动时 `ensure_layout_migrated()` 枚举 System 工作区 + 所有已登记工作区 + `workspaces/*/.cili`，
逐个 `migrate_data_dir()`：

1. 先备份到 `data/cili/backup/layout-migration-{时间戳}-{key}-{随机}/`
   （复制 `.cili`，排除 `tmp/`、`cache/`——可丢弃数据，且可能含权限受限的 junction）；
2. 按子系统搬运（见下）；3. 写本机/共享标记。已迁移（version ≥ 4）直接跳过；
   `CILI_SKIP_LAYOUT_MIGRATION=1` 可整体跳过（测试/排障用），迁移失败抛错并阻断启动。

| 旧位置 | 新位置 |
|---|---|
| `workflows/{id}/state.json`、`runs/` | `runtime/workflows/{id}/state.json`、`runs/` |
| `cron/{id}/state.json`、`.migrated.json` | `runtime/cron/{id}/state.json`、`runtime/cron/.migrated.json` |
| `tools/todo/*.json` | `runtime/todo/`；`tools/loop/` 删除 |
| `memory/MEMORY.md`、`summary.md` | `cache/memory/` |
| `memory/journal.jsonl` | 按 `session_key` 拆到 `runtime/memory/sessions/{key}/journal.jsonl`，原文件改名 `.migrated` |
| `memory/.extract/*.json` | `runtime/memory/sessions/{stem}/pointer.json` |
| `memory/.cursor` | `runtime/memory/sessions/_orphan/.cursor`；`memory/.git` 删除 |
| `approval_rules.json` | `config/approval_rules/{rule_id}.json` + `_constraints.json`，原文件改名 `.migrated` |
| `sensitive_words.json` | `config/sensitive_words/settings.json` + `rules/{sha1(word)}.json` |
| `courseware/quiz-results.jsonl`、`学习资料/成绩记录.html`、`错题本.md` | `{workspace}/courseware/quiz-results/legacy.jsonl`、`*-legacy.*` |
| 会话目录内的工具输出文件/attachments/exec 输出 | `cache/tool_output/{session_id}/`、`cache/tool_output/{exec_id}/` |
| `tmp/downloads/` | `{workspace}/downloads/` |

会话目录中的保留文件（`messages.jsonl`、`index.json`、`meta.json` 等）留在 `sessions/`；
`index.json.legacy`、`goal.json` 直接删除。

搬迁采用「目标存在则合并/删除源，否则 `shutil.move`」；同卷 rename，跨卷由 `shutil.move` 处理。

## 四、兼容读取

`core/memory_paths.resolve_existing()`：`MEMORY.md` / `summary.md` 优先读新路径
`cache/memory/`，不存在再读旧 `memory/`；写入方按新布局。旧文件保留，直到下次迁移或手动清理。

## 五、与共享工作区/组织托管的关系

- 组织共享工作区创建时直接建出 `sessions/`、`config/approval_rules/`、`config/sensitive_words/rules/`
  并写布局标记；
- 共享工作区用 Git `remote_wins` 同步：共享层（config/sessions/memory entries/workflows 定义/
  cron 定义/mcp）随 Git 走，`runtime/`、`cache/`、`tmp/` 不入同步；
- 工作区迁移（换盘）按目录整体搬运，布局不变。

## 六、测试

`test/test_layout_migration.py`、`test/test_workspace_layout.py`、`test/test_memory_paths.py`、
`test/test_cache_state.py`、`test/test_workspace_paths.py`。

## 七、已知边界

- 旧文件（`*.migrated`、旧 `memory/MEMORY.md`）不会自动删除，保留用于回滚/核对；
- 备份不含 `tmp/`、`cache/`，迁移失败时这两层不保证可恢复；
- 布局版本是硬编码常量，新增目录需同时更新迁移与 `workspace_layout.py` 入口；
- 迁移是启动期同步操作，工作区很大时启动会变慢（先备份再搬运）。
