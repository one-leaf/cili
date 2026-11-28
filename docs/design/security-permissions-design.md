# 权限、规则与审批设计

> 按当前实现整理（2026-11-28）。代码：`core/permission_settings.py`、`core/security/`、
> `core/approval_cards.py`、`core/approval_watchdog.py`、`core/tools/permission.py`、
> `web/routes_permissions.py`、`web/static/permission-settings.js`。

## 一、定位

工具执行前统一做「路径 / 命令 / URL」三类判定，结论只有四种：

| 结论 | 行为 |
|---|---|
| `allow` | 直接执行 |
| `deny` | 直接拒绝 |
| `ask` | 弹出审批卡，等用户决定 |
| `ai` | 交给 lite 判定器，判定失败/低置信再转 `ask` |

配置在 `setting.json` 顶层 `permissions`（不在 `system` 块）；规则在全局
`data/cili/approval_rules.json` 与工作区 `{workspace}/.cili/approval_rules.json`。

## 二、配置（`PermissionConfig`）

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 关闭后所有判定直接放行 |
| `default_mode` | `allow` | 命令未命中任何规则时的默认结论：allow / ai / ask |
| `readonly_bypass` | true | 内置只读命令白名单直接放行 |
| `outside.read` | `allow` | 工作区外读取 |
| `outside.write` | `ask` | 工作区外写入 |
| `outside.delete` | `ask` | 工作区外删除 |
| `outside.navigate` | `ai` | 非公网导航（URL） |
| `rules_hot_reload` | true | 规则文件变更自动重载 |
| `global_rules_path` | 空 | 空 = `data/cili/approval_rules.json` |
| `judge` | 见下 | AI 判定器 |
| `approval` | 见下 | 审批卡生命周期 |

`judge`：`enabled=true`、`timeout_seconds=60`、`on_error=ask|deny`、`persist_allow=true`、
`cache=session|none`、`max_consecutive_failures=3`、`intent_context=true`、`model=""`（空 = lite 角色）。

`approval`：`timeout_seconds=300`（0 = 不自动过期）、`auto_resume=web|all|none`。

## 三、判定顺序（`PermissionResolver`）

1. 权限系统关闭 → `allow`；
2. 内置 deny（bash/pwsh 命令文本）；
3. 规则引擎 `deny` → 直接拒绝；
4. 规则引擎 `ask` → 审批；
5. 规则引擎 `allow` → 记为候选放行；
6. 所有写/删目标的路径判定（可能把结论提升为 ask/ai/deny）；
7. 会话已批准（`decision_id` 命中）→ `allow`；
8. 候选放行 → `allow`；
9. 内置只读白名单（无静态风险、无写目标、只读命令且外部路径允许）；
10. 安全区内创建（touch/mkdir 等，目标全在安全区）；
11. 分项策略：命令用 `default_mode`，路径用 `outside.read/write/delete`，URL 用 `outside.navigate`；
    静态证据（动态目标/可疑代码）会把 allow 提升为 `ai`；
12. `ai` → lite 判定器；`ask` → 审批卡；`deny` → 拒绝。

优先级：**deny > ask > allow**；工作区规则优先于全局规则。

## 四、规则引擎（`core/security/permission_rules.py`）

- 每条规则：`{id, enabled, decision(allow|deny|ask), tool, kind(command|path|url), match, note, source}`；
- 全局与工作区两层文件，工作区层优先；支持热重载与按文件锁写入；
- 命令匹配：`exact / prefix / argv_glob`；按 `bash`/`pwsh` 词法切段，段级匹配；
- 路径匹配：前缀覆盖（工作区相对路径可展开）；URL：前缀匹配；
- 会话批准 `remember` 会生成工作区级 allow 规则（`build_granted_rule`），
  同时为命令涉及的越界目标生成隐式路径规则；
- 内置规则（只读展示）：禁止 `rm -rf /`、`rm -rf ~`、`mkfs`、`format`、`diskpart`、
  `shutdown`、`reboot`、`Clear-Disk`、`Initialize-Disk`、`Stop-Computer`、`Restart-Computer`、
  `dd of=/dev/*` 等。

**内置只读白名单**：`ls/cat/head/tail/grep/rg/diff/echo/...` 等；只读 `git` 子命令、
`pwsh` 只读 cmdlet。要求无写目标、无静态风险，且外部路径访问策略允许（`outside.read=allow`
时不做额外限制）。

## 五、目标提取

- **shell**（bash/pwsh）：`core/security/command_lexer.py` + `shell_targets.py` 静态提取
  写/删目标（重定向、`rm/mv/cp/touch/mkdir`、常见 PowerShell cmdlet）；变量/通配符/命令替换
  等动态目标标记 `dynamic=True`，保守按越界待审；
- **python**：`python_targets.py` AST 分析，覆盖 `os/shutil/pathlib/Path.open` 等直接写/删调用；
  非字面量参数按动态目标处理；库方法内部（如 `df.to_csv`）不可见，属已知局限；
- **路径目标**三态：`static`（可解析）、`prefixed`（前缀可确认）、`opaque`（完全动态）。
  静态在安全区内放行；动态前缀在安全区内且不含 `..` 放行；否则按分项策略/审批。

安全区：工作区目录 + 系统临时目录（`os_temp_root`），由 `PathPolicy.is_inside` 判断。

## 六、运行时装配

`core/security/runtime.py::build_permission_stack` 是唯一构造点（SessionRunner 初始化与配置热重载共用）：

- `PermissionStore`：会话批准（`decision_id` → 已批准）与 pending 审批载荷；
- `PermissionRules`：全局 + 工作区规则；
- `PermissionResolver`：判定；
- `PermissionJudge`：lite 判定器。

子代理与根代理共享同一 store/rules。审批审计写 `{session_dir}/approval_audit.jsonl`。

## 七、AI 判定器

- 一次性 `chat_structured`（不起子 agent），默认用 lite 角色，可配 `judge.model`；
- 输入含动作文本、静态证据、可选的最近用户消息（`intent_context`，判断“是否已授权”而非“动作危险”）；
- 结论：allow / ask / deny；高置信才自动执行；deny 不缓存；
- 连续失败达到 `max_consecutive_failures` → 本会话停用自动判定，转 `ask`；
- 连续拒绝 3 次/累计 20 次 → 转人工；
- `persist_allow` 时把允许写成规则；超时/异常按 `on_error`（默认 ask）。

## 八、审批卡与看门狗

审批采用「退出循环 + 占位符 + 恢复」：

1. 判定为 `ask` 时，工具返回 `completed=False` 的占位 `tool_result`，卡片随会话保存，回合结束；
2. 用户提交：`approve`（本次会话）/ `remember`（写工作区规则）/ `deny` / `abort`；
3. `apply_answer` 结算占位符并注入结果，runner 恢复；
4. `approval_watchdog` 每 5 秒扫描空闲 runner：超时结算为“未响应、未执行”；
   `auto_resume=web` 只恢复 Web，`all` 恢复有服务端推送通道的接口（Web/TUI），
   `none` 只结算；服务启动前发出的历史审批只结算不自动恢复；
5. 青少年模式：strict 下未解锁不出卡、判定放行不落盘；受保护路径硬 deny。

## 九、HTTP 与前端

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/PUT | `/api/permissions` | 读取/更新 `setting.json → permissions` |
| GET | `/api/permissions/rules?scope=global|{ws}` | 列出规则（含内置） |
| POST | `/api/permissions/rules` | 新增规则 |
| PATCH/DELETE | `/api/permissions/rules/{rule_id}` | 修改/删除 |
| POST | `/api/permissions/rules/revoke-ai` | 撤销 AI 自动生成的规则 |
| GET | `/api/permissions/judge-stats` | 判定器统计 |

设置页「权限」页签：默认策略、outside 四项、只读白名单、AI 判定参数、审批超时；
工作区设置可维护工作区规则。配置/规则变更后刷新缓存 runner，下一动作生效。

## 十、测试

`test/test_permission_rules_dir.py`、`test/test_permission_judge.py`、
`test/test_permissions_api.py`、`test/test_path_policy.py`、`test/test_dynamic_path_permission.py`、
`test/test_shell_path_policy.py`、`test/test_python_path_policy.py`、`test/test_write_path_policy.py`、
`test/test_aux_tools_path_policy.py`、`test/test_approval.py`、`test/test_approval_timeout.py`、
`test/test_approval_runner_e2e.py`。

## 十一、已知边界

- 静态分析有盲区：库方法内部写文件、动态构造路径、解释型子进程等按“动态目标”保守处理；
- 默认策略偏可用性（`default_mode=allow`），高风险场景应改为 `ask`/`ai` 并收紧规则；
- 会话批准只在进程内，重启后失效（落盘规则例外）；
- 审批恢复依赖接口有推送通道；IM 只结算，等用户下次入站。
