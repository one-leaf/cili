# Browser 服务架构设计文档

本文档描述 Cili Agent 的全局浏览器服务（BrowserService）架构，以及 browser / web_search 两个工具如何委托给服务层进行浏览器操作。

---

## 一、功能概述

Cili Agent 提供两个浏览器相关工具：

| 工具 | 功能 |
|------|------|
| `browser` | 浏览器自动化：导航、截图、执行 JS、获取文本/链接、等待元素 |
| `web_search` | 通过 cn.bing.com 搜索，返回结构化结果 |

两者都依赖 Playwright + Chrome。为避免 Playwright 单实例冲突和 Chrome 进程管理混乱，引入 `BrowserService` 全局单例，集中管理所有浏览器资源。

**核心特性**：
- 全局唯一的 Playwright 实例（解决 Sync API 单实例限制）
- 全局唯一的 Chrome 进程（CDP 端口 9222）
- Chrome 延迟启动（不使用时不占用资源）
- **专用工作线程**：所有 Playwright 操作在专用线程执行，解决 greenlet 线程绑定问题
- **嵌套调用检测**：工作线程检测到嵌套调用时直接执行，避免死锁
- **Tab 池自动管理**：每次 navigate 开新 tab 并返回 tab_index，10 分钟无活动自动关闭
- **Chrome 锁文件清理**：启动前删除 SingletonLock/Socket/Cookie，防止 profile 锁定
- **增强错误诊断**：操作失败时收集 Playwright/Browser/Chrome/CDP 状态信息
- 线程安全（支持 Agent executor 和 Cron 后台线程并发使用）
- 工具层与服务层分离（工具只负责接口适配，不管理底层连接）

---

## 二、架构设计

### 2.1 组件关系

```
┌──────────────────────────────────────────────────────────────┐
│                        工具层                                │
│                                                              │
│  ┌──────────────┐          ┌──────────────┐                 │
│  │ BrowserTool  │          │ WebSearchTool│                 │
│  └──────┬───────┘          └──────┬───────┘                 │
│         │                         │                          │
│         │  get_service()          │  get_service()           │
│         ▼                         ▼                          │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              BrowserService  (全局单例)               │   │
│  │              core/browser_service.py                 │   │
│  │                                                      │   │
│  │  ┌────────────┐  ┌──────────────────────────────┐   │   │
│  │  │ Worker     │  │ Task Queue + Result Event    │   │   │
│  │  │ Thread     │──│ (解决 greenlet 线程绑定)     │   │   │
│  │  │ (专用线程) │  │ + Lazy Tab Cleanup (每轮检查)│   │   │
│  │  └────────────┘  └──────────────────────────────┘   │   │
│  │                                                      │   │
│  │  ┌────────────┐  ┌──────────┐  ┌──────────────────┐  │   │
│  │  │ Playwright │  │ Browser  │  │ Tab Pool         │  │   │
│  │  │ (唯一实例) │──│ (CDP)    │──│ {tab_idx: (page, │  │   │
│  │  └────────────┘  └──────────┘  │  timestamp)}     │  │   │
│  │                                 │ _active_tab_index │  │   │
│  │                                 │ 10min 自动回收   │  │   │
│  │                                 └──────────────────┘  │   │
│  │                                                      │   │
│  │  ┌──────────────────┐  ┌──────────────────────────┐  │   │
│  │  │ Chrome 进程      │  │ threading.Lock (线程安全) │  │   │
│  │  │ (全局唯一)       │  │ + 嵌套调用检测            │  │   │
│  │  └──────────────────┘  └──────────────────────────┘  │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 模块级 API

遵循 `CronScheduler` 的模块级单例模式：

```python
# 获取服务实例（不存在则自动创建并启动）
service = get_service() -> BrowserService

# 启动服务（main.py 调用）
start_browser_service() -> BrowserService

# 停止服务（web_api.py lifespan shutdown 调用）
stop_browser_service() -> None
```

---

## 三、生命周期

### 3.1 启动流程

```
web_api.py lifespan startup
  └─ get_service()                # 创建 BrowserService 实例
       └─ BrowserService()        # 仅创建实例，设置 _running = True
                                  # Playwright 不在此处启动！
                                  # Chrome 也不在此处启动！

首次工具调用（在 Agent 线程中执行）
  └─ service.navigate(url)
       └─ _ensure_connected()
            └─ _ensure_playwright()       # 首次调用时启动 Playwright
                 └─ sync_playwright().start()
                      # Chrome 不在此处启动！
```

**关键设计**：三层延迟初始化
1. **服务实例**：在 uvicorn lifespan 中创建（避免 asyncio 冲突）
2. **Playwright**：在首次工具调用时启动（工具在独立线程中执行，避免与主 event loop 冲突）
3. **Chrome 进程**：在首次连接时启动（如果用户不使用浏览器功能，不会浪费资源）

### 3.2 连接流程（懒初始化 Chrome）

```
service.navigate(url) 或其他操作
  └─ _ensure_connected()
       ├─ 尝试 connect_over_cdp("http://localhost:9222")
       │    └─ 成功 → 返回（复用已有 Chrome）
       │
       ├─ 连接失败，检查端口是否监听
       │    └─ 端口在监听 → 重试 3 次连接
       │         └─ 全部失败 → 杀死旧 Chrome，等待端口释放
       │
       └─ 端口未监听 → 启动新 Chrome
            ├─ _start_chrome(port=9222)
            │    ├─ 查找 Chrome 可执行文件
            │    ├─ 启动 Chrome 进程（带 --remote-debugging-port=9222）
            │    └─ 重试 CDP 连接，等待 Chrome 就绪
            │
            └─ connect_over_cdp()
                 └─ 成功 → 返回
```

### 3.3 Tab 池自动管理

**问题背景**：固定分配 tab（browser=tab1，web_search=tab2）存在以下问题：
- Tab 长期占用内存，即使不再使用
- 无法追踪 tab 的使用情况
- 大量操作后 tab 积累，浪费资源

**解决方案**：Tab 池 + 自动回收机制。每个 tab 分配唯一编号（tab_index），navigate/web_search 返回 tab_index 供后续操作使用。

```
Tab 池数据结构：
self._page_pool: dict[int, tuple]       # tab_index → (page, 最后活动时间戳)
self._next_tab_index: int               # 下一个可分配的 tab 编号（从 1 开始）
self._active_tab_index: int | None      # 最近使用的 tab_index

操作策略：
- navigate / web_search → _open_new_page() → 新 tab，加入池，返回 tab_index
- screenshot/execute/get_text/get_links/wait_for → 使用 tab_index 或 _active_tab_index
- switch_tab(tab_index) → 切换到指定 tab
- list_tabs() → 列出所有打开的 tab
- close_tab(tab_index) → 关闭指定 tab，释放资源
- _worker_loop 每次任务前检查：关闭 time.time() - ts > 600 的非活跃 tab

生命周期：
├── navigate("https://example.com")
│    └─ _open_new_page() → tab_index=1, page_A
│         └─ _page_pool = {1: (page_A, t1)}
│         └─ _active_tab_index = 1
│         └─ 返回 ToolResult(..., data={"tab_index": 1})
│
├── screenshot("shot.png", tab_index=1)
│    └─ _ensure_page(tab_index=1) → 复用 page_A
│         └─ _page_pool = {1: (page_A, t2)}  (时间戳更新)
│
├── navigate("https://other.com")  # 无 tab_index，开新 tab
│    └─ _open_new_page() → tab_index=2, page_B
│         └─ _page_pool = {1: (page_A, t2), 2: (page_B, t3)}
│         └─ _active_tab_index = 2
│
├── switch_tab(1)
│    └─ _touch_page(1, page_A)
│         └─ _active_tab_index = 1
│
├── close_tab(2)
│    └─ _close_page_internal(2, page_B)
│         └─ _page_pool 移除 key 2，page_B.close()
│
├── (10 分钟后) _worker_loop 触发 _cleanup_expired_tabs()
│    └─ tab 2 超过 10 分钟未活动 → page_B.close()
│         └─ _page_pool = {1: (page_A, t_last)}
│         └─ tab 1 是活跃 tab，不会被回收
```

**实现**：
```python
TAB_IDLE_TIMEOUT = 600  # 10 分钟

def _open_new_page(self) -> int:
    """创建新 tab，加入池并设为活跃 tab。返回 tab_index。"""
    if self._context is None:
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context()
    new_page = self._context.new_page()
    tab_index = self._next_tab_index
    self._next_tab_index += 1
    self._touch_page(tab_index, new_page)
    return tab_index

def _touch_page(self, tab_index: int, page) -> None:
    """更新 page 的最后活动时间，设为活跃 tab。"""
    self._page_pool[tab_index] = (page, time.time())
    self._active_tab_index = tab_index

def _cleanup_expired_tabs(self):
    """关闭超过 TAB_IDLE_TIMEOUT 无活动的非活跃 tab（在 worker 线程中执行）。"""
    now = time.time()
    tabs_to_close = [
        (idx, page) for idx, (page, ts) in self._page_pool.items()
        if now - ts > TAB_IDLE_TIMEOUT and idx != self._active_tab_index
    ]
    for tab_index, page in tabs_to_close:
        self._close_page_internal(tab_index, page)

def _close_page_internal(self, tab_index: int, page) -> None:
    """关闭单个 page 并从池中移除。"""
    self._page_pool.pop(tab_index, None)
    if self._active_tab_index == tab_index:
        self._active_tab_index = None
    try:
        if not page.is_closed():
            page.close()
    except Exception:
        pass
```

**Lazy cleanup 策略**：不需要额外的后台线程。`_worker_loop` 每次处理任务前调用 `_cleanup_expired_tabs()`，自然实现定期清理。活跃 tab（`_active_tab_index`）不会被自动关闭。

**_ensure_page() 逻辑**：
```python
def _ensure_page(self, tab_index: int | None = None) -> int:
    """确保有可用的 page，返回 tab_index。

    策略：
    - 如果指定了 tab_index，切换到该 tab（无效则报错）
    - 如果 _active_tab_index 有效，复用它
    - 否则创建新 tab
    """
    # 1. 指定了 tab_index → 检查有效性
    if tab_index is not None:
        page = self._get_page(tab_index)
        if page is None:
            raise ValueError(f"Tab {tab_index} not found or invalid")
        self._touch_page(tab_index, page)
        return tab_index
    # 2. active tab 有效 → 复用
    if self._active_tab_index is not None:
        page = self._get_page(self._active_tab_index)
        if page is not None:
            self._touch_page(self._active_tab_index, page)
            return self._active_tab_index
    # 3. 无有效 tab → 新建
    return self._open_new_page()
```

### 3.4 关闭流程

```
disconnect()                    # 断连接，保 Chrome（在 worker 中执行）
  └─ _disconnect_internal()
       ├─ 关闭 _page_pool 中所有 page
       ├─ 清空 _page_pool / _active_tab_index
       ├─ browser.close()
       └─ 重置 _context/_browser = None

stop()                          # 完整清理
  ├─ _run_in_worker(_cleanup)   # 在 worker 中执行
  │    └─ _cleanup()
  │         ├─ _disconnect_internal()     # 断连接（含关闭所有 tab）
  │         ├─ _playwright.stop()         # 停 Playwright
  │         ├─ _kill_chrome_internal()    # 杀 Chrome
  │         └─ _running = False
  │
  └─ 停止 worker 线程
       ├─ task_queue.put((None, ...))     # 发送退出信号
       └─ worker_thread.join(timeout=5)
```

---

## 四、线程安全与工作线程架构

### 4.1 并发场景

| 调用方 | 线程 |
|--------|------|
| Agent executor | 主线程池（每个 Agent 一个线程） |
| Cron scheduler | 后台调度线程 |
| Web UI | async 事件循环（通过 run_in_executor） |

### 4.2 工作线程模式（Worker Thread Pattern）

**问题背景**：Playwright Sync API 基于 greenlet 实现，会绑定到创建它的线程。当不同的工具调用发生在不同线程时，会出现 "Cannot switch to a different thread" 错误。

**解决方案**：引入专用工作线程，所有 Playwright 操作都在同一个线程中执行。

```python
class BrowserService:
    def __init__(self):
        self._lock = threading.Lock()
        self._worker_thread: threading.Thread | None = None
        self._task_queue: queue.Queue = queue.Queue()
        self._task_result: tuple[bool, Any] | None = None
        self._result_event = threading.Event()

    def _start_worker_thread(self) -> None:
        """启动专用工作线程。所有 Playwright 操作都在此线程执行。"""
        if self._worker_thread and self._worker_thread.is_alive():
            return

        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="BrowserService-Worker",
            daemon=True
        )
        self._worker_thread.start()

    def _worker_loop(self) -> None:
        """工作线程主循环，处理任务队列中的任务。

        每次执行任务前，自动清理超时的 tab（> TAB_IDLE_TIMEOUT 无活动）。
        """
        while True:
            try:
                task_func, task_args, task_kwargs = self._task_queue.get()
                if task_func is None:  # 退出信号
                    break

                # Lazy cleanup: close tabs idle for > TAB_IDLE_TIMEOUT
                try:
                    self._cleanup_expired_tabs()
                except Exception as e:
                    logger.debug(f"[BrowserService] Cleanup error (non-fatal): {e}")

                try:
                    result = task_func(*task_args, **task_kwargs)
                    self._task_result = (True, result)
                except Exception as e:
                    self._task_result = (False, e)
                finally:
                    self._result_event.set()
            except Exception as e:
                logger.error(f"[BrowserService] Worker loop error: {e}", exc_info=True)

    def _run_in_worker(self, func, *args, **kwargs):
        """在专用工作线程中执行函数。

        支持嵌套调用检测：如果已经在工作线程中，直接执行以避免死锁。
        """
        # 检查是否在工作线程内部调用（避免死锁）
        if threading.current_thread() == self._worker_thread:
            return func(*args, **kwargs)

        # 确保工作线程已启动
        self._start_worker_thread()

        # 提交任务并等待结果
        self._result_event.clear()
        self._task_result = None
        self._task_queue.put((func, args, kwargs))

        if not self._result_event.wait(timeout=60):
            raise TimeoutError("Task execution timeout (60s)")

        if self._task_result is None:
            raise RuntimeError("Task result is None")

        success, result = self._task_result
        if not success:
            raise result
        return result
```

### 4.3 嵌套调用检测

**问题背景**：工作线程可能导致死锁。例如：
```
_run_in_worker(navigate) → _execute_operation → _ensure_connected → _run_in_worker(_ensure_playwright)
                                                                    ↑ 死锁：等待工作线程完成，但已在工作线程中
```

**解决方案**：检测当前线程是否为工作线程，如果是则直接执行而不放入队列。

```python
def _run_in_worker(self, func, *args, **kwargs):
    # 关键检测：如果已经在工作线程中，直接执行
    if threading.current_thread() == self._worker_thread:
        return func(*args, **kwargs)  # 直接执行，不放入队列

    # ... 其他代码：放入队列等待
```

### 4.4 锁策略

```python
def stop(self) -> None:
    with self._lock:           # 生命周期方法持有锁
        self._run_in_worker(_cleanup)

def disconnect(self) -> None:
    with self._lock:           # 生命周期方法持有锁
        self._run_in_worker(_disconnect)

def navigate(self, url: str, tab_index: int | None = None) -> ToolResult:
    # 操作方法不持锁！线程安全由 worker 线程保证
    # navigate 默认创建新 tab，可指定 tab_index 复用
    return self._execute_operation("navigate(...)", _do_navigate, tab_index=tab_index)

def screenshot(self, path: str, tab_index: int | None = None) -> ToolResult:
    # 使用 tab_index 或 _active_tab_index
    return self._execute_operation("screenshot(...)", _do_screenshot, tab_index=tab_index)
```

**设计选择**：
- `self._lock` 仅用于生命周期方法（`stop`、`disconnect`），防止并发停止/断开
- 操作方法（`navigate`、`screenshot` 等）**不持有锁**，线程安全完全由 worker 线程串行化保证
- Worker 线程确保同一时刻只有一个 Playwright 操作在执行

---

## 五、工具层设计

### 5.1 BrowserTool（browser.py）

精简为 ~150 行，主要职责：
1. 提供 Tool 接口给 LLM 调用
2. 将 action 参数路由到 BrowserService 的对应方法
3. 传递 tab_index 参数实现多 tab 操作

```python
class BrowserTool(Tool):
    def execute(self, action, url=None, script=None, tab_index=None, ...):
        service = get_service()
        if not service.is_running():
            return ToolResult("Error: Browser service not available", error=True)

        if action == "navigate":
            return service.navigate(url, tab_index=tab_index)  # 开新 tab，返回 tab_index
        elif action == "screenshot":
            return service.screenshot(path, tab_index=tab_index)
        elif action == "save_pdf":
            return service.save_pdf(path, tab_index=tab_index)
        elif action == "execute":
            return service.execute_script(script, tab_index=tab_index)
        elif action == "get_text":
            return service.get_text(tab_index=tab_index)
        elif action == "get_links":
            return service.get_links(tab_index=tab_index)
        elif action == "wait_for":
            return service.wait_for(selector, timeout, tab_index=tab_index)
        elif action == "switch_tab":
            return service.switch_tab(tab_index)
        elif action == "list_tabs":
            return service.list_tabs()
        elif action == "close_tab":
            return service.close_tab(tab_index)
```

**close() 行为**：委托给 `service.disconnect()`，断开连接但保持 Chrome 运行。

### 5.2 WebSearchTool（web_search.py）

精简为 ~140 行，主要职责：
1. 构建搜索 URL（cn.bing.com/search?q=...）
2. 调用 service.navigate() 加载搜索页面（每次开新 tab，获取 tab_index）
3. 使用 tab_index 调用 service.wait_for() 和 execute_script()
4. 格式化输出，搜索完成后调用 service.close_tab() 释放资源

```python
class WebSearchTool(Tool):
    def execute(self, query, max_results=10):
        service = get_service()

        # 1. 导航到搜索结果页（每次搜索开新 tab）
        search_url = f"https://cn.bing.com/search?q={query}"
        nav_result = service.navigate(search_url)

        # 2. 获取 tab_index
        tab_index = nav_result.data.get("tab_index") if nav_result.data else None

        # 3. 等待结果加载（使用同一 tab）
        service.wait_for(".b_algo", timeout=15000, tab_index=tab_index)

        # 4. JS 提取结果（使用同一 tab）
        js_code = "..."
        result = service.execute_script(js_code, tab_index=tab_index)

        # 5. 格式化输出（含 tab_index）
        return ToolResult(formatted_output, data={"tab_index": tab_index})
```

**不再需要重试逻辑**：BrowserService 的 `_ensure_connected()` 内部已处理连接恢复。

**Tab 池 + tab_index 的好处**：
- 每次操作开新 tab，不干扰之前的页面状态
- navigate/web_search 返回 tab_index，后续操作可精确指定目标 tab
- 旧 tab 自动回收，不浪费内存
- 支持多 tab 并行操作（如：在 tab 1 登录，在 tab 2 查资料）

---

## 六、Chrome 进程管理

### 6.1 Chrome 启动

```python
def _find_browser(self) -> str | None:
    """返回配置中的浏览器路径（若存在且有效）。"""
    # 从 config 读取 browser_path（完整可执行文件路径）
    # 若路径存在则返回，否则返回 None
    ...

def _start_chrome(self, port: int) -> tuple[bool, str]:
    """启动 Chrome 进程（带远程调试端口）。
    Returns: (成功标志, 错误信息)
    """
    # 1. 检查已有进程
    if self._chrome_process and self._chrome_process.poll() is None:
        return True, ""

    # 2. 检查端口（可能被外部启动的 Chrome 占用）
    if self._is_port_listening(port):
        if self._try_cdp_connect():
            return True, ""
        else:
            self._kill_chrome_internal()  # 杀死 stale Chrome
            # 等待端口释放...

    # 3. 查找 Chrome 可执行文件
    chrome_path = self._find_browser()

    # 4. 删除 Chrome 锁文件（防止 profile 被锁定）
    self._remove_chrome_locks(user_data_dir)

    # 5. 启动新 Chrome
    cmd = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",             # 避免 GPU 相关问题
        "--disable-dev-shm-usage",   # 避免 /dev/shm 空间不足
    ]
    # Windows: 使用 STARTUPINFO 隐藏控制台窗口
    self._chrome_process = subprocess.Popen(cmd, ...)

    # 6. 等待 CDP 连接就绪（最多 20 秒）
    for _ in range(20):
        time.sleep(1)
        if self._try_cdp_connect():
            return True, ""
    return False, "Chrome CDP not ready after 20s"

def _remove_chrome_locks(self, profile_dir: str) -> None:
    """删除 Chrome 的 singleton 锁定文件，防止 profile 被锁定。

    Chrome 在运行时会创建以下锁文件：
    - SingletonLock: 防止多个实例使用同一 profile
    - SingletonSocket: Unix socket 锁
    - SingletonCookie: Cookie 文件锁
    """
    lock_files = ["SingletonLock", "SingletonSocket", "SingletonCookie"]
    for filename in lock_files:
        filepath = os.path.join(profile_dir, filename)
        if os.path.exists(filepath):
            os.remove(filepath)
```

### 6.2 Chrome Profile

使用 `data/deps/browser/` 作为共享的 Chrome user-data-dir：
- 所有工具共享同一个 profile
- 保持登录状态、cookies 等
- 避免每次启动都创建新 profile

### 6.3 Chrome 终止

```python
def _kill_chrome_internal(self) -> None:
    # 1. 终止已知进程（平台差异处理）
    if self._chrome_process:
        if sys.platform == "win32":
            # Windows: 使用 taskkill /T /F /PID 杀死进程树（含子进程）
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], ...)
            # 失败时 fallback: terminate() → wait(timeout=3) → kill()
        else:
            self._chrome_process.terminate()
            self._chrome_process.wait(timeout=5)
            # 超时则 kill()
        self._chrome_process = None

    # 2. 杀死使用相同 profile 的其他 Chrome 进程
    _kill_existing_chrome(self._chrome_profile_dir)

    # 3. 删除 Chrome 的 singleton 锁定文件
    self._remove_chrome_locks(self._chrome_profile_dir)

    # 4. 等待端口释放（最多 10 秒）
    for i in range(20):
        time.sleep(0.5)
        if not self._is_port_listening(DEFAULT_CDP_PORT):
            break
```

`_kill_existing_chrome()` 通过 PowerShell 查找使用特定 profile 的 Chrome 进程并终止，避免误杀用户自己的 Chrome。

---

## 七、错误处理与诊断

### 7.1 增强错误消息

**问题背景**：浏览器操作失败时，错误信息不够详细，难以定位问题根源。

**解决方案**：在 `_execute_operation()` 中捕获异常时，内联收集诊断信息并返回错误 `ToolResult`。

```python
def _execute_operation(self, operation_name: str, func, tab_index: int | None = None) -> ToolResult:
    """执行浏览器操作的通用包装。提供统一的错误处理和详细错误信息。"""
    def _do_operation():
        error = self._ensure_connected()
        if error:
            return error

        actual_tab_index = self._ensure_page(tab_index)
        page = self._get_page(actual_tab_index)
        if not page:
            return ToolResult(f"Browser {operation_name} failed: no valid page", error=True)

        self._apply_stealth()

        try:
            return func(page)
        except Exception as e:
            # 内联收集诊断信息
            page_info = f"url={page.url}, closed={page.is_closed()}"
            browser_info = f"connected={self._browser.is_connected()}"
            chrome_info = f"pid={self._chrome_process.pid}, alive={...}"
            port_listening = self._is_port_listening(DEFAULT_CDP_PORT)
            tab_pool_size = len(self._page_pool)

            return ToolResult(
                f"Browser {operation_name} failed.\n"
                f"Error type: {type(e).__name__}\n"
                f"Error message: {str(e)}\n\n"
                f"Diagnostics:\n"
                f"  Tab: {actual_tab_index}, Page: {page_info}\n"
                f"  Browser: {browser_info}\n"
                f"  Chrome: {chrome_info}\n"
                f"  Tab pool: {tab_pool_size} page(s)\n"
                f"  Port listening: {port_listening}\n\n"
                f"Suggestion: Try kill_chrome action to restart browser, then retry.",
                error=True,
            )

    try:
        return self._run_in_worker(_do_operation)
    except Exception as e:
        return ToolResult(f"Browser {operation_name} failed with worker error: {e}", error=True)
```

注意：诊断信息是在异常捕获处内联收集的（不使用单独的 `_collect_diagnostics()` 方法），错误通过返回 `ToolResult(error=True)` 处理（不抛出 `BrowserError` 异常）。

### 7.2 连接恢复策略

```
操作失败（如 "Execution context was destroyed"）
  └─ 下次操作时自动恢复
       └─ _ensure_connected()
            ├─ 尝试连接 → 失败
            ├─ 检查端口 → Chrome 在运行
            ├─ 重试 3 次 → 仍失败
            ├─ 杀死旧 Chrome
            ├─ 等待端口释放（2 秒）
            ├─ 删除 Chrome 锁文件
            └─ 启动新 Chrome → 成功
```

### 7.3 常见错误及处理

| 错误 | 原因 | 处理 |
|------|------|------|
| "Execution context was destroyed" | 页面导航导致 context 失效 | 下次操作自动重连 |
| "Target page, context or browser has been closed" | 连接被其他操作关闭 | `_ensure_connected()` 检测并重连 |
| "Cannot switch to a different thread" | Playwright greenlet 线程绑定 | 工作线程模式解决 |
| "Chrome started but CDP not ready" | Chrome 锁文件未清理 | `_remove_chrome_locks()` 解决 |
| "asyncio.run() cannot be called from a running event loop" | Playwright 在 async 上下文中启动 | 延迟初始化解决 |
| "Task execution timeout (60s)" | 工作线程死锁 | 嵌套调用检测解决 |
| "Connection closed (probably due to tab closure)" | CDP 连接无效 | `_connect_browser()` 先关闭旧 browser |

### 7.4 Stealth 反检测

每次操作前自动应用 `playwright-stealth`：

```python
def _apply_stealth(self) -> None:
    if not STEALTH_AVAILABLE:
        return
    page = self._get_page()
    if not page:
        return
    try:
        stealth_sync(page)
    except Exception:
        pass  # Stealth 失败不中断操作
```

这可以绕过常见的 bot 检测（如 navigator.webdriver 属性）。

---

## 八、文件结构

```
core/
├── browser_service.py              # 全局浏览器服务（~1000 行）
│   ├── BrowserService              # 服务类
│   │   ├── start() / stop()        # 生命周期
│   │   ├── disconnect()            # 断连接保 Chrome
│   │   ├── navigate()              # 导航（开新 tab，返回 tab_index）
│   │   ├── screenshot()            # 截图（支持 tab_index）
│   │   ├── save_pdf()              # 保存 PDF（支持 tab_index）
│   │   ├── execute_script()        # 执行 JS（支持 tab_index）
│   │   ├── wait_for()              # 等待元素（支持 tab_index）
│   │   ├── get_text()              # 获取文本（支持 tab_index）
│   │   ├── get_links()             # 获取链接（支持 tab_index）
│   │   ├── switch_tab()            # 切换到指定 tab
│   │   ├── list_tabs()             # 列出所有 tab
│   │   ├── close_tab()             # 关闭指定 tab
│   │   ├── get_current_tab_index() # 获取活跃 tab index
│   │   ├── kill_chrome()           # 终止 Chrome
│   │   ├── _run_in_worker()        # 工作线程任务分发
│   │   ├── _worker_loop()          # 工作线程主循环（含 lazy tab cleanup）
│   │   ├── _ensure_page()          # 获取 page（tab_index > active > 新建）
│   │   ├── _get_page()             # 按 tab_index 获取 page
│   │   ├── _open_new_page()        # 创建新 tab，返回 tab_index
│   │   ├── _touch_page()           # 更新 tab 活动时间
│   │   ├── _cleanup_expired_tabs() # 关闭 >10min 无活动的非活跃 tab
│   │   ├── _close_page_internal()  # 关闭单个 page
│   │   ├── _execute_operation()    # 操作通用包装（含错误诊断）
│   │   ├── _find_browser()         # 查找浏览器可执行文件
│   │   ├── _remove_chrome_locks()  # 删除 Chrome 锁文件
│   │   └── _connect_browser()      # CDP 连接管理
│   │
│   └── 模块级 API
│       ├── get_service()
│       ├── start_browser_service()
│       └── stop_browser_service()
│
└── tools/shared/
    ├── browser.py                  # 浏览器工具（~150 行）
    │   └── BrowserTool
    │       ├── execute()           # 委托给 BrowserService（含 tab_index）
    │       ├── close()             # disconnect()
    │       └── kill_chrome()       # kill_chrome()
    │
    └── web_search.py               # 搜索工具（~140 行）
        └── WebSearchTool
            └── execute()           # 委托给 BrowserService，返回 tab_index

main.py                             # 启动时调用 start_browser_service()
web/web_api.py                      # 关闭时调用 stop_browser_service()
```

---

## 九、依赖关系

```
BrowserService
  ├── playwright.sync_api           # Playwright Python API
  ├── playwright_stealth            # 反 bot 检测
  ├── core.tools.shared.base        # ToolResult
  └── core.config                   # PROJECT_ROOT, load_config (浏览器路径查找)

BrowserTool / WebSearchTool
  └── core.browser_service          # get_service()

main.py
  └── core.browser_service          # start_browser_service()

web/web_api.py
  └── core.browser_service          # stop_browser_service()
```

---

## 十、设计决策记录

### 10.1 为什么用全局单例而不是依赖注入？

Playwright Sync API 在同一进程内只允许一个实例。使用全局单例可以：
- 确保整个进程只有一个 Playwright 实例
- 避免线程间传递实例的复杂性
- 遵循 CronScheduler 的既有模式

### 10.2 为什么 Playwright 延迟初始化？

Playwright Sync API 内部通过 greenlet 创建自己的 event loop。如果在 uvicorn 的 async event loop 中调用 `sync_playwright().start()`，会导致 "asyncio.run() cannot be called from a running event loop" 错误。

解决方案：将 Playwright 的启动延迟到首次工具调用时。工具执行发生在独立的线程池线程中（不在主 event loop 中），因此可以安全地启动 Playwright。

**三层延迟初始化**：
1. 服务实例：在 uvicorn lifespan 中创建（仅创建对象，不启动 Playwright）
2. Playwright：在首次工具调用时启动（在工具线程中，避免 asyncio 冲突）
3. Chrome 进程：在首次连接时启动（如果用户不使用浏览器，不浪费资源）

### 10.3 为什么工具不持有连接状态？

之前 BrowserTool 和 WebSearchTool 各自持有 Playwright 实例，导致：
- 多实例冲突（"Playwright Sync API inside asyncio loop"）
- 连接状态不同步（一个工具关闭连接，另一个工具还在用）
- Chrome 进程泄漏（多个工具各自启动 Chrome）

将连接管理集中到服务层后，工具只需调用 API，无需关心底层状态。

### 10.4 为什么 disconnect() 不杀 Chrome？

Chrome 启动需要时间（约 2-3 秒）。保持 Chrome 运行可以：
- 下次操作直接复用，无需重启
- 保持 cookies 和登录状态
- 多个工具共享同一个 Chrome 实例

只有在服务关闭（`stop()`）时才终止 Chrome。

### 10.5 为什么用 Tab 池 + tab_index 而不是固定分配 Tab？

之前为每个工具分配固定 tab（browser=tab1，web_search=tab2），存在以下问题：
- Tab 长期占用内存，即使不再使用
- 无法追踪 tab 的使用情况
- 大量操作后 tab 积累，浪费资源

**Tab 池 + tab_index + 自动回收**：
- 每次 navigate/web_search 开新 tab，保证操作独立性
- `_page_pool: dict[int, tuple]` 以 tab_index 为 key，追踪所有 tab 的活动时间
- navigate/web_search 返回 tab_index，后续操作可精确指定目标 tab
- 10 分钟无活动的非活跃 tab 自动关闭，释放内存
- Lazy cleanup：在 `_worker_loop` 每轮检查，无需额外线程
- `_active_tab_index` 跟踪最近使用的 tab，不指定 tab_index 时自动复用

**多 tab 操作示例**：
```
# 场景：先登录网站 A，再搜索信息，最后回填到网站 A
navigate("https://site-a.com/login")  → tab_index=1
# ... 输入用户名密码登录 ...
web_search("需要查找的资料")          → tab_index=2
# ... 获得资料内容 ...
switch_tab(1)                          → 切回登录后的网站 A
execute_script("document.querySelector('#input').value='...'")
```

**为什么是 10 分钟**：Agent 的一次完整浏览器操作（navigate → 提取数据 → 后续处理）通常在 1-2 分钟内完成。10 分钟给了足够的缓冲，同时避免长时间空闲 tab 积累。

### 10.6 为什么需要工作线程？

Playwright Sync API 内部使用 greenlet 实现同步接口。greenlet 会绑定到创建它的线程：
- 在 thread A 创建 greenlet
- 在 thread B 调用 Playwright 方法
- 错误："Cannot switch to a different thread"

**解决方案**：所有 Playwright 操作都在专用工作线程中执行，确保 greenlet 始终在同一线程。

### 10.7 为什么需要嵌套调用检测？

工作线程模式可能导致死锁：
```
_run_in_worker(outer)
  └─ outer 内部调用 _run_in_worker(inner)
       └─ 等待工作线程完成
            └─ 但已在工作线程中，无法处理新任务
                 └─ 死锁
```

**解决方案**：检测当前线程是否为工作线程，如果是则直接执行而不放入队列。

```python
if threading.current_thread() == self._worker_thread:
    return func(*args, **kwargs)  # 直接执行，避免死锁
```

### 10.8 为什么需要删除 Chrome 锁文件？

Chrome 使用 profile 时会创建锁文件（SingletonLock, SingletonSocket, SingletonCookie）：
- 防止多个 Chrome 实例使用同一 profile
- 如果 Chrome 异常退出，锁文件可能残留
- 重启 Chrome 时检测到锁文件，会拒绝启动

**解决方案**：在启动 Chrome 前删除锁文件，确保可以正常启动。

### 10.9 为什么需要增强错误诊断信息？

浏览器操作涉及多个组件：
- Playwright 实例
- CDP 连接
- Chrome 进程
- Tab/Page 状态

当操作失败时，仅显示错误消息不足以定位问题。

**解决方案**：收集诊断信息，包括：
- Playwright 运行状态
- Browser 连接状态
- Chrome 进程状态（PID、退出码）
- CDP 端口监听状态

这些信息帮助快速定位问题根源。

---

**文档版本**: v2.3  
**创建时间**: 2026-08-25  
**更新时间**: 2026-08-30  
**状态**: 已实现（含工作线程、Tab 池自动回收、tab_index 管理、PDF 导出、Chrome 锁文件清理、错误诊断）
