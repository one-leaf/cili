# 邮件接入端设计

> 按当前实现重写（2026-11-25）。代码：`interfaces/email/`、`web/routes_email.py`。

## 一、形态

- IMAP 收 + SMTP 回（stdlib `imaplib/smtplib`，无新依赖）；**多账号**、**默认拒绝**。
- 只有 `allowed_senders` 白名单内的来信才触发回合；其余静默丢弃且**永不回信**
  （反 backscatter / 环路）。
- 一回合只回**一封纯文本邮件**；`send_user_file` 转为附件随回信发出。

## 二、模块

| 文件 | 职责 |
|---|---|
| `config.py` | 账号配置与默认值（轮询 60s、入站 100MB、出站 20MB/5 件、正文 5 万字符、每轮 50 封） |
| `state.py` | 会话状态（账号 → 聊天）与旧格式兼容 |
| `cursor.py` | IMAP 游标（UIDVALIDITY + last_uid）与拦截统计（每账号一文件） |
| `client.py` | IMAP/SMTP 基础操作（stdlib） |
| `poller.py` | 轮询 → 头部闸 → 额度/去重 → 下载正文 → 交给 inbound |
| `filter.py` | 十道闸纯头部判断 + 额度/去重实现（`RateLimiter` / `SeenIds`） |
| `inbound.py` | 回合执行：附件落盘、认领会话、`turn_dispatch`、回信 |
| `outbound.py` | 回信构造（线程头 In-Reply-To/References、纯文本、附件、体积/条数裁剪） |
| `commands.py` / `owner.py` / `profile.py` / `service.py` / `runtime.py` | 指令/归属/档案/多账号启停/单账号运行时 |

## 三、十道闸（`filter.check_headers` + 有状态闸）

纯头部闸（顺序即代价顺序，不需要下载正文）：

1. `too_large`：超过 `max_inbound_bytes`（默认 100MB）。
2. `dsn` / `bounce`：DSN/MDN 结构（multipart/report、空信封、MAILER-DAEMON、postmaster）。
3. `auto`：`Auto-Submitted` 自动消息。
4. `bulk`：`Precedence: bulk/list/junk` 或 `List-Id`。
5. `spam`：`X-Spam-*` 标记。
6. `auth`：SPF/DKIM/DMARC 失败（Authentication-Results）。
7. `not_whitelisted`：发件人不在白名单（**默认拒绝**）。

有状态闸（头闸之后）：

8. `quota`：每发件人/小时（默认 20）、每账号/天（默认 200）——内存环形表，
   冒用/失控时的止血带。
9. `duplicate`：`SeenIds`（内存 + 持久化游标内去重）。
10. 首次启动 / UIDVALIDITY 变化：**跳过历史不重放**。

## 四、轮询与游标

- `cursor_path(account_id)`：`data/cili/tools/email/accounts/{id}.json`，
  含 `uidvalidity`、`last_uid`、最近拦截事件（最多 200 条，24h 展示）。
- 每轮取新邮件（最多 `max_messages_per_poll`），逐封过闸；增量更新游标。
- 认证错误上抛，服务层标记账号异常。
- `GET /api/email/status`：账号运行态、`ignored_24h` 统计（不含凭据）。

## 五、回信

`outbound.build_reply`：

- 纯文本正文；`From/To/Subject`（`Re: ` 前缀）、`In-Reply-To`/`References` 线程头；
- 附件：来自本回合 outbox（`send_user_file`），按 `max_outbound_files`（5）与
  `max_outbound_bytes`（20MB）裁剪，裁剪结果写交付回执；
- `ReplyContext` 从入站邮件取收件人/主题/Message-ID。

## 六、配置

`EmailConfig` 白名单字段：`id`（= 邮箱地址，小写）、`email_address`、`password`（掩码回传保留）、
`imap_host/port/security`、`smtp_host/port/security`、`allowed_senders`、`enabled`、
`session_mode`、`session_locked`、额度与体积参数。

## 七、安全

- 默认拒绝：非白名单来信**静默丢弃且不回信**。
- 附件路径位于工作区（`delivery_path` 校验）；入站附件走 `core.media_fetch` 的 SSRF/体积限制。
- 密码在 GET 设置时掩码；凭据不写入日志。
