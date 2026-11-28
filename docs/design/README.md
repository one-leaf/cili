# 设计文档索引

本目录的设计文档按**当前代码**整理；改代码时同步更新对应文档，新增子系统先补文档再实现。

## 运行时与模型

| 文档 | 内容 |
|---|---|
| [session-runner-design.md](session-runner-design.md) | SessionRunner/BaseSessionRunner 分层、角色分叉、回合执行、会话池、事件与持久化 |
| [system-prompt-design.md](system-prompt-design.md) | system prompt 块拼装、user 层注入、项目指令 |
| [compression-design.md](compression-design.md) | 三层消息压缩（Microcompact / Full Compact / Emergency） |
| [content-block-design.md](content-block-design.md) | LLM Adapter 分层、ContentBlock、StreamChunk |
| [prompt-cache-design.md](prompt-cache-design.md) | Prompt 缓存契约与缓存状态追踪 |
| [llm-api-reference.md](llm-api-reference.md) | 供应商协议、预设、OAuth、模型目录 |

## 会话与工作区

| 文档 | 内容 |
|---|---|
| [session-management-design.md](session-management-design.md) | 会话目录布局、消息过滤、usage、持久化 |
| [session-ownership-design.md](session-ownership-design.md) | 节点身份、会话归属、共享工作区协作与 fork |
| [workspace-layout-design.md](workspace-layout-design.md) | `.cili` 共享/runtime/cache/config/tmp 布局与迁移 |
| [workspace-migration-design.md](workspace-migration-design.md) | 单工作区换盘迁移、冻结、journal 恢复、`workspace/`→`workspaces/` |
| [workspace-wizard-design.md](workspace-wizard-design.md) | 新建工作区向导：类别、目录、AGENTS.md 骨架、学习组、教材下载 |
| [environment-setup-design.md](environment-setup-design.md) | 启动入口、便携依赖、端口/重启/升级、旧数据迁移 |

## 工具与能力

| 文档 | 内容 |
|---|---|
| [tool-system-design.md](tool-system-design.md) | 统一注册表、角色白名单、工具分类与执行流 |
| [todo-write-design.md](todo-write-design.md) | TodoWrite 整表替换与三态状态 |
| [browser-service-design.md](browser-service-design.md) | 浏览器单例、工作线程、Tab 池、快照 |
| [file-delivery-design.md](file-delivery-design.md) | send_user_file / outbox / 各接入端文件交付 |
| [security-permissions-design.md](security-permissions-design.md) | 权限配置、规则引擎、路径/命令/URL 判定、AI 判定、审批卡与看门狗 |
| [mcp-workspace-design.md](mcp-workspace-design.md) | 工作区 MCP 配置、懒连接、工具包装与执行 |
| [memory-system-design.md](memory-system-design.md) | 记忆条目、journal、提取/整合、索引 |
| [image-pipeline-design.md](image-pipeline-design.md) | 图片归一化、内联/引用、请求预算、持久化与 sidecar |
| [cron-scheduler-design.md](cron-scheduler-design.md) | Cron v2：任务模型、调度、执行、投递、种子 |
| [workflow-design.md](workflow-design.md) | 业务无关监督者 + worker/lite 执行器、产物契约、运行日志 |
| [textbook-design.md](textbook-design.md) | 教材数据包、同步、检索、5 个教材工具与官方下载 |
| [courseware-design.md](courseware-design.md) | 课件答题记录：JSONL 事件、错题本、成绩页、回传端点 |
| [office-pdf-design.md](office-pdf-design.md) | Office/WPS COM 转换、PDF 缓存、工作区同步与预览 |
| [deepseek-web-tools-design.md](deepseek-web-tools-design.md) | DeepSeek 服务端联网工具的客户端包装与能力降级 |

## 接入端与客户端

| 文档 | 内容 |
|---|---|
| [web-api-design.md](web-api-design.md) | FastAPI 装配、认证、端点总表、SSE、前端结构 |
| [email-interface-design.md](email-interface-design.md) | 邮件接入端：IMAP + SMTP、多账号、白名单与十道闸 |
| [qq-interface-design.md](qq-interface-design.md) | QQ 接入端：官方机器人、单条流式消息、被动回复配额 |
| [wechat-interface-design.md](wechat-interface-design.md) | 微信接入端：iLink 长轮询、多账号、进度节拍 |
| [cli-client-design.md](cli-client-design.md) | 终端客户端：进程外、HTTP+SSE、cwd 即工作区 |

## 平台能力

| 文档 | 内容 |
|---|---|
| [org-management-design.md](org-management-design.md) | 组织托管：短信登录、配置物化、共享工作区、退出恢复 |
| [teen-mode-design.md](teen-mode-design.md) | 青少年模式：密码/解锁、硬禁路径、策略、时长、审计 |
| [inbox-design.md](inbox-design.md) | 收件箱：持久通知、已读/回收站、来源与青少年过滤 |
