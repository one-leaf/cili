# 工具系统设计

> 按当前实现重写（2026-11-25）。以 `core/tools/` 代码与角色 JSON 为准。

## 一、结构

```
core/tools/
  registry.py        TOOL_REGISTRY（注册键 → 工厂）、TOOL_CLASSES、ToolBuildContext、create_tools
  base.py            Tool 基类（契约、路径解析、输出限制、权限入口）+ ShellMixin/BackgroundMixin
  shell_tool_base.py bash/pwsh 共享基类（分发、权限流水线、超时、后台管理）
  shell.py           shell 基础设施（管道、字符串剥离、临时目录）
  background.py      ShellTool 后台任务管理（Bash/Pwsh 共用）
  agent_background.py AgentTool 的后台子代理管理
  permission.py      权限占位协议（META_KEY / 卡片文案 / 规则 ID）
  textbook_base.py   教材工具公共基类（store、书名解析、候选提示）
  <tool>.py          单工具实现（read/write/edit/bash/…）
```

模块名与注册键一致（不再使用 `_tool` 后缀；`shell_command.py` 已改名 `shell_tool_base.py`）。

## 二、注册与实例化

- `TOOL_REGISTRY: dict[str, Factory]`：键即角色 JSON `tools` 中的注册键。
- `Factory = Callable[[ToolBuildContext], Tool]`；`ToolBuildContext` 携带
  `role_cfg/cwd/workspace_uuid/session/config/permission_store/permission_rules/permission_policy`。
- `_factory(cls, needs_config=, needs_permission=)` 只注入工具声明的依赖。
- `create_tools(...)` 按角色白名单实例化；`workflow` 工具在带 `metadata.workflow` 的会话中被跳过。
- `TOOL_CLASSES` 汇总全部工具类，`capability_tool_names(attr)` 按类属性聚合工具名。
- `get_tool_by_name(tools, name)` 线性查找（`core.tools` 保留重导出）。

当前注册键（36 个）：

`read` `write` `edit` `bash` `pwsh` `grep` `glob` `browser` `web_search`
`deepseek_web_search` `deepseek_read_url` `memory` `python` `todo_write` `latex`
`message_bus` `cron` `clock` `read_tool_result` `session_search` `temp` `loop`
`pdf2markdown` `office2pdf` `skill` `agent` `ask_user` `workflow` `tool_search`
`send_user_file` `textbook_find` `textbook_outline` `textbook_read` `textbook_search`
`textbook_original`

## 三、Tool 基类契约

### 3.1 声明

| 成员 | 含义 |
|---|---|
| `name` | 工具名（LLM 可见；与注册键一致） |
| `description` / `parameters` | 描述与 JSON Schema（`send_user_file` 的 description 按接口档案动态生成） |
| `concurrency_safe` | 纯读、无副作用、非流式 → 同批可并行 |
| `streams_output` | 输出流式写外部文件（bash/python/pdf2markdown），前端轮询 |
| `placeholder_output` | 输出内联、不落外部文件（ask_user/agent） |
| `mutates_workspace` | 可能改工作区文件 → 回合末广播 `files_changed` |
| `interface_scoped` | 接口专属工具（`send_user_file`：web/qq/wechat/email） |
| `MAX_TOOL_RESULT_SIZE_CHARS` | 单结果字符上限（`core.tool_limits`） |

能力集合由类属性声明，registry 汇总；runner/executor/default_sink 不再维护硬编码名表
（读取时取 `type(tool)` 属性，测试替身不会伪装真值）。

### 3.2 构造

`Tool.__init__(cwd, workspace_uuid, session, permission_store, permission_rules, permission_policy, **kwargs)`。
子类重写 `__init__` 必须声明所需参数并透传 `**kwargs`（registry 会注入 config/permission_*）。
同一工具实例带执行锁，避免并行批中 `output_file/on_output` 互覆。

### 3.3 接口档案钩子

`set_interface_profile(profile)` 统一保存到 `Tool.interface_profile`；
`send_user_file` 按档案生成描述与校验（条数 `max_files_per_turn`、体积 `file_size_limit_mb`）。

### 3.4 路径与输出

- `_resolve_path(path)`：`normalize_bash_path` → 相对 cwd 展开 → `realpath`；越界判定由 `PathPolicy` 负责。
- 输出上限与截断统一在 `core/tool_limits` / `core.compression`。
- 大输出外置到会话目录，`read_tool_result` 读取；流式工具实时写 `{tool_use_id}.txt`。

### 3.5 权限流水线

工具把目标路径/命令收敛为 `PathTarget`（`core/security/path_target.py`），由
`PermissionResolver` 统一裁决（规则 `deny > ask > allow`，AI 判定 `slow`）。
命令静态证据在 `ShellToolBase.collect_command_findings`；判定器为 `PermissionJudge`（`lite` 模型）。
占位结果通过 `core/tools/permission.META_KEY` 返回，审批卡与 ask_user 共用
`core/approval_cards` 管道，Watchdog 负责超时结算（Web/CLI 可自动恢复）。

## 四、角色白名单

| 角色 | mode | approval | 工具 |
|---|---|---|---|
| master | interactive | ✓ | 35 个：全部注册键（不含 workflow_agent） |
| worker | autonomous | — | 30 个：读写/命令/浏览器/搜索/记忆/python/skill/agent/教材… |
| lite | autonomous | — | 7 个：read/write/edit/bash/python/message_bus/clock |
| workflow（监督者） | interactive | — | 5 个：read/grep/glob/todo_write/clock；`workflow_agent` 动态注入 |

`deferred_tools`（master/worker）：browser、todo_write、latex、message_bus、temp、loop、pdf2markdown
（worker 无 cron）。deferred 工具默认不出现在工具列表，由 `tool_search` 激活。

## 五、工具清单（按职责）

| 类别 | 工具 | 要点 |
|---|---|---|
| 文件读 | read、read_tool_result | 文本/图片/PDF；SVG 按文本读源码；图片归一化在 `core.image_read` |
| 文件写 | write、edit | 内容上限、截断拒绝；工作流定义守卫 |
| 搜索 | grep、glob、session_search、tool_search | 忽略目录列表；tool_search 激活 deferred |
| 命令 | bash、pwsh | Git Bash / PowerShell；共享 `ShellToolBase`；后台任务/超时/权限证据 |
| 代码 | python | 脚本运行、后台、pip 镜像；目标提取 `core/security/python_targets` |
| 浏览器 | browser、web_search | 委托 `core.browser_service` 单例（Playwright + CDP） |
| 联网（服务端） | deepseek_web_search、deepseek_read_url | 按模型门控（`core.llm.deepseek_capabilities`） |
| 记忆 | memory | v3 记忆：store/search/skill/consolidate（`core.memory_*`） |
| 协作 | agent、message_bus、ask_user | 子代理委派（后台/轮次）、跨会话消息、提问卡 |
| 任务 | todo_write、loop、cron、clock | 任务表（整表替换）、周期循环、定时任务、时间 |
| 文档 | latex、office2pdf、pdf2markdown | LaTeX 编译、Office→PDF、MinerU OCR |
| 教材 | textbook_find/outline/read/search/original | 基于教材数据包；公共基类 `TextbookToolBase` |
| 交付 | send_user_file | 接口专属；校验后入 outbox，由接入端回合末交付 |
| 技能 | skill | 读取/列出技能（`core/skills_catalog`） |
| 工作流 | workflow（+ workflow_agent） | 定义/运行/状态；结果会话可重跑单步 |

## 六、执行流程（runner 侧）

1. 工具列表：`SessionRunner._eligible_tools()`（接口门控 + DeepSeek 服务端工具门控）。
2. 执行：`TurnExecutor` 负责占位/流式/落盘/截断与并行批（`concurrency_safe`）。
3. 事件：`OutputSink` 回调 → 请求级 SSE（`web/sse`）与全局事件流（`default_sink`）。
4. 回合末：`files_changed`（`mutates_workspace`）、outbox 交付（`send_user_file`）。

## 七、测试约定

- 每个工具模块对应 `test_<tool>.py`；跨工具边界在 `test_tools_boundary.py`。
- 伪造工具应提供真实存在的接口（如 `to_schema`），不要依赖 MagicMock 伪属性——
  能力判定读取类属性，替身不会伪造。
