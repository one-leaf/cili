# 文件交付设计（send_user_file + outbox）

> 按当前实现重写（2026-11-25）。代码：`core/tools/send_user_file.py`、`core/outbox.py`、
> `web/delivery.py`、各接入端 outbound/sink。

## 一、模型

`send_user_file` 只做**校验与登记**，不直接上传：

1. 工具把一批路径登记到 `core/outbox`（按 `workspace:session` 键）。
2. 驱动本回合的接入端在回合末取走并交付：
   - Web：`web.delivery.drain_web_files` → `file` 事件 + `file_card` 消息（下载卡片）。
   - QQ / 微信：各自 `_send_outbox_files`（富媒体上传；超软限制降级为文件卡片）。
   - 邮件：`send_user_file` 转为附件随回信发出。
   - CLI：不可见（终端形态决定），仍登记但不交付。
3. 交付后把回执写入会话（UI 可见、不进 LLM 上下文）。

## 二、约束来自接口档案

`InterfaceProfile`（`core/interface_profile.py`）：

| 字段 | 含义 |
|---|---|
| `can_send_file` | 是否允许交付 |
| `file_delivery` | `upload`（上传到聊天）/ `link`（transcript 下载链接） |
| `file_size_limit_mb` | 单文件体积上限 |
| `max_files_per_turn` | 每回合条数上限 |

工具 `description` 是 property：基础描述 + 按当前档案追加约束。
档案由 `SessionRunner.set_turn_interface` 推送到 `Tool.interface_profile`。

## 三、校验（整批）

按顺序：工作区边界（realpath 前缀，先于存在性，避免存在性 oracle）→ 存在性 →
必须是普通文件 → 体积上限。**任一件不合格整批拒绝**，错误文案列出全部问题。

登记阶段由 outbox 判定：

- **去重**：同会话已登记路径跳过（重复登记同一产物只交付一次，保留首次 note）。
- **容量**：超过 `max_files_per_turn` 或兜底上限时**整批拒绝**（`overflow`），不做截断——
  截断会让「已登记」回执与事实不符。
- 全部重复时不视为失败，返回「already queued」。

## 四、outbox

`core/outbox`：

- `enqueue_files(session_key, [(path, note)], limit=)` → `EnqueueResult(accepted, duplicates, overflow)`；
- `enqueue_file` 单件版；
- `drain_files(session_key)` 取走本回合文件；`pending_count` 查询；
- `build_delivery_notice` / `record_delivery_notice` 生成并落盘交付回执；
- `clear(session_key=None)` 供测试与停机清空。

## 五、Web 渲染（`web/delivery.py`）

`build_web_file_card(runner, f)` 返回 `(消息内容, file 事件, file_card meta)`：

- 路径统一为工作区相对路径，空格 URL 编码；
- 消息为 `📎 [name](link)`；meta `{"file_card": {name, path, note}}`；
- `drain_web_files(runner, publish_global)` 落 `file_card` 消息，可选发布全局 `file` 事件
  （后台/恢复回合由 `default_sink` 调用）。

## 六、接入端差异

| 接口 | 交付形态 | 备注 |
|---|---|---|
| web | 下载卡片（link） | 请求级 SSE 推 `file` 帧；刷新后靠 `file_card` 消息 |
| qq | 富媒体上传 | 受被动回复配额；超软限制（图片 20MB/视频 30MB）降级为文件卡片 |
| wechat | 媒体上传 | context_token 预算与 defer 队列 |
| email | 附件 | 一回合只回一封纯文本邮件 |
| cli | 无 | 终端不可见 |

## 七、测试

- `test_send_user_file.py`：整批校验、档案约束、去重、容量溢出。
- `test_qq_outbound.py` / `test_wechat_outbound.py` / `test_email_*`：各接口交付。
- `test_web_*`：`file` 事件与 `file_card` 消息。
