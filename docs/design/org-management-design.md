# 组织托管设计（企业组织）

> 按当前实现整理（2026-11-28）。客户端代码：`core/org/`、`core/workspace_git.py`、`web/routes_org.py`、
> `web/static/org-settings.js`、`core/app_context.py`。组织服务端是独立项目（`cili-server`），不在本仓库。
> 本文只描述 cili 客户端侧行为。

## 一、定位与边界

员工在 cili 里用「手机号 + 短信验证码」加入组织，客户端自动获得：

1. 组织统一下发的模型接入配置（provider + models + key，物化进 `setting.json`）；
2. 群组共享工作区（按组织下发自动创建/更新，Git 以远程为准）；
3. 管理员在服务端吊销设备后，客户端下一次同步即标记失效并恢复本地配置。

边界：

- **只加 provider，不改角色**：`apply_config(..., bind_roles=[])`，组织模型只进入 `providers`，
  `agents.master/worker/lite/workflow` 的选择权仍归用户（角色绑定功能已取消）。
- **令牌只在本机**：`data/org/session.json` 存设备令牌，`state.json` 与 `workspaces.json` 都不含令牌；
  Git 令牌只写 `.git/cili-credentials`。
- **服务端契约由 cili-server 实现**：客户端只按 `/api/v1` 调用，不假设服务端内部结构。

## 二、本地状态

目录：`{DATA_DIR}/org/`（创建时尝试 `0700`，文件 `0600`，Windows 上为 best-effort）。

| 文件 | 内容 | 是否含令牌 |
|---|---|---|
| `session.json` | `server_url`、`org_id`、`phone`、`device_token`、`expires_at`、`org`、`user`、`config_version` | 是 |
| `state.json` | `provider_key`、`roles_applied`、`roles_before`、`model_label/protocol/keys`、`config_version`、`org/user` 快照、`last_sync`、`last_error`、`session_invalid`、`workspaces`、`workspaces_synced` | 否 |

损坏文件的恢复走 `load_json_or_backup`：保留 `.corrupt-*` 备份并回退空对象。

共享工作区在 `workspaces.json` 里以 `org_managed=true` 标识，并记录 `org_id`、`org_workspace_key`、
`org_group_id/org_group_name`、`git_remote_url/branch/username`、`git_sync_mode`（默认 `remote_wins`）。

## 三、服务端契约（`/api/v1`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/public/orgs` | 按服务器地址发现可加入的组织 |
| POST | `/auth/sms/send` | `{org_id, phone}` → `cooldown_seconds` |
| POST | `/auth/sms/verify` | `{org_id, phone, code, device_name}` → `token`、`org`、`user`、`expires_at`、`config_version` |
| GET | `/auth/me` | Bearer；返回最新 `user/org/expires_at` |
| GET | `/config` | Bearer；`If-None-Match: <etag>` 可返回 304；200 返回 `model/workspaces/version/org/user` |
| POST | `/auth/logout` | Bearer；吊销设备令牌 |

客户端错误分类（`core/org/client.py`）：

- `401/403` → `OrgAuthError(status, code, message)`；`401` 或 `403 + token_invalid/person_disabled` 视为登录失效；
- `429` → `OrgRateLimited(retry_after)`；
- 连接失败 → `OrgNetworkError`；其他 4xx/5xx → `OrgProtocolError`。

## 四、生命周期

```
startup ──10s──> sync ──每 6h──> sync
login  ──> verify ──> [换组织: 吊销旧令牌 + 断开旧工作区 + 恢复旧配置] ──> save session ──> sync(force)
logout ──> 服务端吊销 ──> 断开共享工作区 ──> restore_config ──> 清空本地
失效   ──> 标记 session_invalid ──> 清 Git 凭证 ──> restore_config ──> 保留文件与索引
```

- **启动**：`AppContext.startup()` 调 `OrgService.startup()`，后台线程 `OrgSync` 等 10s 后首次同步，
  之后每 `6 * 3600` 秒一次；`shutdown()` 置停止事件并 join。
- **登录**：`verify` 成功后写 `session.json`；若切换了服务器或组织，先吊销旧设备、断开旧组织工作区、
  恢复旧 provider，再写新会话；随后强制同步一次。
- **同步**：`me` 刷新用户信息 → 拉配置。当前实现每次都全量拉取（`etag` 固定为空），
  `If-None-Match/304` 分支保留但未启用；304 分支若将来启用，只做共享工作区 Git 同步。
- **退出**：先调服务端吊销（失败只记日志，不阻断），再断开共享工作区、恢复 `setting.json`，最后清空 `data/org/`。
- **失效**：`401` 或 `403 + token_invalid/person_disabled` 时标记 `session_invalid`，断开共享工作区、
  恢复配置并清掉 `provider_key/roles_*` 引用；工作区文件与索引保留，重新登录按首次绑定处理。
- **网络/协议/限流错误**：只写 `last_error`，不清登录态、不中断后台循环。

## 五、配置物化（`core/org/apply.py`）

- **provider key**：优先用服务端下发的 `model.provider_key`（`/` 替换为 `-`），否则 `org-<org_id>`；
  统一过 `safe_provider_key`（仅 `[A-Za-z0-9_-]`，最长 64）。
- **provider 体**：`model` 去掉 `{mode, provider_key, label, roles}` 后作为 provider；
  `protocol` 缺省 `openai-compatible`；`models` 必须非空。
- **写入**：先把旧版本配置迁移到当前格式，再写 `providers[provider_key]`；旧 key 不同则删除旧 provider；
  `save_global_config` 成功后 `_reload_cached_runners()`：持 `CONFIG_RELOAD_LOCK` 遍历 `registry` 调
  `runner.reload_config()`，让已缓存 runner 立即切换。
- **不覆盖用户手动选择**：`roles_applied/roles_before` 记录上次托管痕迹；若某角色当前模型已被用户改成
  非组织引用，则跳过并写入 `roles_skipped`。`state.json` 丢失时从 `setting.json` 里 `org-*/model`
  形式的引用反推，避免误覆盖。
- **恢复**（`restore_config`）：删除组织 provider；把仍指向该 provider 的 `agents.*.model` 恢复为
  进入组织前的快照（空则删除字段）；再次重载缓存 runner。恢复失败保留状态并提示，不强行清空。

## 六、共享工作区（`core/org/workspaces.py` + `core/workspace_git.py`）

服务端下发字段：`key`、`slug`、`name`、`group_id/group_name`、`memory_enabled`、
`git{remote_url, branch, username, password, sync_mode}`。

- **目录**：`{workspaces.root}/{slug}`（slug 清洗；缺省用 key）；写入工作区索引时 `category=org`、
  `git_enabled=true`、`git_sync_mode` 默认 `remote_wins`。
- **Git**：`remote_url` 拒绝带用户名/密码；`branch` 必须匹配 `[A-Za-z0-9._/-]+` 且不以 `-` 开头、
  不含 `..`，否则回退 `main`；`password` 缺省用设备令牌；令牌只写入 `.git/cili-credentials`，
  `workspaces.json` 中的 `git_token` 置空。
- **创建/更新**：建目录与 `.cili/{sessions, config/approval_rules, config/sensitive_words/rules}`、
  写布局标记、`init_workspace_git`、`ensure_git_exclude`、`set_git_remote`、`sync_remote_wins`。
  单工作区失败只把该条目标成 `error/warning`，不影响其他工作区。
- **304/周期同步**：`sync_managed_workspaces` 强制恢复 Git 配置并以 `remote_wins` 同步。
- **服务端移除**：`prune_managed_workspaces` 清 Git 凭证、`org_managed=false`、
  `org_active=false`，**保留目录与索引**。
- **退出/失效**：`disconnect_workspaces` 同上，只断开关联，不删文件。

## 七、HTTP 接口与前端

`web/routes_org.py`（POST 均带 `csrf_protect`）：

| 方法 | 路径 | 返回 |
|---|---|---|
| GET | `/api/org/status` | `OrgService.status()` |
| POST | `/api/org/discover` | `{orgs: [...]}` |
| POST | `/api/org/sms/send` | `{ok, cooldown_seconds}` |
| POST | `/api/org/login` | 登录后的 status |
| POST | `/api/org/sync` | `sync(force=True, wait=True)` 的 status |
| POST | `/api/org/logout` | 退出后的 status |

错误映射：`OrgAuthError→401`、`OrgRateLimited→429 {retry_after}`、`OrgNetworkError→502`、
`OrgProtocolError→400`、其他 `OrgError→500`。

`web/static/org-settings.js` 只通过 `/api/org/*` 读写，页面永远不接触令牌；状态字段包括
`joined/session_invalid/org/user(phone_masked)/provider_key/roles/model/config_version/last_sync/last_error/workspaces`。

## 八、安全要点

- 服务器地址只允许 `http(s)://host[:port]`；非本机必须 HTTPS；不允许 URL 内嵌用户名/密码或路径/参数。
- 令牌只落 `data/org/session.json` 与 `.git/cili-credentials`；API 与日志不返回令牌。
- 目录/文件权限在 POSIX 上 `0700/0600`，Windows 上为尽力而为。
- Git 远程 URL 与分支做输入校验，避免注入参数或路径穿越。

## 九、测试

`test/test_org_core.py`、`test/test_org_routes.py`、`test/test_org_workspaces.py`；
组织同步相关的工作区 Git 行为在 `test/test_workspace_git.py` 覆盖。

## 十、已知边界

- ETag/304 分支当前未启用（`sync` 固定全量拉取），保留是为了后续降低轮询开销。
- 角色绑定已取消；`roles_applied/roles_before` 仅用于「退出/失效恢复」与旧状态兼容。
- 组织服务端（账号、短信、配置下发、管理端、Git 网关）属于独立项目，不在 cili 仓库内。
