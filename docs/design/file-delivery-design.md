# 文件交付设计：`send_user_file` 的多接口形态

> 状态：**已实现**（2026-10-19）。前置：`docs/design/qq-interface-design.md` §七、
> `docs/design/wechat-interface-design.md` §七、`docs/design/tui-interface-design.md` §五。
>
> 工具名从 `send_file` 改为 `send_user_file`（明确收件人是用户），模块
> `core/tools/send_user_file.py`。

## 一、问题

`send_file` 现在是「一个工具 + 一个 outbox + 各接口自取」的形态。三个接口各自
实现了取件与限额，但**约束散落、失败静默**：

| 缺陷 | 表现 |
|---|---|
| 条数限制对模型不可见 | 工具描述只讲体积与图片后缀，模型不知道一次能发几个；QQ/微信超限时**弹出即截断，余者丢弃** |
| 工具回执与事实不符 | 工具回「Queued for delivery」，模型据此认为成功；实际可能被交付层丢掉 |
| 队列上限与会话轮次无关 | `MAX_PENDING_FILES=8` 是跨轮累积的队列容量，不是「本轮能发几个」 |
| Web 描述自相矛盾 | Response Surface 里两条 `Files:` 行：一条说「用 send_file 交付」，一条说「按路径引用」 |
| 重复登记不去重 | 同一路径发两次 → 两张卡片、两次上传 |
| TUI 的 file 帧是死路径 | `can_send_file=False` → 队列永远为空 → `file_line` 渲染永不触发 |

## 二、设计原则

1. **交付通道决定约束，工具只管语义**。体积、条数、呈现形态、上传与否都是
   接口的事实，全部走 `InterfaceProfile`；工具层不写死任何阈值。
2. **登记即校验，交付不静默**。能在登记时判定的（条数、体积、类型）当场回绝
   并给自愈指引；只有交付层才能发现的（配额耗尽）必须显式告知用户。
3. **声明式交付**。工具只登记「这些产物要交付」，不搬运字节——字节由各接入端
   在回合末按自己的通道处理（上传 / 链接 / 仅路径）。
4. **模型契约完整**。模型要同时知道三件事：能发几个、发出去长什么样、
   **交付是自动的，不要在正文里再贴一遍**。

## 三、工具面

### 3.1 签名

```json
{
  "paths": ["charts/trend.png", "report.pdf"],   // 必填，1..N
  "note": "本季度分析产物"                        // 可选，整批共用
}
```

一次调用提交多个产物（调用一次 = 一次交付意图）。`note` 是整批说明，不再逐件。

### 3.2 校验顺序（登记时）

1. **工作区边界**（先于存在性——否则错误文案泄露工作区外文件是否存在）
2. 逐件存在性 / 是否普通文件
3. 逐件体积（按档案 `file_size_limit_mb`）
4. **条数**：本回合已登记数 + 本次件数 ≤ 档案 `max_files_per_turn`
5. 去重：与队列中已有路径重复的件跳过（保留最新 `note`）

任一件不合格 → **整批拒绝**，错误文案列出全部不合格项与原因。不做「部分接受」——
部分接受会让模型难以推断实际交付了什么。

### 3.3 返回文案

- 成功：`Queued 3 files for delivery: a.png, b.pdf, c.csv. They are delivered
  automatically after this turn — do not link or inline them in your reply.`
- 条数超限：`Error: this client delivers at most 3 files per turn; you asked for 5.
  Deliver 3 now and offer the rest in a later turn, or ask the user which to send.`

### 3.4 描述（模型面）

按档案生成，一段完整叙事而非并列断言：

```
Send files from the workspace to the user.
Use this when the user asked for files (reports, images, exports, archives)
and you have written them to disk. Delivery happens automatically after the
current turn — do not also link or inline them in your reply.
Constraints: files must exist inside the workspace; at most N per turn.
[形态行，按档案] [体积行，按档案]
```

## 四、接口矩阵

`InterfaceProfile` 新增字段 `max_files_per_turn: int | None`（None = 不限）。

| | web | qq | wechat | tui |
|---|---|---|---|---|
| `can_send_file` | ✓ | ✓ | ✓ | ✗ |
| `file_delivery` | `link` | `upload` | `upload` | `none` |
| `max_files_per_turn` | None | 3 | 3 | — |
| `file_size_limit_mb` | None | 200 | 200 | — |
| `inline_image_exts` | — | jpg/jpeg/png | 9 种图片后缀 | — |
| 超限行为 | 不适用 | 登记时拒绝 | 登记时拒绝 | — |
| 交付记录 | `file_card` 消息 | 无（用户可见 ⚠️） | 无（用户可见 ⚠️） | — |

**QQ 的 3**：流式消息占 1 次被动回复，单条用户消息共 4 次 → 1+N ≤ 4。
**微信的 3**：与正文/进度共享 8 条 token 预算，且进度可占用——留 1 条给正文。
**Web 不限**：本地下载链接，无通道成本。
**TUI 不给**：终端不内嵌图片、不能打开文件；模型在正文里给工作区相对路径就是
交付本身。`file` 帧渲染保留（未来若做终端图片协议），但当前不产生流量。

## 五、队列与交付

**保留 outbox**：QQ/微信的配额模型决定了交付必须在回合末（流式消息先占配额，
再按剩余配额发文件），这个交接点不能去掉。

调整：

1. **队列容量按接口**：`MAX_PENDING_FILES` 由档案的 `max_files_per_turn`
   推导（None → 保留一个宽松兜底，防模型绕开校验狂发）。
2. **溢出语义改为登记时拒绝**（§3.2 第 4 条），交付层不再截断。
3. **交付回执**：`drain` 后按结果落一条 UI 可见记录（成功 N 件 / 跳过 M 件 + 原因），
   与 `file_card` / `error_notice` 同机制（`_meta` 过滤，不进 LLM 上下文）。
   QQ/微信配额耗尽的 ⚠️ 文案保留（面向用户），回执补齐「哪几件没出去」。
4. **去重**：`enqueue_file` 对同会话已存在的路径跳过（不新增条目）。

## 六、各接口交付细节

### web（`link`）
- 每件推一个 `file` 帧 + 落一条 `file_card` 消息（`flush=True`，立即落盘）
- **多件分组**：同一回合 ≥3 件时前端折叠为一个「附件」块（避免刷屏），
  点击展开；1–2 件维持现状的逐条卡片
- 相对路径链接 → `renderMarkdown` 改写为 `/api/workspaces/{uuid}/files/...`

### qq（`upload`）
- 上传后按类型内嵌（png/jpg 图片）或文件卡片；超软限制降级为文件卡片（已实现）
- 配额不足：本回合剩余被动回复不足以发完 → 未发出的件**显式告知用户**，
  并在交付回执里记录

### wechat（`upload`）
- 与文本共享 token 预算；媒体项不进 defer 队列（保持现状）
- 预算不足 → 显式告知（已实现），回执补记

### tui（`none`）
- 模型在正文给工作区相对路径；`send_user_file` 不可见

## 七、Response Surface 修订

`render_capability_section` 的 `Files:` 行与档案 `guidance` 职责划清：

- **`Files:` 行（结构化）**：只讲交付通道——能不能发、发几个、多大、长什么样
- **`guidance`（散文）**：渲染面规则（哪些内容该内联、不要把图表栅格化成 PNG），
  外加一条**交付判据**——生成了用户该看到的图片/文件后，必须内联引用或调用
  `send_user_file`，只写描述不算交付（回归：画完花只写了文字，浏览器什么也没有）。

Web 档案的 guidance 删掉「Files: call `send_user_file`…」那半句，避免与结构化行
并列出现两条 Files 指令；交付判据不占用 `Files:` 行首，不构成重复。

## 八、落地情况（2026-10-19 完成）

| 步骤 | 落点 |
|---|---|
| 档案字段 | `core/interface_profile.py` 新增 `max_files_per_turn`；能力段新增 `At most N files per turn.` |
| 工具 | `core/tools/send_user_file.py`（`paths[]` + `note`；逐件校验 → 整批拒绝 → 去重登记） |
| 队列 | `core/outbox.py`：`enqueue_files` 批量 + 去重 + 整批容量判定；`build_delivery_notice` / `record_delivery_notice` |
| 档案 | web 不限；qq/wechat 各 3（注释写明配额依据）；web guidance 去掉重复的 Files 指令 |
| 交付回执 | QQ/微信交付层发不完时落 `_meta.delivery_notice`；上下文过滤（`core/session.py`、`runtime/context.py`）已同步 |
| 前端 | `chat.js` 的 `renderMessages` 把连续 ≥3 条 `file_card` 折叠成一个附件块 |
| 测试 | 批量签名、整批拒绝、去重、条数上限、回执落盘与过滤、Web 能力段单一 Files 行 |
| 文档 | 三篇接入端设计文档的出站文件章节同步；CLAUDE.md 设计文档表补本行 |

**与设计稿的偏差**：

- `web/sse.py` 未加额外回执——`file_card` 消息本身就是记录（Web 不丢件）。
- 折叠只落在**持久化渲染路径**：回合末前端会重拉会话覆盖流式渲染，在 SSE 分支
  折叠会被冲掉，所以在 `renderMessages` 里按「连续 ≥3 条」分组。
- QQ/微信 media 层的 `send_file` / `send_file_message` 函数名保留——它们描述传输
  动作，不是模型可见的工具名。
- 折叠只做在 `renderMessages`：向上翻页的历史分页路径（`prependMessages`）仍逐条
  渲染文件卡片。历史里的文件卡片是少数场景，翻页加载时保持现状。

## 九、明确不做

- 不引入「字节级交付存储」（不拷贝、不上传副本到别处）——交付引用工作区里的原文件
- 不做跨回合的重试队列（发不出去就告诉用户与模型，不做后台补发）
- 不给 TUI 加文件交付（终端形态决定）
- 不做交付历史面板（`file_card` 消息已在会话里可回溯）
