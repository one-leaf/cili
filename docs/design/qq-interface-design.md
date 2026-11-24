# QQ 接入端设计

> 按当前实现重写（2026-11-25）。代码：`interfaces/qq/`、`web/routes_qq.py`。

## 一、形态

- 官方 QQ 机器人（WebSocket 网关），**仅单聊（私聊）**，**多账号**。
- 配置段：`setting.json` 的 `qq.accounts`（列表）；凭据只由扫码绑定写入、由「断开连接」清除。
- 状态按「账号 → 聊天」两级分键（`interfaces/qq/state.py`，`data/cili/tools/qq/state-{account}.json`）。
- SDK：`qqbot-agent-sdk`（延迟导入，未启用/未安装不影响启动）。
- API 域名进程级：默认沙箱 `https://sandbox.api.sgroup.qq.com`，环境变量 `QQ_API_BASE` 覆盖
  （`interfaces/qq/sdk.py` 在导入 SDK 前 setdefault）。

## 二、模块

| 文件 | 职责 |
|---|---|
| `sdk.py` | API 域名、SDK 延迟导入与可用性查询 |
| `gateway.py` | SDK 接线：`WSCallbacks` 全量回调 + 事件分发 |
| `service.py` | 多账号启停（`start/stop/qq_interface_running`） |
| `runtime.py` | 单账号运行时（api/ws/uploader/loop） |
| `inbound.py` | 入站：过滤→认领会话→回合执行→交付 |
| `inbound_media.py` | 入站附件（下载/分类/落盘） |
| `sink.py` | **单条流式消息**输出（transcript） |
| `stream_controller.py` | 前缀不可变 + 节流的更新策略状态机 |
| `stream_api.py` | `stream_messages` 端点封装（SDK 缺口适配层） |
| `media.py` | 出站媒体上传 |
| `limiter.py` | 被动回复配额（每消息 4 次 / 60 分钟窗口） |
| `login.py` | 扫码绑定会话表（内存 TTL，`common.qr_login` 常量） |
| `commands.py` / `owner.py` / `profile.py` / `config.py` / `state.py` | 指令/归属/能力档案/配置/状态 |

## 三、输出：单条流式 transcript

整个回合只开**一条**流式消息（占 1 次被动回复配额）：

- 正文/思考并入 transcript；工具只显示「正在调用 xx 工具…」，连续同名去重；
  ask_user 提示折进流尾部（不另发消息）。
- `stream_controller`：内容只增不减（`40007 前缀不可扩展` 时收尾另开一条）；
  节流参数来自账号配置（`flush_interval_seconds` / `flush_max_wait_seconds`）。
- 降级：流一片都没发成功 → 按 markdown 结构切分后用 `send_text` 兜底（片数受剩余配额限制，
  超限显式标注截断）。
- 长文本上限 `MAX_CHUNK_CHARS`（SDK 按 4000 静默截断，切大了会丢内容）。

## 四、入站与回合

`inbound.py`：

1. 过滤：仅单聊、非自己、去重（`msg_id`，SDK 内置 300s 窗口）。
2. 指令（`/w /s /mode /new /status /stop /h`）由 `commands.py` 处理（共享 `common.commands`）。
3. 回合：`common.session_claim.prepare_and_claim` → `turn_dispatch` → 交付 outbox
   （`send_user_file` 经 `media.py` 上传；超软限制降级为文件卡片）。
4. 权限：审批卡/提问卡以文本形式下发并回映射（`common.ask_user.resolve_answer`）。

## 五、扫码登录

- `login.QQLoginStore`：内存会话（`SESSION_TTL_S=600`），SDK onboard 绑定流程；
  成功后写回 `setting.json` 账号凭据并热重启该账号宿主。
- Web：`GET /api/qq/status`；`POST /api/qq/accounts/{id}/login/start|cancel|logout`；
  `GET /api/qq/accounts/{id}/login/status`。

## 六、配置

`QQConfig` 白名单字段（未知键丢弃、类型兜底）：`id`/`enabled`/`app_id`/`secret`/
`msg_format`（markdown|text）/`session_mode`（latest|dedicated）/`session_locked`/
`flush_*` 节流参数。`app_id`/`secret` 留空 = 继承已保存值；掩码值视为保留。

## 七、安全

- 凭据永不出设置接口（`/api/qq/status` 不含 secret）。
- 出站媒体路径必须位于工作区（`interfaces/common/delivery_path.validate_media_path`）。
- 接口专属工具 `send_user_file` 只在 qq/web/wechat/email 回合可见。
