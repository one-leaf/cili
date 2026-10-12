# 微信接入端设计

> 目标：给 Cili 增加第三个接入端——**个人微信**（iLink 协议），与 Web / QQ **共享同一会话**。
>
> 范围：**仅私聊、仅单人使用**（一个 owner）；文本/图片入站、文本/文件出站。
>
> 版本：v1.0 ｜ 日期：2026-10-12

---

## 一、定位与分层

与 QQ 接入端同构：会话层接口无关（`core/session_registry.py` 持有 runner 池与运行权
认领），本模块补「接入端」这一层。最大的差别：**iLink 没有官方 Python SDK**——
`protocol.py` / `client.py` / `login.py` / `poller.py` 都是本项目用 httpx 直接实现的
（协议对标腾讯官方 npm 插件 `@tencent-weixin/openclaw-weixin` v2.4.6）。

```
interfaces/wechat/         微信接入端（纯 HTTP，宿主线程 + 宿主 loop）
  __init__.py              barrel：start/stop_wechat_interface 等
  config.py                WechatConfig + setting.json 的 "wechat" 段
  account.py               凭证与游标（account.json）+ context_token 台账
  state.py                 owner + 当前工作区/会话（单用户，全局一份）
  access.py                owner 记录（自动绑定 + 每次对话刷新）
  runtime.py               WechatRuntime 容器（避免循环导入）

  protocol.py              协议纯函数层：请求头/消息编解码/错误分类/AES 加解密
  client.py                HTTP 客户端（8 端点 + 重试；可注入 MockTransport）
  login.py                 扫码登录状态机 + 内存会话表（Web 每 2s 轮询）
  poller.py                长轮询循环（游标、去重、群聊过滤、退避）
  inbound.py               消息 → 会话回合
  outbound.py              发送：token 刷新 + 预算 + defer 队列
  sink.py                  WechatSink：进度节拍 + 正文缓冲 + 扇出全局事件流
  typing.py                「正在输入…」票据 + 5s 续期
  media.py                 入站下载解密 / 出站加密上传
  sanitize.py              微信 markdown 清洗（代码区外 <> 转全角等）
  commands.py              斜杠指令（与 QQ 同套：/h /w /s /new /status /stop）
  service.py               宿主线程 + 宿主 loop；模块级幂等启停

core/…（共享层，QQ 亦用）
  media_fetch.py           SSRF 校验 + 流式下载（transform 钩子供微信边下边解）
  text_chunk.py            markdown 感知切分（QQ 传 4000、微信传 1800）
  text_sanitize.py         脚手架剥离（system-reminder / think 标签）
  multimodal.py            入站媒体 → user_input（图片 multimodal block）
  config.py                get_workspace_data_dir 的 Default 回落（测试隔离，见八）
  outbox.py / turn_dispatch.py / default_sink.py / session_registry.py   与 QQ 共用
```

与 QQ 的分层差异：
- **没有传输线程**：QQ 的 WebSocket 线程归 SDK；微信的长轮询循环是自己的（`poller.py`）
- **没有 rawapi / stream / limiter**：iLink 不能更新已发消息（无流式）、配额模型不同
  （每 context_token 约 8 条，而非每消息 4 次被动回复）
- **策略层与 QQ 同构**：sanitize / chunk / multimodal 从 QQ 提取到 core 共用；微信特有
  的 markdown 清洗（`<>` 转全角等）在 `wechat/sanitize.py`

---

## 二、协议来源与「OpenClaw」品牌

腾讯开放的个人微信 Bot 能力（iLink，`ilinkai.weixin.qq.com`）的官方载体是 npm 包
**`@tencent-weixin/openclaw-weixin`**——scope 是 `@tencent-weixin`，微信团队维护；
微信侧产品名「微信 ClawBot」。协议字段与错误码以官方插件 v2.4.6 为准（社区逆向：
`github.com/hao-ji-xing/openclaw-weixin`）。

推论：**扫码确认页与《微信ClawBot功能使用条款》由腾讯服务端下发，品牌显示
OpenClaw/ClawBot**——与哪个程序发起登录无关（cili 用 httpx 直说协议，腾讯视角与
OpenClaw、nanobot 的登录无异）。条款保留限速/拦截/终止权利 → 不要把核心业务押在上面。

---

## 三、线程模型

**只有一个线程是我们的**：宿主线程跑宿主 loop——长轮询、回合驱动、出站发送全在它上面。
没有 QQ 那样的第二个传输线程：HTTP 长轮询的循环就是 `poller.py` 自己。

- 阻塞的 `runner.run()` 走 `run_in_executor`，不卡宿主 loop
- sink 的同步回调经 `run_coroutine_threadsafe` 派回宿主 loop（同 QQ）
- 登录流程**请求驱动**：Web 每 2s 调一次 `/api/wechat/login/status`，无常驻后台任务，
  用户关掉设置页也不会留下后台任务

---

## 四、iLink 协议的限制（决定实现方式）

### 4.1 context_token（最硬的约束）

| 项 | 值 |
|---|---|
| 来源 | 每条 inbound 消息携带，**回复必须原样带上** |
| 有效期 | 服务端约 **90–160s**；发送前 age ≥ 60s 先 `getconfig` 换新 |
| 配额 | **约 8 条**/token（服务端未公开，社区实测）；超出回 `-2` |
| `-2` 的应对 | 文本进 **defer 队列**（每会话 3 条、丢最旧），下一条 inbound 带新 token 时按序重放；**`client_id` 复用**保证服务端幂等不重复 |

本地预算兜底在打 API 之前：`used + 1 + reserve ≤ context_message_budget(8)`，超了直接
拒——等 API 报错时那条消息已经白发一次了。

### 4.2 消息形态

- 单条文本 ≤ **1800 字符**；markdown 在微信客户端会吞裸 `<` → 代码区外 `<>` 转全角、
  删 `~~` / 图片语法 / 5-6 级标题（**代码区逐字节保留**）
- **无增量投递**：发出去的消息不能更新 → 做不出 QQ 那种流式 transcript
- item 类型：1 text / 2 image / 3 voice / 4 file / 5 video（11/12 工具进度项本设计未用）
- 错误码：`-2` 配额（defer）/ `-3` 参数 / `-14` token 失效（重新扫码）；
  可重试 HTTP = {408, 425, 429} ∪ ≥500；连续 3 次失败退避 30s

### 4.3 二维码登录状态机

`get_bot_qrcode(bot_type=3)` → `get_qrcode_status` 轮询（~1s）：

| status | 处理 |
|---|---|
| wait / 未知 / 可重试网络错误 | 继续等 |
| scaned_but_redirect | 换 `redirect_host` 继续轮询（刷新二维码后重置回默认接入点） |
| need_verifycode | 前端收集验证码带 `verify_code` 重试 |
| verify_code_blocked / expired | 刷新二维码（上限 3 次） |
| confirmed | 保存 `bot_token` / `baseurl` / `ilink_user_id` |
| binded_redirect | 本地有凭证即算成功——cili 要的是「能用」，不是「拿到一批新凭证」 |

**凭证与配置分离**：`bot_token` 等存 `data/cili/tools/wechat/account.json`，不进
`setting.json`——Web 配置接口天然不泄露凭证；`/api/wechat/status` 亦不含 token。

---

## 五、输出：进度节拍 + 正文

### 5.1 为什么没有流式

iLink 不支持更新已发消息——QQ 的「单条流式 transcript」（前缀不可变 replace）在微信
**不存在对应 API**。回合内反馈只剩两种手段：typing 指示器（`sendtyping` + 5s 续期，
不占配额）和**发新消息**（每条都吃 context_token 预算）。

### 5.2 指数进度节拍

工具调用累计到 **1 / 2 / 4 / 8 / 16 / 32 / 64 次**时各发一条进度消息：

```
🔧 已调用 1 次工具（read），仍在进行…
🔧 已调用 2 次工具（read），仍在进行…
🔧 已调用 4 次工具（grep、bash），仍在进行…
…
🔧 已调用 64 次工具（…），仍在进行…
```

- **指数间隔**：短回合最多 1-2 条不打扰；长回合 7 条封顶——**7 条进度 + 1 条正文正好
  用满一份 8 条预算**
- 内容 = 累计次数 + 自上条进度以来的工具名（去重、最多 3 个），不含结果
- **进度是心跳不是内容**：失败（-2 / -14 / 网络）直接丢弃，不进 defer、不重试——
  不能让心跳挤掉正文的位置
- `tool_progress: false` 可整体关闭（回到「只有 typing + 答完一条」）

### 5.3 配额账

| 用途 | 条数 |
|---|---|
| 进度消息 | ≤ 7 |
| 正文（回答 + ask_user 折进尾部；**不含工具轨迹**） | 1（超 1800 字切分，受剩余预算压缩，截断显式标注） |
| 出站文件（`send_file`） | ≤ 3，与正文共享剩余预算 |
| 合计 | **≤ 8** |

**工具轨迹由进度承载、正文只装回答**——轨迹写两遍既冗余又挤占第 8 条；上次进度之后
的剩余调用在正文前补一行「🔧 累计 N 次工具调用」（仅当确实发过进度，进度关闭时不
凭空冒出）。

进度与正文共享预算的代价：进度吃满时出站文件本轮发不出（`send_file` 明确提示）。
发不出去**不静默丢**：文本截断显式标注、文件明确报错。

### 5.4 typing

`getconfig` 取 `typing_ticket`（TTL 24h 缓存），回合内 `sendtyping status=1` + 5s 续期，
收尾 `status=2` 取消。进度消息**不**打断 typing。

---

## 六、入站

1. **只处理私聊**：`from_user_id` 以 `@chatroom` 结尾（群聊）与 `message_type=2`（自己）
   在 poller 层就丢弃——官方插件能力元数据只声明单聊
2. **去重**：`message_id`（缺省 `from_user_id + create_time_ms`）LRU 1000；**游标
   `get_updates_buf` 每轮落盘**——丢了重启会把服务端积压消息重收一遍
3. **owner 自动绑定**（同 QQ）：ClawBot 形态下与 bot 私聊的就是**绑定微信的主人本人**，
   owner 即绑定者。若将来把 bot 入口分享给他人（或产品开放好友对话），第一个发送者会
   成为 owner——届时需要加白名单。owner 只作状态记录与日志，不是访问闸门
4. **媒体**：AES-128-ECB 解密。key 两种形态——图片在 item 顶层 `aeskey`（hex）、其余在
   `media.aes_key`（base64，解出 raw16 或 hex 串）；**非图片必须带 key**。下载经
   `core/media_fetch` 的 `transform` 钩子**边下边解**（不落密文中间文件）；图片按魔数
   嗅探真实类型并改扩展名（`read_image` 等按扩展名判类型的工具会把错误 `media_type`
   报给模型接口，那边直接报错）
5. **命令** → 与 QQ 同套：`/h /w /s /new /status /stop`

---

## 七、出站文件：`send_file`

链路（对标官方插件）：随机 16 字节 key → `getuploadurl`（报 **hex** key 与 PKCS7 后
大小）→ AES-128-ECB 加密整文件 POST CDN → 读响应头 `x-encrypted-param` 作下载参数 →
`sendmessage` 携带媒体项。

⚠️ **`media.aes_key` 填 `base64(hex串)` 而非 `base64(raw16)`**——两个编码用错服务端
不报错，只会让用户下载到坏文件。

路径白名单（realpath 双侧比对）为纵深防御：`send_file` 工具已校验，`media.py` 再校验
一次——本模块才是真正把文件内容交给网络的地方。

---

## 八、对既有代码的改动

- **`core/config.py`**：未知 uuid 的回落从硬编码 `PROJECT_ROOT/workspace/.cili` 改为
  **优先查索引里 Default 工作区的目录**——生产行为等价，但让 `from core.config import
  get_workspace_data_dir` 的直接导入方（session_search、memory 工具）也被测试索引覆盖
- **`test/conftest.py`**：隔离索引补注册 `Default → .test`；测试数据（会话/记忆/工具
  状态）全部收编 `.test`，不再漏进真实默认工作区（全量回归 1623 用例 0 失败）
- **`core/` 提取共享层**：`media_fetch` / `text_chunk` / `text_sanitize` / `multimodal`，
  QQ 侧改再导出（import 路径不变，155 个 QQ 测试全绿）
- **`requirements.txt`**：`cryptography`（AES-128-ECB）、`qrcode`（服务端渲染二维码 PNG）
- **CLAUDE.md**：设计文档表补本篇与 QQ 篇

---

## 九、未做（明确排除）

- 群聊（官方插件能力元数据只声明私信）
- 多用户（owner 单用户模型）
- 语音转写（微信自带转写文本优先；无转写只留文件路径，不接 STT）
- 视频/语音出站（只做图片与文件）
- 消息撤回、历史消息 API（协议未提供）
- 工具进度的 item 类型 11/12（结构化进度项——本设计用文本心跳替代）
- 流式（协议不支持更新已发消息）
