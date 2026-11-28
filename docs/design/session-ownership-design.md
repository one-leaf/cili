# 会话归属与共享协作设计

> 按当前实现整理（2026-11-28）。代码：`core/node_identity.py`、`core/session_owner.py`、
> `core/session.py`、`web/routes_workspace.py`、`web/routes_chat.py`、`web/static/{sidebar,chat}.js`。

## 一、目标

共享工作区里的会话可以互相看见，但「继续写 / 删除 / 改名 / 隐藏 / 撤销」只允许 owner：

- 自己的会话：正常对话与修改；
- 别人的会话：只读查看；发送时提示并 fork 副本；
- 未归属的旧会话：放行（保持兼容）；
- 列表默认显示全部并标出归属，支持「只看我的 / 只看他人」。

## 二、节点身份（`core/node_identity.py`）

- 首次启动生成 `{DATA_DIR}/node.json`：`{version, id, name, created_at, updated_at}`；
- `id` 形如 `node-<12 位十六进制>`；`name` 默认主机名（清洗控制字符，≤120 字）；
- 可用 `CILI_NODE_ID` / `CILI_NODE_NAME` 覆盖（`ensure_node_identity` 会写回环境变量）；
- 只是设备标识，不是凭证，不含用户名/密钥/路径；节点身份也用于 cron 归属、课件记录分文件等。

## 三、会话归属字段

新建会话时由 `SessionStore.create_new_session` 合并 `owner_metadata(metadata)` 写入
`meta.json -> metadata`：

| 字段 | 来源 |
|---|---|
| `owner_node_id` / `owner_node_name` | 本机节点身份（显式传入则保留） |
| `owner_user_id` / `owner_user_label` | 组织登录用户（`core.org.store.load_session` 的 `user.id/name`）；未加入组织时回退 `channel_account` / `account_id` |

判定（`is_mine`）：

- 无 owner 字段 → `None`（未归属的旧会话，放行）；
- `owner_user_id` 与当前组织用户一致 → `True`；
- `owner_node_id` 与当前节点一致 → `True`；
- 否则 `False`。

## 四、行为约束

| 操作 | 别人的会话 | 未归属 |
|---|---|---|
| 查看 / 读取消息 | 允许 | 允许 |
| 发送消息 | 403：`这是 {label} 的会话；请先创建副本再发送` | 允许 |
| fork 副本 | 允许（副本归当前节点） | 允许 |
| 删除 / 改名 / 隐藏 / 撤销 | 403（批量操作逐条返回失败原因） | 允许 |

- 路由守卫：`_ensure_session_owned`（删除等）与 `is_mine(...) is False` 判断（发送/撤销/批量）；
- 批量操作逐条校验，属于他人的条目返回 `success=false` 与归属标签，不中断整批；
- 归属字段在会话列表接口里展开为 `owner_node_id/name、owner_user_id/label、owner_mine`；
- 前端 `sidebar.js` 标注「我的会话 / 他人 / 未归属」，`chat.js` 在他人会话发送前提示 fork。

## 五、列表过滤

`GET /api/workspaces/{ws}/sessions?scope=all|mine|others`：

- `mine`：`owner_mine === true`；
- `others`：`owner_mine !== true`（含未归属）；
- `all`：不过滤；默认排序按 mtime 倒序。

## 六、与其它子系统的关系

- **组织托管**：加入组织后 `owner_user_id` 取组织用户；退出组织后旧会话仍按 node id 归属；
- **通道会话**（QQ/微信/CLI）：`metadata.channel_account/account_id` 在无组织用户时作为用户标识；
- **cron**：定时任务另有 `owner_node_id`，由 `core/cron/service.py` 写入、`engine.py` 只在本节点调度；
- **青少年模式**：会话删除/隐藏/撤销仍额外受 teen 硬禁/策略约束，优先级高于归属判断。

## 七、测试

`test/test_node_identity.py`、`test/test_session_owner.py`。

## 八、已知边界

- 归属是**协作提示**，不是安全边界：同机可改文件、可伪造 metadata；
- 未归属旧会话一律放行，迁移后不会自动补 owner；
- 用户身份依赖组织登录或通道账号；纯本地用户只按 node id 区分（同机多用户不可分）；
- 换机后 node id 不同，旧会话在新机视为“他人会话”（只读 + fork）。
