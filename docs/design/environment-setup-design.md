# 环境与启动设计

> 按当前实现重写（2026-11-25）。代码：`main.py`、`start.ps1`、`start.cmd`、`core/port_guard.py`、
> `core/restart.py`、`core/updater.py`、`core/migrations/`。

## 一、启动入口

| 入口 | 说明 |
|---|---|
| `start.cmd` | Windows 双击入口，转调 `start.ps1` |
| `start.ps1` | 便携模式：检测/下载 Git Bash 与 Python 3.11 到 `data/deps/`，再启动 `main.py` |
| `python main.py` | 直接启动（需 Python 在 PATH） |
| `cli.cmd` | 终端客户端（`python -m clients.cli`），非服务端 |

`main()` 只做三阶段编排：

```
main()
  _isolate_user_site()          # 移除 user site-packages（只在 main 调用）
  args = parse_args()
  --apply-upgrade-only ? _run_upgrade_apply_mode() : 继续
  _prepare_environment()        # 阶段一
  _ensure_runtime()             # 阶段二
  _start_services(args)         # 阶段三
```

## 二、阶段一：环境与数据迁移（`_prepare_environment`）

1. Windows 下把 stdout/stderr 切到 UTF-8（`errors=replace`）。
2. `_setup_logging()`：`data/cili/logs/`（cili.log 全量 / error.log 仅 ERROR / access.log 访问），
   各 10MB 轮转保留 5 份。
3. `_migrate_projects_to_cili()`：旧 `data/projects/` → 各工作区 `.cili/`（须在目录创建前）。
4. `_setup_directories()` + `_migrate_legacy_cili_paths()` + `_init_settings()`：
   目录、system 工作区、`setting.json` 初始化（缺失时从示例生成）。
5. 旧数据迁移（全部幂等）：会话格式 `migrate_sessions_dir`；`tools/cron.d` 目录；
   接入端多账号（qq/wechat 段、state、微信凭证、QQ ws 会话文件）。
6. **合并升级迁移**（`core/migrations.run_pending`）：按 `data/cili/schema_version.json`
   顺序执行未完成步骤；首次执行前备份 `setting.json`/`workspaces.json` 到
   `data/cili/backup/migrations/{ts}/`。步骤失败上抛（配置类）或跳过告警（会话类，
   如不可解码文件）。

## 三、阶段二：运行时依赖（`_ensure_runtime`）

1. Git Bash：`data/deps/git/` 优先，其次 PATH/常见安装路径；缺失且无法下载时退出。
2. deps Python：`data/deps/python/`（embeddable 3.11 + pip）；健康检查与必要修复。
3. 依赖安装：读取 `requirements.txt`，按镜像顺序安装；新装后按需 `os.execv` 重启
   （带次数上限，防重启环）。名字齐全后再做两道自检：`pip check` 查依赖元数据
   不一致；导入探测在 deps Python 子进程里真实 import，查 dist-info 完好但模块文件
   损坏/被删的包。常规启动只探 fastapi/uvicorn/httpx/pydantic，异常修复时全量探测；
   失败项按镜像 `--force-reinstall --no-deps --no-cache-dir` 重装。
4. 解释器切换：直接 `python main.py` 且当前解释器缺关键依赖时，切到 deps Python
   运行服务（依赖实际安装的位置），避免启动到一半才抛 ModuleNotFoundError。
5. 启动异常修复：`main()` 捕获环境类启动异常（ImportError / ModuleNotFoundError /
   DLL 加载失败），设置 `CILI_ENV_REPAIR_COUNT` / `CILI_ENV_FORCE_REPAIR` 后
   `os.execv` 重启一次（干净进程）：新进程做全量导入探测并强制重装；非环境异常
   原样抛出。
6. 环境损坏交接：启动关键包（fastapi/uvicorn/httpx/pydantic）强制重装后仍不可
   导入时，打印「Python 环境损坏」与坏包清单。`start.ps1` 托管时以退出码 43
   （`core.restart.ENV_REBUILD_EXIT_CODE`）交回启动器：删除 `data/deps/python`、
   重装 Python 后重启（最多一次；删除发生在进程退出后，无文件锁问题）。直接
   `python main.py` 时打印「删除目录后重启」的指引并退出。等价于手工删除
   deps Python，但自动化且保留明确诊断。
7. 字体/浏览器探测：matplotlib 字体初始化失败降级为告警；浏览器路径缺失不阻塞。

## 四、阶段三：服务（`_start_services`）

1. `_prepare_web_binding(args)`：端口探测与绑定地址。
   - `core.port_guard.resolve_web_port`：cili 自身占用 → 停止旧实例复用；外部程序占用 → 顺延。
2. `_start_core_services()`：`core.app_context.startup()`（浏览器服务、AgentMailbox、
   cron 调度器、工作流、通道服务等单例；幂等）。
3. `_start_web_interface(args)`：uvicorn 启动 `web.web_api:app`（lifespan 再做
   `init_web_deps + app_context.startup + profile/oauth hooks`）。

## 五、重启与升级协议

- 重启由启动脚本托管：Web 手动重启/升级后重启通过 `core.restart.request_restart`
  写标记，主循环 `shutdown_and_restart_if_requested` 退出并以约定退出码（42）通知
  `start.ps1` 重新拉起；空闲重启用 `schedule_idle_restart`（所有会话空闲时执行）。
- 环境重建：服务以退出码 43（`ENV_REBUILD_EXIT_CODE`）退出时，`start.ps1` 删除
  `data/deps/python` 并重装后重启（每次启动最多一次）。
- 升级：`core/updater`（检查/下载/解压/合并文件树/回滚），文件树原语在
  `core/upgrade_archive`；`--apply-upgrade-only` 只应用升级包后退出。
- 升级后启动会自动执行 `core/migrations` 的未完成步骤。

## 六、便携模式（`start.ps1`）

检测顺序：`data/deps/` → 系统 PATH/常见安装路径 → 下载到 `data/deps/`。

- Git Bash：便携 7z 自解压包；
- Python 3.11：embeddable 包 + pip。

使用 deps Python 时 `main.py` 自动以其作为运行时（无需额外 venv）。

## 七、配置与数据位置

| 路径 | 内容 |
|---|---|
| `data/cili/setting.json` | 全局配置（providers/agents/system/permissions/images/qq/wechat/email/mcp_servers） |
| `data/cili/workspaces.json` | 工作区索引 |
| `data/cili/logs/` | 日志 |
| `data/cili/backup/migrations/` | 迁移前配置备份 |
| `data/cili/teen_mode.json` | 青少年模式状态 |
| `data/cili/tools/` | 工具状态（loop、email 游标、chatgpt 凭证等） |
| `{workspace}/.cili/` | 工作区数据（sessions/memory/cron/workflows/approval_rules.json） |

## 八、测试与环境隔离

- 测试不读取 `data/cili/`：conftest 迁移 `workspaces.json`/`workspace data`/`teen_mode`/
  email 游标到临时目录；CI 注入 `ANTHROPIC_API_KEY` 占位值。
- `main._isolate_user_site` 只在 `main()` 调用（测试 import main 不触发）。
