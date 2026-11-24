# 微信接入端设计（iLink 个人微信）

> 按当前实现重写（2026-11-25）。代码：`interfaces/wechat/`、`web/routes_wechat.py`。

## 一、形态

- 个人微信（iLink 协议，**无官方 SDK**，纯 httpx 实现）；**仅私聊**、**多账号**。
- 凭证每账号一文件 `data/cili/tools/wechat/accounts/{id}.json`（不进 setting.json）。
- 状态按「账号 → 聊天」两级分键（`interfaces/wechat/state.py`）。
- 配置段：`setting.json` 的 `wechat.accounts`（白名单字段）。

## 二、模块

| 文件 | 职责 |
|---|---|
| `protocol.py` / `client.py` | iLink 协议常量、请求头/消息构造、httpx 客户端 |
| `poller.py` | 长轮询（游标 `get_updates_buf`，失败退避，-14 登录失效上抛） |
| `inbound.py` | 入站过滤 → 认领会话 → 回合执行 → 交付 |
| `inbound_media.py` / `media.py` | 入站下载（AES-128-ECB 解密）与出站上传 |
| `outbound.py` | 发送：context_token 刷新、发送预算、defer 队列、进度提示 |
| `context_token.py` | context_token 台账（按用户：token、收到/刷新时间、已用条数） |
| `account.py` | 账号凭证（每账号一文件） |
| `login.py` / `qr` | 扫码登录会话（`SESSION_TTL_S=600`，二维码渲染） |
| `typng.py` / `sink.py` | 输入中提示 / 输出接收端 |
| `commands.py` / `owner.py` / `profile.py` / `config.py` / `state.py` / `service.py` / `runtime.py` | 指令/归属/能力档案/配置/状态/多账号启停/单账号运行时 |

## 三、发送预算

- 每个 `context_token` 约 **8 条**发送预算（`context_message_budget`），token 有效期 60 秒
  （`context_token_max_age_seconds`），过期或预算用尽时刷新 token。
- 服务端返回 `-2`（受限/配额用尽）→ 消息进入 **defer 队列**（文本，最多 3 条），
  等用户下一条消息带来新 token 后重放；媒体项不走 defer。
- `typing` 输入的节奏发送不占预算逻辑之外的额外消息类型（按协议支持）。
- `/status` 展示本轮剩余条数与待重发条数。

## 四、入站与回合

1. 长轮询：`get_updates` 返回消息列表与新游标；只放行「私聊 + 非自己 + 未见过」。
2. 认证失效（-14）：停止轮询并上抛，服务层提示重新扫码。
3. 指令与回合：与 QQ 共用 `common.commands`/`common.session_claim`/`turn_dispatch`。
4. `send_user_file`：出站上传媒体；受预算限制时按 defer/降级处理。

## 五、扫码登录

- `login.WechatLoginStore`：内存会话（600s TTL）；流程包含二维码、扫码确认、验证码。
- Web：`GET /api/wechat/status`；`POST /api/wechat/accounts/{id}/login/start|cancel|logout`；
  `GET /api/wechat/accounts/{id}/login/status`（登录成功后写凭证并热重启该账号）。

## 六、配置

`WechatConfig` 白名单字段：`id`/`enabled`/`session_mode`/`session_locked`/
`context_message_budget`/`context_token_max_age_seconds` 等；表单缺失字段继承已保存值。

## 七、安全

- 凭证文件不进设置接口；GET 状态不含明文凭证。
- 入站媒体落盘到工作区 `uploads/`（`core.media_fetch` 做 SSRF 校验与体积上限）。
- 出站路径必须位于工作区（`interfaces/common/delivery_path`）。
