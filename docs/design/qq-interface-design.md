# QQ 接入端设计

> 目标：给 Cili 增加**第二个接入端**——官方 QQ 机器人，与 Web **共享同一会话**。
>
> 范围：**仅私聊（单聊 C2C）、仅单人使用**（一个 owner）。
>
> 版本：v1.0 ｜ 日期：2026-10-10

> **v1.1 增补（2026-10-18）：多账号支持**。变更摘要——config 段改为
> `{"accounts": [...]}`（节流、上限等调参字段下沉为固定默认值，不再出现在配置与
> 设置页）；state 按「账号 → 聊天」两级分键；`service.py` 每账号一个宿主线程
> （start/stop/running 增加可选 `account_id`，无参 = 全部账号）；ws_sessions 每账号
> 一个目录；**API 域名收敛为进程级默认（沙箱），环境变量 `QQ_API_BASE` 覆盖**，
> `test_connection` 不再有切换域名的全局副作用；工作区无会话时入站回合自动新建
> （仅限回合路径，指令解析不建）；账号级「默认工作空间 + 锁定工作空间」（锁定后
> `/w` 拒绝执行）。完整设计与逐条依据见
> `workspace/qq-wechat-multi-account-design-2026-10-18.md`。本文其余章节描述 v1.0
> 单账号机制，凡与上冲突处以本增补为准。

---

## 一、定位与分层

Cili 的会话层是接口无关的（`core/session_registry.py` 持有 runner 池与运行权认领）。
本模块补上「接入端」这一层：Web 与 QQ 各是一个 `Interface` 实现，共享同一份会话。

```
interfaces/qq/             QQ 接入端（SDK 单例，宿主线程 + 宿主 loop）
  __init__.py              barrel：只导出 start_qq_interface / stop_qq_interface
  config.py                QQConfig + data/cili/setting.json 的 "qq" 段
  state.py                 owner + 当前工作区/会话（单用户，全局一份）
  runtime.py               QQRuntime：SDK 对象与共享状态的容器（避免循环导入）

  rawapi.py                适配层：stream_messages 裸调用（SDK 唯一缺口）
  gateway.py               SDK 接线：WSCallbacks 11 个回调 + 事件分发
  access.py                owner 记录（自动绑定 + 每次对话刷新）
  attachment.py            入站附件：下载 / 分类 / SSRF 校验
  inbound.py               消息 → 会话回合

  stream.py                流式状态机：前缀不可变 + 发散两分支 + 节流
  limiter.py               被动回复配额 + 主动消息降级
  sanitize.py              文本消毒（剥离 thinking / 脚手架）
  chunk.py                 markdown 感知切分（4000 上限、围栏闭合、表格整块）
  media.py                 出站上传：类型映射 + 路径校验（上传委托 SDK）
  sink.py                  QQSink：单流 transcript + 扇出全局事件流

  commands.py              斜杠指令（/h /w /s /new /status /stop；/help /workspace /session 为别名）
  service.py               宿主线程 + 宿主 loop；延迟 import SDK

core/interface.py          Interface ABC + Attachment
core/turn_dispatch.py      接口无关的回合驱动（认领后的「跑什么 / 怎么收尾」）
core/default_sink.py       default_sink 绑定（发布到全局事件总线）
core/outbox.py             出站文件队列（send_file 工具入队 → 接入端回合末出队）
core/tools/send_file.py    send_file 工具（QQ 专属，按回合门控）
```

### 1.1 `Interface` ABC 只有一个方法

```python
class Interface(ABC):
    name: str = "base"

    @abstractmethod
    def make_sink(self, attachment: Attachment, runner: SessionRunner) -> OutputSink: ...
```

`OutputSink` 已经**就是**渲染 seam，且已被 Web 验证过——Web 的
`make_sse_callbacks(queue, runner)` 与 QQ 的 `QQSink(...)` 是同一角色的两个实现。

设计草案里的 `ask_user(attachment, questions) -> str` / `request_approval(...) -> str`
被刻意放弃：两个实现都**无法在回调里同步返回答案**（Web 的答案经 HTTP POST 异步回来，
QQ 的答案是下一条消息）。把它们做成「返回答案」的方法是在说谎。交互提示的渲染本来
就在各自 sink 内部。

入站侧也不进 ABC：Web 走 HTTP 路由、QQ 走 SDK 回调，形态差太远；两者收敛到同一个
`core/turn_dispatch.py` 即可。

---

## 二、SDK 选型

用 **`qqbot-agent-sdk`**（官方 2026-05 在维护的新 Python SDK），不用 `qq-botpy`。

| | `qq-botpy`（2024-08 停更） | `qqbot-agent-sdk`（2026-05） |
|---|---|---|
| 分片上传 / 本地文件上传 | ❌（`post_c2c_file` 只接受 URL） | ✅ `MediaUploader` / `ChunkedUploader` |
| 附件下载管线 | ❌ | ✅ `AttachmentDownloader` / `AttachmentProcessor` |
| `msg_seq` 管理 | ❌ | ✅ `next_msg_seq()` |
| 超限错误类型化 | ❌ | ✅ `UploadFileTooLargeError` / `UploadDailyLimitExceededError` |
| WebSocket 重连 / Resume | 部分 | ✅ |
| 类型标注 / 单测 | ❌ | ✅ PEP 561 + mypy strict |
| **流式消息** | ❌ | ❌ **两个都没有** |

**SDK 的职责边界**：它是**传输层 SDK**——只做协议、鉴权、上传、事件解析、重连、去重。
**策略层必须自己做**：文本消毒、路径安全、配额、节流、流式状态机、审批，SDK 里一个都
没有（下面 2.1 逐条列出）。所以 `interfaces/qq/` 的规模由**策略层的需要**决定，而不是
由 SDK 的封装程度决定。

### 2.1 必须自己实现的 7 项

1. `sanitize.py` —— 文本消毒（SDK 完全没有）
2. `media.py` 的路径白名单校验（SDK 只 `(Path.cwd()/p).resolve()`）
3. `attachment.py` 的下载大小上限 + HTTPS 强制 + **SSRF 校验**（SDK 只验 scheme）
4. `chunk.py` —— markdown 感知切分
5. `stream.py` —— 流式状态机
6. `limiter.py` —— 被动回复配额 + 主动消息降级
7. `rawapi.py` —— `stream_messages` 裸调用

### 2.2 SDK 的两个坑

- **没有沙箱开关**：`API_BASE` 在 **import 时**从环境变量 `QQ_API_BASE` 取值。要用沙箱
  必须在 `import qqbot_agent_sdk` **之前**设置它——这是 `service.py` 不在顶层 import
  `gateway` 的原因。
- **`WSCallbacks` 有 11 个必填回调**（含 `get_session`/`set_session`/`fail_pending`），
  缺一个都构造不出来。传输线程归 SDK（`QQWebSocket` 自带），我们只提供宿主 loop。

---

## 三、线程模型

两个线程，只有一个是我们的：

| 线程 | 归属 | 职责 |
|---|---|---|
| **宿主线程** | 我们（`service.py`） | 跑宿主 loop：回合驱动、附件下载、出站上传 |
| **传输线程** | SDK（`QQWebSocket`） | 网络 / 心跳 / 重连；入站回调经 `run_coroutine_threadsafe` 派回宿主 loop |

- 阻塞的 `runner.run()` 用 `宿主loop.run_in_executor(None, run_fn)` 跑，不卡住宿主 loop
- `QQSink` 的同步回调把发送动作交回宿主 loop

---

## 四、QQ 官方 API 的限制（决定实现方式）

### 4.1 被动回复配额（最硬的约束）

| 场景 | 有效期 | **每条消息可回复次数** |
|---|---|---|
| **单聊（我们）** | **60 分钟** | **4 次** |
| 群聊 | 5 分钟 | 5 次 |

**消息去重**（原文）：相同 `msg_id` 可能多次推送；**相同的 `msg_id` + `msg_seq` 重复
发送会失败**，可递增 `msg_seq` 实现对同一消息的多次回复。

→ `msg_seq` 用 SDK 的 `next_msg_seq()`（**伪随机**，不是计数器）——API 只要求不重复。

### 4.2 能力矩阵

| 场景 | 发送端点 | 流式 |
|---|---|---|
| **QQ 单聊** | `POST /v2/users/{openid}/messages` | ✅ `POST /v2/users/{openid}/stream_messages` |
| QQ 群聊 | `POST /v2/groups/{group_openid}/messages` | ❌ 文档原文「群消息不支持流式参数」 |

**Markdown**：官方 2026/04/23 起，单聊自定义 Markdown「已开放到所有机器人均可使用，
无需单独申请模版」。唯一坑：markdown 里的图片必须是**公网可访问 URL**，所以 cili 产出的
本地图片不能内联，只能走 `msg_type=7` 富媒体。

**富媒体**：`file_type` = `1`图片 / `2`视频 / `3`语音 / `4`文件；软/硬限制
图片 20/200MB、视频 30/200MB、语音 20/200MB、文件 200/200MB。**没有删除已上传文件的
API**，靠 `file_info` 的 `ttl` 到期自动失效。消息撤回仅限发送后 **2 分钟**内。

**收费**：公开文档无任何计费说明。

---

## 五、输出：单条流式消息

### 5.1 为什么必须用流式

单聊每条用户消息只有 **4 次**被动回复配额，长回答用普通消息根本发不完。原生流式**整个流
只占 1 次配额**（同一个 message 被持续 replace）。

### 5.2 单流 transcript

整个回合只开**一条**流式消息，内容是只增不减的 transcript：

```
🔧 正在调用 read 工具…
🔧 正在调用 grep 工具…
根据检索结果，这个问题在于……

🔧 正在调用 edit 工具…
已修复，改动如下……
```

工具只显示「正在调用 xx 工具…」——不带入参、不带结果；连续同名工具去重。

> **为什么不给每个工具调用各发一条消息**：单聊每条用户消息只有 **4 次**被动回复配额。
> 一个回合若调了 3 个工具、每个工具各占 1 次，配额就见了底，**真正的答案反而发不出去**。
> 合并成一条流后，工具轨迹与答案共用 1 次配额，长回合也发得完——这是这个设计存在的
> 全部理由。

### 5.3 配额账

| 用途 | 条数 |
|---|---|
| 流式消息（工具轨迹 + 答案 + ask_user 提示折进尾部） | 1 |
| 出站文件（`send_file`） | N |
| 合计 | **1 + N ≤ 4 → N ≤ 3** |

`ask_user` / 审批提示**折进流尾部**而不是另发一条——流本来就是纯追加的，天然兼容，
省下 1 次配额。

**降级链**：
- 配额耗尽 → **降级为主动消息**（配额宽松得多：20/qpm、1000 条/用户/天），而不是丢弃
- 流从未成功发出任何分片 → **降级为静态普通消息**
- 任何发送失败只记日志，**绝不让 sink 抛异常打断 runner**

### 5.4 `replace` 语义的推论

`input_mode=replace` 要求 `content_raw` **须以上游已下发前缀 SentContent 开头**——流式
消息**只能追加、不能回退**。

推论：**「首片推 `⏳ 正在处理…` 占位、随后被正文替换」不成立**（占位符会永久留在消息
开头）。首片必须是最终内容的一个真前缀。

**前缀发散**（新文本不再以已下发内容为前缀）分两种：
- 更短 → 视为**新回复**：收尾当前流，另开一条
- 更长但发散 → **只追加发散出来的尾巴**，不回退已下发内容

比较前先**归一化空白**（模型重发时空白常有差异）。

> 这条直接解决了 runner 的 **413 去图重试**：它发 `RETRY_CLEAR_SENTINEL` 要求清空已流式
> 输出的文本，而 QQ 侧**无法清空**——只能走「新回复」那条路。

### 5.5 切分上限必须 ≤ 4000

SDK 的 `build_text_body` 会按 `MAX_MESSAGE_LENGTH = 4000` **静默截断**——它不报错，只是
把超长内容切掉。所以**我们自己的切分上限必须 ≤ 4000**：取更大的值（比如按观感取 5000）
会被 SDK 在每个分片上再切一刀，**内容直接丢失且毫无提示**。

切分要按 markdown 结构：**代码围栏跨片时闭合/重开**、**GFM 表格整块保留**（超限时按行切，
不劈行）。硬切的代价是可见的——切断代码围栏会让后续内容全渲染成代码块，切断表格会让整张
表渲染崩掉。

---

## 六、入站

1. **只处理单聊**（`chat_scope == "c2c"`），群聊一律忽略
2. **去重与重连不用自己写**：SDK 已内置（`msg_id` + 300s 窗口）
3. **owner 记录**：**自动绑定**，且**每次对话把 owner 刷新为当前发送者**并落盘——
   不需要配置，也不用去翻自己的 openid（openid 会因换设备/换号而变，自动刷新免维护）。
   ⚠️ 这意味着任何能给机器人发私聊的人都会成为 owner；安全性依赖 QQ 平台自身的限制
   （未认证机器人只有管理员能用）。若将来机器人公开使用，需重新加白名单。
   owner 只作**状态记录与日志**（`/status` 可查），不是访问闸门
4. **附件**：
   - 协议相对地址 `//host` → `https://host`（SDK 的 `resolved_url` 已归一化）
   - **只允许 HTTPS** + **SSRF 校验**（DNS 解析后逐个 IP 判定是否公网）——SDK 只验 scheme
   - 下载大小上限对齐 Web 的 `_MAX_UPLOAD_SIZE`（100MB）
   - 落盘到 **`{workspace}/uploads/`**
   - **图片** → multimodal block（与 Web 的图片上传路径一致）
   - **非图片** → 把路径写进文本；**语音** → 用 QQ 自带的 `asr_refer_text`
5. **命令** → `/h` `/w` `/s` `/new` `/status` `/stop`（`/help` `/workspace` `/session` 是等价别名）

---

## 七、出站文件：`send_file` 工具

agent 生成报告/图片后显式调用 `send_file(path, note="")`：校验（工作区内、realpath 防符号
链接、≤200MB）后入队 `core/outbox`，由接入端在回合末取走上传发送。

**为什么用工具而不是扫描目录**：语义明确、可控，不会误发中间产物。

**为什么按回合门控**：`send_file` 是 QQ 专属。但 runner 创建一次就缓存在 registry 里，
而会话在 Web/QQ 之间共享——同一个 runner 可能这回合被 Web 驱动、下回合被 QQ 驱动。所以
门控必须**按回合**（`runner.set_turn_interface(...)`），不能按 runner 创建时过滤。

三层挂点：
1. **工具 schema 层**（硬约束）：非当前接口的专属工具**既不在 active 也不在 deferred**——
   放进 deferred 会被 `tool_search` 激活，模型就能在 Web 上把它调起来。`_execute_tool`
   另加一道拒绝做纵深防御。
2. **system prompt 层**：`interface_caps` 块按当前接口生成「Response Surface」能力段
   （渲染面 / 消息上限 / 文件交付方式），事实由 `core/interface_profile.py` 从传输层
   常量生成，QQ 档案在 `interfaces/qq/profile.py`（`__init__` import 时注册）；接口未
   声明（cron 等无客户端回合）时不注入。`interface_hint` 块保留为接口专属散文的逃生口
   ——原两块 send_file 散文已并入档案 guidance，master.json 不再持有。
3. **skill 层**（本次未用）。

---

## 八、已知行为变更

- **Web 主界面拖放上传改为落到 `{workspace}/uploads/`**（原先落工作区根目录），插入输入框
  的路径随之变成 `uploads/<name>`。文件管理器面板内的拖放行为不变。
- **Web 暂不消费 outbox**：Web 回合里 `send_file` 不可见，所以不会产生待发文件；出队逻辑
  对 Web 是空操作。

## 九、未做（明确排除）

- 群聊（官方群聊不支持流式，且用户明确限定仅私聊）
- 多用户（用户明确限定仅单人）
- 消息按钮审批（QQ 的内联键盘体验更好，但要处理 `INTERACTION_CREATE` 回调、且按钮组件
  需要权限；先用编号文本把链路跑通，按钮留作后续体验优化）
- 入站语音转写（用 QQ 自带的 `asr_refer_text`，不接 STT）
- 出站图片内联进 markdown（官方要求公网 URL）
