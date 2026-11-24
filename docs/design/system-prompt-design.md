# System Prompt 设计

> 按当前实现重写（2026-11-25）。代码：`core/prompt_builder.py`、`core/prompt_sections.py`、
> `core/project_instructions.py`。

## 一、两层注入

| 层 | 载体 | 生成器 |
|---|---|---|
| system blocks | `role_cfg.system_prompt.blocks` 顺序拼装，返回字符串列表 | `SYSTEM_BLOCK_GENERATORS` |
| user 层 | 每次请求从磁盘/会话动态生成，不持久化到 messages | `USER_LAYER_GENERATORS` |

拼装结果按 `DYNAMIC_BOUNDARY` 分割：boundary 之前为**静态区**（跨会话共享，
Anthropic 侧 `cache_control scope=global`），之后为**动态区**（会话特定，不全局缓存）。

## 二、System blocks

| block | 生成器 | 内容 |
|---|---|---|
| `text` | `_gen_text` | 角色基础提示词（role JSON `system_prompt.text`） |
| `tools` | `_gen_tools` | 工具列表段（按当前可见工具生成） |
| `skills` | `_gen_skills` | 可用技能清单（按角色与接口过滤，预算 8000 字符） |
| `context` | `_gen_context` | 环境上下文：工作区/目录/时间/平台等 |
| `interface_hint` | `_gen_interface_hint` | 接口专属散文（仅当前驱动接口命中时注入） |
| `interface_caps` | `_gen_interface_caps` | 接口能力段（渲染面/交付形态/约束） |
| `teen_mode` | `_gen_teen_mode` | 青少年模式约束块（模式开启且策略允许时；全角色） |

- `teen_mode` 不依赖 role JSON，始终参与拼装；关闭时返回空字符串自动跳过。
- 工具描述本身不随青少年模式变化（避免污染缓存与提示词测试）。

## 三、User 层

`USER_LAYER_GENERATORS`：

| layer | 生成器 | 内容 |
|---|---|---|
| `project_instructions` | `_gen_project_instructions` | 工作区 AGENTS.md/agent.md/CLAUDE.md/claude.md（优先级顺序，`core/project_instructions`） |
| `workflow_panel` | `_gen_workflow_panel` | 工作流面板会话的步骤上下文 |

user 层在 `_get_messages_with_header` 中动态注入，**不写入 messages**；
连续的注入 user 消息与首条用户消息会被合并（`_merge_user_messages`），避免连续 user 角色。

## 四、分层职责

- `core/prompt_sections.py`：工具/技能/环境上下文等段落生成函数。
- `core/project_instructions.py`：项目指令文件查找与注入消息构造。
- `core/prompt_builder.py`：块注册表、顺序拼装、user 层注入与合并。
- `core/interface_profile.py`：接口能力档案（`render_capability_section`）。

## 五、缓存配合

- 静态区放在 `DYNAMIC_BOUNDARY` 之前；动态区（环境/时间/接口）之后，避免每次变化打断前缀缓存。
- 工具定义顺序稳定（按角色白名单顺序实例化），保证 tools 缓存断点稳定。
- 详见 `prompt-cache-design.md`。
