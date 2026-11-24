# 浏览器服务设计

> 按当前实现重写（2026-11-25）。代码：`core/browser_service.py`、`core/browser_chrome.py`、
> `core/browser_worker.py`、`core/browser_tabs.py`、`core/browser_snapshot.py`、
> `core/browser_url_policy.py`、`core/tools/browser.py`、`core/process_utils.py`。

## 一、组成

| 模块 | 职责 |
|---|---|
| `browser_service.BrowserService` | 全局单例门面：连接/生命周期/动作分发/状态编排 |
| `browser_chrome` | Chrome/Edge 进程：端口与 PID 探测、profile 校验、锁清理、杀进程树、启动命令与 CDP 探测 |
| `browser_worker.WorkerExecutor` | Playwright 同步 API 的专用工作线程：懒启动、任务槽位、超时回调、换代重建 |
| `browser_tabs.TabPool` | tab 页码分配/回收、活跃页、空闲过期判定 |
| `browser_snapshot` | ARIA snapshot 解析为 ref 表（`r1, r2, ...`） |
| `browser_url_policy` | 导航 URL 安全判定（硬禁协议/本机地址；可选允许非公网） |
| `process_utils` | 监听 PID 与进程命令行（CIM/netstat/lsof，与 port_guard 共用） |
| `tools/browser` | 工具层：参数校验、动作分发（委托服务单例） |

单例获取：`core.browser_service.get_service()`；应用启动时由 `core.app_context` 初始化。

## 二、连接模型

- Playwright `chromium.connect_over_cdp(http://localhost:{port})` 连接**独立 Chrome 进程**
  （用户数据目录 `data/deps/browser/`，与用户个人 Chrome 隔离）。
- 端口：默认 9222；被非本项目 Chrome/其他程序占用时切换到随机空闲端口（9300–9900，
  失败回退顺序扫描）。
- 已有本项目 Chrome：通过进程命令行校验 profile 后复用；外来进程绝不误杀。
- 关闭：`close()/stop()` 断开连接但**保留 Chrome 进程**；`reset()` 杀 Chrome 并作废连接，
  下一次动作自动重启（原生弹窗/渲染阻塞时的自愈入口）。

## 三、工作线程

- 所有 Playwright 同步调用都在单一线程（`WorkerExecutor`）执行；跨线程通过队列 + 独立结果槽位。
- 任务超时（默认 60s/按操作分级）触发恢复：杀 Chrome 释放卡住的 CDP 调用；线程仍未退出则
  换代重建 worker（旧线程 daemon 回收）。
- 每次执行任务前做空闲 tab 清理（`TAB_IDLE_TIMEOUT = 600s`，保留活跃页）。

## 四、标签页

`TabPool`：

- `allocate()`：优先复用已释放页码，否则递增；
- `touch(index, page, ts)`：记录活跃时间并设为活跃页；
- `expired(now, idle)`：非活跃且超时；
- `release(index)`：移出并回收页码；
- `clear(reset_indices=)`：连接作废/重启时清空。

## 五、动作集合

`tools/browser.py` 的 action 枚举（均带 `tab_index`/`frame` 参数）：

`navigate` `snapshot` `find` `click` `fill` `type` `press` `scroll` `hover` `drag`
`select` `upload` `get_text` `get_links` `wait_for` `execute_script` `screenshot`
`save_pdf` `extract` `extract_table` `console` `requests` `dialogs` `get_frames`
`switch_tab` `list_tabs` `close_tab` `get_cdp_port` `get_stealth_status` `reset`

交互流程：`snapshot/find` 产出 ref → `click/fill/type/press/scroll/hover/drag` 使用 ref。
`web_search` 工具同样委托本服务（导航到 cn.bing.com/search 并提取结果）。

## 六、安全

- 导航前 `check_navigate_url`：拒绝 `file:/data:/javascript:` 等危险协议与本机/内网地址
  （`hard_deny_reason` 不可绕过；`allow_non_public` 仅用于用户显式授权路径）。
- `upload`：路径必须位于工作区内（介质校验）。
- 浏览器上传/下载行为受青少年模式策略约束（`browser_upload: deny`）。
- 本机端点（如 `/api/open-in-explorer`）与浏览器无关，见 `web-api-design.md`。

## 七、前端/状态

- `BrowserService.status()` 提供 tab 池、CDP 端口、worker 状态（诊断用）。
- 会话切走不关闭浏览器；tab 按空闲时间回收。
