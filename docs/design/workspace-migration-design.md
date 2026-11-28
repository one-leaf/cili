# 工作区数据迁移设计

> 按当前实现整理（2026-11-28）。代码：`core/workspace_relocation.py`、
> `core/workspace_migration.py`、`core/paths.py`、`web/routes_workspace.py`、
> `web/static`（工作区设置 → 数据迁移）。

迁移分两类：

1. **单工作区迁移**（用户触发）：把一个工作区目录搬到其他盘/目录，入口「工作区设置 → 数据迁移」；
2. **启动期布局迁移**（自动、幂等）：旧 `workspace/` 布局 → `workspaces/`，以及旧默认工作区
   `workspace/` 根 → `workspace/default/`。

持久化路径统一走 `core/paths.py`：项目内路径按相对项目根存储（`to_storage_path`），
读取时还原为绝对路径（`resolve_storage_path`），整个 cili 目录搬家后索引仍有效。

## 一、单工作区迁移

### 1. 预检（只读）

`preflight_workspace_migration(uuid, target)` 返回计划与 blockers：

- 工作区存在、不是 System 工作区、当前没有迁移在进行；
- 目标路径：不能等于源、不能在源内部、不能包含 cili 项目目录、不能是项目根、
  不能落在 `DATA_DIR`/`SYSTEM_DATA_DIR`、不能与任何其他工作区目录互相嵌套、路径 ≤240；
- 目标不能已存在；
- 目标父目录必须存在且可写；跨卷时计算 `文件数/字节数` 与可用空间（留 10% 余量，
  余量不足 20% 给 warning）；
- 工作区必须空闲：运行中的会话、排队回合、后台任务、运行中的定时任务任一存在即 blocker；
- `same_volume` 决定方法：`rename`（同卷）或 `copy`（跨卷）。

### 2. 启动与状态机

`start_workspace_migration`：

1. 进程级迁移锁（同时只允许一个迁移）；
2. 预检 + 备份 `setting.json` / `workspaces.json` 到
   `data/cili/backup/migrations/{时间戳}-{reason}-{随机后缀}/`（失败即中止）；
3. `_mark_migrating(uuid)` 冻结工作区；标记后再次确认无活动；
4. 从 `SessionRegistry` 移除该工作区 runner；
5. 写 journal（`data/cili/workspace-migration.json` 的 `active`），后台线程执行。

阶段（journal.phase）：

```
preparing → copying → verifying → switched → cleaning → done
                └──── cancelled（仅复制/校验阶段可取消）
同卷：preparing → switching → switched → done
```

- **跨卷**：复制到 `{dst}.partial-{migration_id}` → 校验文件数/字节数一致 → `os.replace(partial, dst)`
  → 更新 `workspaces.json` 的 `directory` → `rmtree(src)`；
- **同卷**：`os.replace(src, dst)` → 更新索引；
- 索引指向目标且校验通过前**绝不删除源目录**；索引写失败时同卷回滚 `dst→src`，
  跨卷保留目标并标记 `待启动修复`；
- 清理源失败只记 `error` 并保留 active，下次启动继续；
- 取消只在 `preparing/copying/verifying` 有效：删除 partial、状态 `cancelled`。

### 3. 冻结

`is_workspace_migrating(uuid)` 在以下位置拦截：

- `SessionRegistry`：拒绝新 runner / 回合；
- `web/deps.py`：工作区依赖与回合准入返回 409；
- `web/routes_files.py`：文件访问拒绝；
- `core/cron/executor.py`：该工作区的定时任务跳过。

其他工作区不受影响。

### 4. 启动恢复

`recover_pending_migration()` 只做一致性收尾，不重新复制：

| 中断阶段 | 处理 |
|---|---|
| `preparing/copying/verifying`（含 verifying 但目标已切换） | 删 partial，标 `failed`，源未动 |
| `switching/switched/cleaning` | 若目标存在：修正索引指向目标、删除源、标 `done` |
| 其他不一致 | 标 `failed`，保留现场 |

### 5. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/workspaces/{uuid}/migration/preflight` | `{target_path}` → 计划/blockers |
| POST | `/api/workspaces/{uuid}/migration` | 启动迁移，立即返回 `{id,status,plan}` |
| GET | `/api/workspaces/{uuid}/migration` | 当前/最近一次状态（含进度/error） |
| DELETE | `/api/workspaces/{uuid}/migration` | 请求取消（仅复制/校验阶段） |

## 二、启动期布局迁移

### 1. 旧默认工作区归一化（`core/workspace_migration.py`）

每次启动 `ensure_default_workspace()` 调 `migrate_default_workspace()`，幂等：

- 索引里没有 `directory == <项目根>/workspace` 的条目 → 直接返回；
- 若已有 `workspace/default` 条目：有真实数据则放弃（两套会话/记忆无法合并），
  只有自动创建的空骨架则删除该条目、由旧条目接管；
- 把 `workspace/` 根下除 `default/` 与已注册独立工作区外的全部内容（含隐藏项）
  移动到 `workspace/default/`；全部成功后更新索引，任一项失败尽量搬回并保持旧索引；
- 兜底：索引仍指旧根但旧根只剩 `default/` 且 `default/.cili` 存在 → 直接修索引。

### 2. `workspace/` → `workspaces/` 批量编排（`migrate_layout_to_workspaces`）

启动期执行（只跑一次，成功后写 `workspaces.layout_migrated_at`）：

1. 旧默认工作区归一化（见上）；
2. 迁移 `workspaces.root` 到 `workspaces/`（自定义 root 不迁移）；
3. 已登记工作区：旧根直接子目录 → `workspaces/<name>`；目标已存在时，
   空骨架删除、非空则跳过并保留原位；
4. 非工作区目录/文件：逐个搬运；与已注册工作区重叠的跳过；
5. 删除空旧根，写 `layout_migrated_at`。

同卷 `rename`，跨卷 `copy → 校验 → replace → 删源`；每项独立失败不影响其他项；
迁移前备份配置，备份失败则整批跳过。

## 三、配置备份

`backup_migration_config(reason)`：把 `setting.json`、`workspaces.json` 复制到
`data/cili/backup/migrations/{时间戳}-{reason}-{随机后缀}/`，失败抛 `MigrationError`
并中止迁移（单工作区迁移与批量布局迁移共用）。

## 四、测试

`test/test_workspace_relocation.py`、`test/test_workspace_migration.py`、
`test/test_workspace_paths.py`、`test/test_web_workspace_api.py` 中的工作区迁移端点用例。

## 五、已知边界

- 迁移期间工作区冻结，但**不阻止外部进程**直接读写目录；Windows 上被占用时会记录
  占用进程（`core/win_locks.py`）并保留现场；
- 跨卷复制需要额外空间（约等于工作区大小）；
- 取消只保证在复制/校验阶段生效，切换与清理阶段不可中断；
- 批量布局迁移只在 `workspaces.root` 仍为旧/新默认根时执行，自定义 root 不动。
