# 新建工作区向导设计

> 按当前实现整理（2026-11-28）。代码：`core/workspace_wizard.py`、
> `core/templates/{subjects,code}/`、`web/routes_workspace_wizard.py`、`web/static`（向导页）。

## 一、定位

向导**不依赖模型**：项目大类、目录位置、`AGENTS.md` 骨架与初始目录全部由确定性代码生成。
一次可创建多个工作区（学习项目按科目拆分），任何单项失败不影响已成功项。

## 二、项目大类

| id | 名称 | 生成内容 |
|---|---|---|
| `study` | 学习项目 | 每个科目一个工作区：科目目录、错题本、学习资料、笔记、练习、教材；`AGENTS.md` 由学科模板渲染；共享一个学习组 |
| `office` | 日常办公 | 收件箱/文档/表格/演示/归档 + 办公约定 |
| `research` | 科研项目 | 文献/笔记/数据/代码/初稿/输出 + 文献库/研究计划模板 |
| `code` | 编程项目 | 按语言与项目类型生成目录、`.gitignore`、编码准则与语言/工程约束 |
| `blank` | 空白项目 | 仅基础工作区与最小 `AGENTS.md` |

位置：`cili`（`workspaces.root`）、`home`（用户目录）、`documents`（文档目录）、`custom`（自定义）。

## 三、学习项目

- 必填：孩子称呼、学段；可选年级/教材版本；至少一个科目；
- 名称 = `孩子-科目`（孩子称呼在前，便于同一孩子的多个工作区排序聚集；目录名不带年级，
  年级写在 `AGENTS.md`，跨学年延续）；
- 同一次创建共享学习组 `{id: g-xxxx, name: 孩子-学段}`，写入各工作区索引
  `study_group_id/study_group_name`；
- 学科模板：`core/templates/subjects/{学段}-{学科}.md`，缺失回退 `通用.md`；
- 创建后可自动启动教材下载任务（见 `textbook-design.md`）：先匹配官方书目并复制已生成的
  Markdown 到 `教材/`，再下载源文件；教材失败只加 warning，不回滚工作区。

## 四、编程项目

- 语言：Python / TypeScript / Java / C# / Go / Rust / C/C++ / PHP / Kotlin / Swift / 其他；
- 项目类型：Web 后端、Web 前端、全栈、CLI、桌面、移动、数据处理、库/SDK、游戏、其他；
- `CODE_SCAFFOLD_DIRS` 决定初始目录；`.gitignore` 按语言合并公共项（`.env`、系统文件等）；
- `AGENTS.md` 主体：项目概况 + 四条准则 + 语言约束 + 项目类型约束（模板在 `core/templates/code/`）。

## 五、预检与创建

`preview_workspaces(...)`（不写文件）与 `create_workspaces(...)` 共用同一校验：

- 类别合法；名称通过 `validate_workspace_name`；同名工作区拒绝；
- 目录位置解析 + 冲突校验：目录已含 `.cili` 拒绝；非空目录需 `allow_existing`；
- 目录相互嵌套/包含 cili 项目目录/系统数据目录等 `validate_workspace_directory` 返回 warnings；
- 预览返回每个工作区的 `AGENTS.md`、初始目录/文件清单、warnings；学习项目返回 group。

创建流程：

1. 建目录、写 `AGENTS.md`（可选覆盖）与初始文件（已存在不覆盖）；
2. 生成 8 位 uuid，写工作区索引（名称、目录、slug、类别、科目、profile、
   `git_enabled/memory_enabled`、study group）；索引写失败回滚条目；
3. 建 `.cili/{sessions, config/approval_rules, config/sensitive_words/rules}`，写布局标记
   （`write_layout_marker`，跳过布局迁移）；
4. 可选 `git init`（`init_workspace_git`）；
5. 单项失败记入 `failed`；全部失败才抛错；返回 `{created, failed, warnings, group}`。

## 六、HTTP 接口（`web/routes_workspace_wizard.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/wizard/options` | 类别、位置、学段/年级、学科、语言/项目类型等选项 |
| POST | `/api/wizard/preview` | 预检（不写文件），返回工作区计划与 warnings |
| POST | `/api/wizard/create` | 创建（CSRF + `teen_guard("config_update")`）；学习项目可带 `download_textbooks` |

创建响应额外返回 `textbook_tasks`（教材下载任务 id/书数/字节数）与 warnings。

## 七、测试

`test/test_workspace_wizard.py`。

## 八、已知边界

- 向导只生成骨架，不生成具体学习计划/内容；
- 学习项目一次创建多个工作区时，每个科目独立目录但共享 group 标识；
- 非空目录必须显式允许写入；已含 `.cili` 的目录一律拒绝；
- `AGENTS.md` 默认不覆盖，需显式 `overwrite_agents`。
