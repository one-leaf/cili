# 邮件接入端设计

> 目标：给 Cili 增加**第四个接入端**——邮箱（IMAP 收 + SMTP 回），与 Web / QQ / 微信**共享同一会话层**。
>
> 范围：**仅单人使用、多账号**（多套邮箱同时在线）；只读 INBOX；纯文本回信 + 附件。
>
> 版本：v1.0 ｜ 日期：2026-11-19 ｜ 状态：**已实现**（P0 + P1；P2 收尾见 §十四）

> **定位**：邮件是**异步任务通道**——用户发一封信让 agent 去做（整理报告、跑分析、写代码后回结果），
> 做完回一封信。它不追求即时性，追求「关掉界面也能派活」。
>
> **本文与既有预留清单的关系**：`workspace/default/docs/design/session-channel-routing-plan-2026-11-16.md`
> §4.8 已给邮件通道留过接入清单（channel 注册、state 复用、inbound、profile）。本设计在此基础上
> 补全**传输层全部细节**，并修正两处：凭证放 `setting.json`（用户在设置页手填，与 QQ secret 同模式，
> 不走微信那种扫码生成的分文件凭证）；**垃圾邮件与滥用防护升级为第一等约束**（§三），
> 这是邮箱通道与 QQ/微信最大的不同。

---

## 一、为什么邮箱最需要垃圾邮件防护

QQ / 微信的入站面受平台约束：QQ 机器人未认证只有管理员能用，微信 ClawBot 只有绑定者能私聊。
**邮箱没有这层平台闸门**——地址一旦被爬虫收录，任何人（或任何自动程序）都能往它发信。
而每封进站信如果都跑一个 LLM 回合，后果是：

1. **烧钱**：垃圾邮件风暴 = 无上限的 token 消耗；
2. **外发被拉黑**：对垃圾邮件自动回信（backscatter）会让该邮箱被反垃圾组织列入黑名单，
   连正常邮件也发不出去，且解封困难；
3. **邮件环路**：两个自动回复程序互相回信，可无限循环；
4. **提示词注入**：陌生人的邮件内容会被模型读到——恶意邮件可以试图指挥 agent。

所以邮箱通道的**默认姿态必须是拒绝**：白名单之外的一切入站内容，不进回合、不回信。

---

## 二、定位与分层

与 QQ / 微信同构。会话层接口无关；本模块补「接入端」这一层。

```
interfaces/email/           邮件接入端（stdlib imaplib/smtplib，宿主线程 + 宿主 loop）
  __init__.py               barrel：start/stop_email_interface + profile 注册
  config.py                 EmailConfig + setting.json 的 "email" 段（accounts 列表）
  state.py                  状态文件接线（复用 interfaces/common/state.py）
  runtime.py                EmailRuntime 容器（账号级，避免循环导入）
  client.py                 EmailClient：IMAP 拉取 + SMTP 发送（阻塞调用，宿主侧走 executor）
  poller.py                 轮询循环：游标推进、UIDVALIDITY 处理、批量上限
  filter.py                 入站过滤（纯函数）：垃圾/自动消息/退信/认证结果/白名单（§三）
  inbound.py                邮件 → 会话回合
  outbound.py               回信：线程头、纯文本正文、附件
  media.py                  入站附件落盘（→ uploads/、图片转 multimodal）/ 出站附件打包
  sink.py                   EmailSink：无流式、无进度，收回合终稿
  commands.py               斜杠指令（复用 interfaces/common/commands.py 同一套）
  profile.py                能力档案注册（name="email"，__init__ import 时注册）
  service.py                宿主线程 + 宿主 loop；模块级幂等启停（HostBase）

core/…（共享层，均不需改）
  channels.py               已登记 email 通道（label「邮件」、标题「邮件会话」、kind=remote）
  turn_dispatch.py          回合驱动
  outbox.py / multimodal.py / text_sanitize.py / interface_profile.py
  interfaces/common/state.py    resolve_turn_session（三元组 = email/邮箱地址/发件人）
```

**无新依赖**：`imaplib` / `smtplib` / `email` 都是标准库。TLS 由 `ssl` 模块承担，
证书校验默认开启（不接受关闭校验的配置——这不是「灵活性」，是帮用户犯错）。

与 QQ / 微信的分层差异：

- **没有流式、没有进度节拍**：邮件是异步的，没人守在收件箱前等。整个回合**只回一封信**
  （`send_user_file` 的附件也放进这封信）。没有 typing、没有中间消息——每多发一封都是噪音。
- **没有 SDK**：IMAP/SMTP 是公开标准协议，标准库实现。与微信一样是「自己的传输循环」。
- **多了一层 filter.py**：QQ/微信的过滤在平台层完成，邮件必须自己做（§三）。

---

## 三、垃圾邮件与滥用防护（核心）

### 3.1 威胁模型

| 威胁 | 后果 | 对策 |
|---|---|---|
| 陌生人发信 | LLM 回合被触发（烧钱）、内容注入 | 白名单默认拒绝（§3.3） |
| 对垃圾回信 | 地址被确认存活 → 更多垃圾；被反垃圾组织拉黑 | 对白名单外**永不回信**（§3.5） |
| 自动回复环路 | 双方互回，无限循环 | RFC 3834 头过滤 + 出站标注 `Auto-Submitted`（§3.4） |
| 退信风暴 | 每条 DSN 都触发回合 | 退信/回执类邮件直接丢弃（§3.4） |
| 伪造发件人 | 冒充白名单地址骗过闸门 | 认证结果校验（§3.4 dmarc） |
| 大批量轰炸 | 崩溃式消耗；队列塞满 | 每发件人/每账号额度 + 每轮批量上限（§3.6） |
| 超大附件 | 磁盘与内存被撑爆 | 读信前看 `RFC822.SIZE`；附件总量上限（§3.8） |

### 3.2 过滤顺序（每封信依次过闸，任一步命中即丢弃）

```
1. 大小闸：RFC822.SIZE > max_inbound_bytes(100MB) → 丢弃（不下载）
2. 结构闸：Content-Type: multipart/report（DSN/MDN 回执）→ 丢弃
3. 自动闸：Auto-Submitted 非空且 ≠ "no" → 丢弃（RFC 3834）
4. 批量闸：Precedence ∈ {bulk, list, junk} / 有 List-Id 头 → 丢弃
5. 退信闸：From 为 MAILER-DAEMON@* / postmaster@* → 丢弃
6. 已标记垃圾闸：X-Spam-Flag: YES 或 X-Spam-Status 以 Yes 开头 → 丢弃
7. 认证闸：见 §3.4（dmarc=fail 或 spf/dkim 双 fail → 丢弃）
8. 白名单闸：发件地址 ∉ allowed_senders（大小写不敏感）→ 丢弃
9. 额度闸：发送者/账号额度超限 → 丢弃（不排队、不回信）
10. 去重闸：Message-ID 命中近期 LRU → 丢弃
        ↓ 全部通过
    owner 刷新 → 指令分流 → 回合
```

所有丢弃都**静默**（不回信、不建状态），只记日志与计数。闸门顺序刻意把「便宜的判断」
放前面：结构/头部检查不需要下载正文。

### 3.3 白名单：默认拒绝，显式授权

```jsonc
"allowed_senders": ["me@example.com", "family@example.com"]
```

- **空列表 = 只收自己的邮件也不行**——空列表意味着没有任何发件人可用，账号整体拒收并
  在状态里提示「未配置 allowed_senders，邮件通道不处理入站」。**不做「第一个发信人自动成为
  owner」**：QQ/微信可以这样是因为平台闸门在先，邮箱这样做等于谁先发信谁接管 agent。
- 匹配用 `email.utils.parseaddr` 解析出的**纯地址**，大小写不敏感。
- 每个白名单发件人各自是一条 chat（三元组 `channel_chat` = 发件人地址），各有独立会话
  （dedicated 模式）。
- `owner` 仍是「最近一次有效来信的发件人」，只做 `/status` 展示与日志，**权限永远由白名单决定**。

### 3.4 认证与自动消息校验

- **Authentication-Results**（由收件方 MTA 写入）：若存在且 `dmarc=fail` → 丢弃；
  若存在且 `spf=fail` 与 `dkim=fail` 同时出现（且无 dmarc=pass）→ 丢弃。
  头部**不存在**时不判失败（小型自建邮件服务器不写这个头，不能因此全拒）。
- **Auto-Submitted**：取值 `auto-generated` / `auto-replied` 等 → 丢弃。出站回信统一带
  `Auto-Submitted: auto-replied`（RFC 3834 建议），与本端入站闸形成闭环——即使对端是
  另一个自动回复程序，也不会互回。
- **Precedence / List-Id**：邮件列表与群发的回信没有意义，丢弃。
- **退信**：`multipart/report` 与 `MAILER-DAEMON` 双闸拦 DSN。
- 以上全部是**纯头部判断**，在 `filter.py` 里实现为纯函数，便于用样本单测。

### 3.5 永不向白名单外回信

丢弃 = 彻底消失，不是「回一封拒绝信」。理由：

1. 回信向垃圾发送者确认了地址存活 → 更多垃圾；
2. 对伪造发件人的退信（backscatter）会投递到无辜第三方，这是收件方被拉黑的经典原因；
3. 任何自动拒绝信本身就是垃圾的一种。

用户在设置页可以看到被丢弃的统计（§3.9），但**系统从不代表用户外发解释性邮件**。

### 3.6 额度与批量上限（成本闸门）

| 项 | 默认 | 说明 |
|---|---|---|
| 每发件人每小时回合数 | 20 | 白名单账号被冒用 / 失控自动发信时的止血带 |
| 每账号每天回合数 | 200 | 总成本上限（约等于正常个人使用够用的量级） |
| 每轮拉取处理封数 | 50 | 积压时不一次性跑 50 个回合；游标持续推进，剩余留给下一轮 |
| 每封正文截断 | 50,000 字符 | 超长正文（newsletter 类排版垃圾）截断并注记 |
| 正文引文剥离 | 剥离 `>` 开头行 | 回信中的引用历史不进上下文 |

超额时：丢弃 + 计数 + 日志（`filter.py` 返回 `RATE_LIMITED`），不回复、不排队。
额度按 `(账号, 发件人)` 与 `(账号)` 两个维度在内存里记时间戳环形表，进程重启清零——
这是**安全阀不是计费系统**，不需要持久化。

### 3.7 首次启动与游标安全

- **首次启动（无游标文件）**：取 INBOX 当前 `UIDNEXT - 1` 作为游标，**跳过全部历史邮件**。
  理由：接线前积压的邮件包含大量垃圾与陈旧内容，逐封跑回合既烧钱又没有意义。
  日志明确写「账号 X 首次启动，跳过历史 N 封」。
- **UIDVALIDITY 变化**（服务端重建邮箱）：游标作废，同样重置到当前 UIDNEXT-1 并记 warning，
  **绝不重放**旧 UID 区间。
- **只读原则**：拉取用 `BODY.PEEK[]`（不置 `\Seen`），不删除、不移动、不标记任何邮件。
  cili 对用户邮箱的副作用为零——它只是「在旁边看」。
- **不读垃圾箱**：只轮询 INBOX。Junk/Spam 目录不碰（即使用户把白名单地址误判进垃圾箱，
  也只是「agent 没回」，不会误处理垃圾）。

### 3.8 附件与体积

- 入站：单封附件总量 ≤ `max_inbound_bytes`（100MB，与 Web 上传一致）；超过则丢弃整封
  （在 §3.2 第 1 步按 `RFC822.SIZE` 提前拦截，不下载）。
- 附件落盘到 `{workspace}/uploads/`，文件名清洗（去路径分隔符与 `..`）；图片走
  `core/multimodal` 转 multimodal block，与 Web/QQ/微信一致。
- 出站（`send_user_file` 转为附件）：单封总量 ≤ 20MB、条数 ≤ 5（能力档案声明，工具登记时
  校验）；超限明确报错，不静默截断。

### 3.9 可观测性（设置页能看到「拦了什么」）

`GET /api/email/status` 每账号返回：

```jsonc
{
  "id": "me@example.com", "running": true,
  "last_poll_at": "2026-11-19 10:32:05", "last_error": "",
  "ignored_24h": { "total": 37, "spam": 20, "auto": 9, "not_whitelisted": 8 },
  "recent_ignored": [ {"from": "noreply@x.com", "reason": "auto", "at": "…"} ]   // 最近 5 条
}
```

用户第一次配置后就能看到「30 多封被拦了」——既是安全感，也是白名单配置是否正确的反馈。

### 3.10 提示词注入的残余风险（明确说明）

白名单挡掉陌生人，但**白名单内的邮件仍可能被转发内容污染**（例如同事转发一封带指令的邮件）。
ceiling 不是本设计能解决的：模型读到的一切文本都是不可信数据，这一点与 Web 粘贴文本相同；
现有审批/工具权限机制照常生效。设置页文案写明「白名单内的邮件内容会自动交给 agent 处理，
请只把可信地址加入白名单」。

---

## 四、配置（`setting.json` 的 `"email"` 段）

多账号模型与 QQ/微信一致：`{"accounts": [...]}`，每个元素一个账号。账号 id = 邮箱地址
（稳定、天然唯一、用户可读）。

```jsonc
"email": {
  "accounts": [
    {
      "id": "agent@example.com",          // = email_address
      "label": "主邮箱",
      "enabled": true,
      "email_address": "agent@example.com",
      "imap_host": "imap.example.com",
      "imap_port": 993,
      "imap_ssl": true,
      "smtp_host": "smtp.example.com",
      "smtp_port": 465,
      "smtp_security": "ssl",              // ssl（465）| starttls（587）
      "username": "agent@example.com",
      "password": "…",                     // 应用专用密码/授权码；GET 时掩码
      "allowed_senders": ["me@example.com"],
      "default_workspace_uuid": "",
      "workspace_locked": false,
      "session_mode": "dedicated",
      "session_locked": false
    }
  ]
}
```

### 4.1 凭证存放与掩码

- `password` 存 `setting.json`，**与 QQ 的 `secret` 同一模式**：`GET /api/config` 返回
  `password_masked`（`_mask_secret`），`PUT` 留空或传掩码值 = 保留原值。
- 状态/游标**不在配置里**（§五），配置接口的读-改-写不会碰游标。
- 日志与错误信息一律不打印 password；`test-email` 只返回成功/失败与错误分类。
- 不做系统钥匙串/ACL 加密：单机自托管场景，与 QQ/微信凭证同等对待。

### 4.2 固定默认值（不进配置、不进设置页）

| 参数 | 默认 | 理由 |
|---|---|---|
| `poll_interval_seconds` | 60 | 异步通道，1 分钟延迟无感；登录频率对各家邮箱都安全 |
| `max_inbound_bytes` | 100MB | 与 Web 上传对齐 |
| `max_outbound_bytes` | 20MB | 主流邮箱服务商单封上限（25MB）留余量 |
| `max_outbound_files` | 5 | 附件不是分发通道，够用 |
| `max_body_chars` | 50,000 | 超出部分截断并注记 |
| `max_messages_per_poll` | 50 | §3.6 |
| `turns_per_sender_per_hour` | 20 | §3.6 |
| `turns_per_account_per_day` | 200 | §3.6 |
| 正文格式 | 纯文本（UTF-8） | 不发 HTML 正文；HTML 解析各家客户端差异大 |

与 QQ/微信相同的实现约定：这些字段保留在 `EmailConfig` 里作为默认值供调用点读，
`from_dict` **不读**它们。

### 4.3 常见邮箱接入参数（设置页提示文案用）

| 邮箱 | IMAP | SMTP | 备注 |
|---|---|---|---|
| QQ 邮箱 | imap.qq.com:993 (SSL) | smtp.qq.com:465 (SSL) | 需在网页版开启 IMAP 并生成**授权码**作密码 |
| 163 邮箱 | imap.163.com:993 (SSL) | smtp.163.com:465 (SSL) | 需开启 IMAP 与客户端授权码 |
| Gmail | imap.gmail.com:993 (SSL) | smtp.gmail.com:465 (SSL) | 需应用专用密码（需两步验证） |
| Outlook | outlook.office365.com:993 | smtp.office365.com:587 (STARTTLS) | 多数账号已禁用密码认证，通常不可用 |

设置页给一行「密码填授权码/应用专用密码，不是登录密码」的提示——这是最常见的配置失败原因。

---

## 五、状态与游标

两层分离，各司其职：

```
data/cili/tools/email/
  state.json                    账号 → owner + chats（共用 interfaces/common/state.py）
  accounts/{email_address}.json 游标与统计：{uidvalidity, last_uid, ignored...}
```

- **state.json** 与 QQ/微信同构（`resolve_turn_session(channel="email", account_id=邮箱地址,
  chat_id=发件人地址)`），无新机制。
- **游标每轮落盘**，按账号分文件（同微信凭证分文件的理由：读-改-写互不覆盖）。
  文件不存在 = 首次启动（§3.7）。
- 过滤统计（§3.9）也落这个文件：`ignored_24h` 的计数是跨重启可展示的，用环形
  `[{"reason","from","at"}]` 保留最近 24h 可聚合的量级（截断保留 200 条足够）。

---

## 六、线程模型

与 QQ/微信相同：`HostBase` 每账号一个守护线程 + 独立 loop。

- 轮询任务跑在宿主 loop 上：`sleep(interval)` → `run_in_executor(IMAP 拉取)` →
  过滤 → 逐封 `run_in_executor(execute_turn)`（同一账号串行跑回合，天然不撞同会话认领）。
- `imaplib` / `smtplib` 是阻塞的：所有网络调用走 `loop.run_in_executor`，不卡宿主 loop。
- **每轮新建连接**（login → select → fetch → logout）：IMAP 连接有服务端空闲超时，
  每分钟一次登录对主流服务商无压力，换来「无需实现断线重连状态机」。连接失败记
  `last_error`，下一轮自然重试；连续失败不退避（间隔本来就是 60s）。
- 不做 IMAP IDLE：需要长连接 + 重连 + 断线续订状态机，收益只是把 60s 延迟降到亚秒级——
  与异步任务的定位不匹配。

---

## 七、入站流程

```
poller.run():
  每 60s：
    读游标 → IMAP FETCH UID > last_uid（最多 50 封，先看 RFC822.SIZE）
    → 逐封 filter.check(...)  ← §3.2 十道闸
    → 通过者交给 inbound.handle()；被拦者计数
    → 游标推进到最后处理的那封（无论通过与否）
```

`inbound.handle()` 与微信同构：

1. owner 刷新（`OwnerTracker.track(发件人)`，只做展示）。
2. 正文解析：优先 `text/plain`；只有 `text/html` 时用标准库转纯文本（去标签、反转义）；
   剥离 `>` 引文行；正文为空且无附件 → 丢弃。
3. `/` 开头的纯文本首行 → 指令分流（`interfaces/common/commands.py`：`/h /w /s /new /mode /status /stop`），
   回信即指令回复。
4. 附件落盘 → 图片转 multimodal block。
5. `resolve_turn_session` → 空工作区自动建「邮件会话」（quirk 与 QQ/微信一致）。
6. `registry.claim` → `prepare_turn` → `runner.set_turn_interface("email")` → `execute_turn`。
7. 回合终稿 → 回一封信（§八）；回合异常 → 回一封简短错误说明（只发给白名单发件人）。

`ask_user` / 审批：与 QQ/微信共用编号文本渲染（`interfaces/common/ask_user.py`）——提示折进
回信正文，用户回一封只含 `1` 的邮件即视为答案（同一 chat 的会话里存在 pending ask_user
时 `resolve_answer` 自动映射）。

---

## 八、出站：一封信装完

### 8.1 回信结构

- **To**：本回合的触发发件人（不是配置 owner，不是白名单全体）。
- **Subject**：`Re: <原主题>`（已以 `Re:` 开头则不重复）；无主题用「Cili 回复」。
  主题拼装前剥离 `\r\n`（头注入防御）。
- **线程头**：`In-Reply-To: <触发信 Message-ID>`、`References: <触发信 References + Message-ID>`
  ——回信落在用户原邮件的会话线程里（邮件客户端按线程折叠）。
- **正文**：纯文本 UTF-8；agent 的 markdown 按纯文本可行形式输出（代码块原样、表格
  不强求对齐）——由能力档案 guidance 告知模型（§九）。
- **附件**：`send_user_file` 登记的文件作为本信附件（§8.3）。
- **头**：`Auto-Submitted: auto-replied`（§3.4 环路防护）、`From: <label> <email_address>`。

### 8.2 一次回合只发一封

- 没有流式、没有进度、没有「排队中」提示——用户不需要实时反馈，多一封都是噪音。
- 正文超 `max_body_chars` 截断并注记「（内容过长，已截断）」——不静默丢。
- 发送失败（SMTP 错误/连接失败）：记日志与 `last_error`，**不重试、不落队列**——
  下一封信到来时会重新跑回合，重放旧回复没有意义；用户在 `/api/email/status` 能看到错误。

### 8.3 出站文件

`send_user_file` 在 email 回合可见（`INTERFACE_TOOLS` 声明），由 `core/outbox` 在回合末
取走，打包为本信附件：单封总量 ≤ 20MB、条数 ≤ 5（能力档案声明、工具登记时整批校验）。
发不出去的件按现有 file-delivery 约定落交付回执（`_meta.delivery_notice`）。

---

## 九、能力档案

`interfaces/email/profile.py`（`__init__` import 时注册，name = `"email"`）：

```python
InterfaceProfile(
    name="email",
    label="Email (IMAP/SMTP)",
    renders=("plain text",),
    not_rendered=("markdown tables", "inline images", "mermaid", "SVG", "LaTeX", "raw HTML"),
    max_message_chars=200_000,      # 邮件本身无单片上限；§4.2 的截断是防御性上限
    inline_image_exts=(),           # 不回 inline 图片；图片一律附件
    streaming=False,
    can_send_file=True,
    file_delivery="attachment",
    file_size_limit_mb=20,
    max_files_per_turn=5,
    guidance=(
        "Replies are plain-text email; avoid tables and inline images. "
        "Use short paragraphs and simple lists.",
    ),
)
```

system prompt 的 Response Surface 段自动按此生成，与 QQ/微信同机制，无需改 prompt 代码。

---

## 十、会话绑定

完全复用 2026-11-16 通道方案：

- 三元组 = (`"email"`, 邮箱地址, 发件人地址)；标题「邮件会话」（`core/channels.py` 已登记）。
- 默认 `session_mode = dedicated`（每个发件人各自一条会话）；用户可在账号卡片切 `latest`。
- `session_locked` / `workspace_locked` / `/w` `/s` `/new` `/mode` 行为与 QQ/微信完全一致。
- **不需要改 core**：`channels.py` 的 email 行早已存在；本设计落地时只是让它开始有实现方。

---

## 十一、Web API 与设置页

### 11.1 配置接口

| 端点 | 变化 |
|---|---|
| `GET /api/config` | `email` 段逐账号掩码 `password` → `password_masked`；已下沉的调参字段不出现 |
| `PUT /api/config` | `email.accounts` 全量替换；password 留空/掩码 = 保留；`id` 必须等于 `email_address`；`allowed_senders` 逐个 `parseaddr` 校验（非法地址 400）；`default_workspace_uuid` 校验同 QQ/微信 |
| `POST /api/config/test-email` | 新增：IMAP 登录 + SELECT INBOX（读 UIDVALIDITY）+ SMTP 连接认证（**不发信**）；返回 `{ok, imap: "...", smtp: "..."}` |
| 开关 diff | `web/routes_config.py::_apply_account_enabled_changes` 增加 `email` 分支（起/停 `start_email_interface(id)`），与 QQ/微信同位置 |

### 11.2 状态接口

| 端点 | 返回 |
|---|---|
| `GET /api/email/status` | §3.9 的每账号结构（running / last_poll_at / last_error / ignored_24h / recent_ignored） |

### 11.3 设置页（`#tab-email`）

账号卡片（与 QQ/微信卡片同布局）+ 底部「添加账号」：

- 字段：label、启用、邮箱地址、IMAP/SMTP 主机端口与加密方式、用户名、密码（占位显示掩码）、
  **白名单发件人**（多行文本，一行一个）、默认工作空间下拉、锁定工作空间、会话方式、锁定会话。
- 每张卡片：「测试连接」按钮（带 `id`）+ 状态行（运行中/未运行、最近轮询、拦截统计、
  最近 5 条被拦记录）。
- 白名单为空时卡片顶部黄条：「未配置白名单，该账号不会处理任何来信」。
- 密码框旁固定提示：「填授权码/应用专用密码，不是登录密码」。

---

## 十二、对既有代码的改动面（很小）

| 位置 | 改动 |
|---|---|
| `interfaces/email/`（新增） | 本设计全部模块 |
| `core/app_context.py` | startup/shutdown 各加一处 `start_email_interface()` / `stop_email_interface()`（与 QQ/微信并列） |
| `web/routes_config.py` | `_apply_account_enabled_changes` 加 `email` 分支；GET 掩码加 email 段；`/api/config/test-email` |
| `web/routes_email.py`（新增） | `GET /api/email/status` |
| `web/web_api.py` | 挂载 `email_router` |
| `web/static/index.html` + `settings.js` | 新增 `#tab-email` 页签与账号卡片 |
| `data/cili/setting.example.json` | email 段示例 |
| `docs/design/email-interface-design.md` | 落地后归档 + `CLAUDE.md` 设计文档表与架构树补充 |

`core/` 与 `interfaces/common/` **零改动**——这是 2026-11-16 通道方案预留工作的直接兑现。

---

## 十三、明确不做

- **群组邮箱/共享邮箱/邮件列表**：白名单之外一律拒绝，邮件列表在 §3.2 直接丢弃。
- **IMAP IDLE**：轮询 60s 够用，IDLE 的重连状态机不值当。
- **HTML 回信**：纯文本保真、无 CSS 兼容问题、不会渲染成钓鱼样式。
- **读取 Junk/Spam/其它文件夹**：只看 INBOX。
- **保存副本到 Sent 文件夹**：各家文件夹命名不一（Sent / 已发送 / 「已发送邮件」），
  追加逻辑脆弱；用户自己的邮件客户端里看线程回复即可。（后续如强需求可加账号级开关。）
- **对白名单外发送者回拒绝信**：§3.5。
- **邮件内附件自动执行/解压**：附件只是文件，落盘后由模型通过工具处理。
- **POP3**：IMAP 是唯一形态。
- **多用户**：仍为单用户；白名单是「多个可信地址」，不是多租户。
- **OAuth2 邮箱登录（Gmail/Outlook 的现代认证）**：首版只支持应用专用密码。
  Outlook 多数账号因此不可用（§4.3 已注明）；后续如需支持 OAuth2 再单独立项。

---

## 十四、实施步骤

### P0 — 通道本体（可独立验收）

1. `interfaces/email/config.py`：`EmailConfig` + `load_email_accounts` / `save_email_accounts`（兼容空段）。
2. `interfaces/email/filter.py`：十道闸纯函数 + 额度表；配套样本单测（§十五）。
3. `interfaces/email/client.py`：IMAP 拉取（`BODY.PEEK`、`RFC822.SIZE`、UIDVALIDITY）与 SMTP 发送（SSL/STARTTLS）。
4. `interfaces/email/state.py` / `runtime.py` / `poller.py`：状态接线、游标落盘、轮询循环。
5. `interfaces/email/inbound.py` / `outbound.py` / `media.py` / `sink.py` / `commands.py`：
   回合链路与一封信出站。
6. `interfaces/email/profile.py` / `service.py` / `__init__.py`：档案注册、宿主多实例、启停。
7. `core/app_context.py` 接线。

**验收**：两个邮箱账号同时在线，各自只收白名单邮件；陌生人来信不触发回合、不外发；
回信线程正确落在原邮件会话里；重启后不重放、不丢信（游标持久）。

### P1 — 管理面

8. `web/routes_config.py`：掩码、test-email、开关 diff 的 email 分支。
9. `web/routes_email.py`：status（含拦截统计）。
10. 设置页 `#tab-email`：账号卡片、测试连接、拦截可见性。
11. `setting.example.json` 示例。

**验收**：设置页可完成全部配置与自助排障（测试连接、错误信息、拦截统计）；
开关账号不影响其它账号。

### P2 — 收尾

12. 设计文档归档到 `docs/design/`、更新 `CLAUDE.md`。
13. 全量定向套件 + ruff。

---

## 十五、测试计划

| 文件 | 覆盖 |
|---|---|
| `test/test_email_filter.py`（新增） | 十道闸逐条：超大、DSN、Auto-Submitted、Precedence/List-Id、MAILER-DAEMON、X-Spam 标记、dmarc=fail、非白名单、空白名单、大小写不敏感、Message-ID 去重、额度超限、无 Authentication-Results 头**不**误杀 |
| `test/test_email_poller.py`（新增） | 首次启动跳过历史；游标推进与持久化；UIDVALIDITY 变化重置不重放；每轮 50 封上限；`BODY.PEEK` 使用（fake client 断言） |
| `test/test_email_inbound.py`（新增） | 纯文本/HTML-only 正文解析；引文剥离；指令分流；附件落盘与 multimodal；回合异常回错误信；ask_user 数字回信映射 |
| `test/test_email_outbound.py`（新增） | Re: 前缀、线程头拼接、主题头注入清洗、Auto-Submitted 标注、附件总量/条数校验 |
| `test/test_email_config.py`（新增） | 账号列表读写、白名单地址校验、空段、幂等 |
| `test/test_web_api.py` / `test_email_routes.py`（补充/新增） | 掩码不泄露密码；PUT 留空保留密码；test-email 失败分类；status 结构 |

验收口径同既有约定：**定向套件 + ruff**，全量套件的存量污染不作为回归信号。

---

## 十六、风险与取舍

1. **白名单配置是使用门槛**：配置完不发信会以为坏了。缓解：设置页黄条 + 拦截统计可见，
   第一次收到「被拦 37 封」就知道闸门在工作。
2. **应用专用密码的可用性**：部分服务商（尤其 Outlook）已禁用密码认证；首版只能支持
   仍开放 IMAP + 应用密码的服务商（QQ/163/Gmail 可用）。OAuth2 留作后续立项。
3. **60s 轮询延迟**：异步定位下可接受；真需要秒级响应时用户会用 QQ/微信。
4. **SMTP 发送失败不重试**：极端情况下回复丢失，用户下次来信会重新触发。取舍是避免
   重试队列与重复回信（重复回信对用户更糟）。
5. **正文截断 5 万字符**：超长邮件（含大量转发的长线程）会丢失尾部内容。截断显式注记，
   且引文剥离已去掉大部分冗余。
6. **每轮只发一封 = 长回合期间零反馈**：符合异步定位；用户在 Web 端可看到同一会话的实时轨迹
   （共享会话 + 全局事件总线）。
7. **`Auto-Submitted: auto-replied` 的副作用**：个别反垃圾系统会给带该头的邮件加分。
   但环路防护的价值更高，且它是 RFC 建议的合规做法。

---

## 十七、验收标准

1. 两个邮箱账号同时在线，互不干扰；关一个不影响另一个。
2. 陌生人来信：不建会话、不跑回合、不外发任何邮件，仅计数。
3. 白名单来信：60s 内在独立会话里跑完回合，回信落在原邮件线程。
4. 退信、自动回复、邮件列表、带 X-Spam 标记的邮件：一律不触发回合。
5. 首次启动跳过历史；重启后从游标继续，不重放不丢信。
6. 每封来信的正文与附件正确进入模型上下文；图片为 multimodal。
7. 发件人被冒用（dmarc=fail）：丢弃。
8. 超出额度：丢弃 + 计数，设置页可见。
9. `send_user_file` 产物作为附件随回信发出；超限明确报错。
10. 设置页：配置、测试连接、启停、拦截统计全部可用；`/api/config` 不泄露密码。
