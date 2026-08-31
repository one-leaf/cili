# Web API 设计文档

本文档描述 Cili Agent 的 Web 接口层，包括 REST API、SSE 流式通信和前端架构。

---

## 一、功能概述

Web 层提供基于 FastAPI 的 HTTP API 和 SSE 流式通信，前端使用原生 JavaScript（无框架）实现交互界面。

**核心特性**：
- **多工作区管理**：支持多个独立工作区，每个工作区有自己的会话和配置
- **SSE 流式响应**：实时推送 Agent 执行过程（文本、工具调用、思考过程）
- **LRU 淘汰**：内存中最多保留 20 个 RootAgent，自动清理最久未访问的
- **特殊命令**：/help、/status、/bash 在服务端处理，不经过 LLM
- **统一数据访问**：所有会话写入通过 SessionManager，不直接操作 JSON 文件

---

## 二、架构设计

### 2.1 组件关系

```
┌─────────────────────────────────────────────────────────────┐
│                     Web UI (Frontend)                       │
│              web/static/index.html + app.js                 │
└────────────────────┬────────────────────────────────────────┘
                     │ SSE / REST API
┌────────────────────▼────────────────────────────────────────┐
│                  FastAPI Backend                             │
│                    web/web_api.py                           │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Agents Dict  │  │ SSE Bridge   │  │ SessionManager   │  │
│  │ (LRU cache)  │  │ (Queue)      │  │ (Data layer)     │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────────────┘  │
│         │                 │                                  │
│  ┌──────▼─────────────────▼───────────────────────────────┐ │
│  │                  RootAgent                              │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │ │
│  │  │SessionManager│  │  LLM Client  │  │   Tools      │  │ │
│  │  └──────────────┘  └──────────────┘  └──────────────┘  │ │
│  └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 Agents Dict（LRU 缓存）

```python
# 全局变量
agents: dict[str, RootAgent] = {}          # key = "workspace_uuid:session_id"
_agent_access: dict[str, float] = {}       # key -> 最后访问时间戳
_MAX_AGENTS = 20                           # 最大缓存数量
_agents_lock = asyncio.Lock()              # 并发访问锁
```

**工作原理**：
- 每次访问会话时，更新 `_agent_access[key]` 为当前时间
- 创建新 Agent 前，如果超过 20 个，淘汰最久未访问的非运行中 Agent
- 被淘汰的 Agent 调用 `cleanup()` 释放资源（浏览器、HTTP 客户端）

```python
def _evict_idle_agent() -> None:
    if len(agents) <= _MAX_AGENTS:
        return
    idle_keys = [k for k in agents if not agents[k].is_running()]
    if not idle_keys:
        return
    oldest = min(idle_keys, key=lambda k: _agent_access.get(k, 0))
    evicted = agents.pop(oldest)
    _agent_access.pop(oldest, None)
    evicted.cleanup()
```

---

## 三、API 端点

### 3.1 健康检查

```
GET /api/health
```

**响应**：
```json
{
  "status": "ok",
  "active_agents": 5,
  "timestamp": "2026-08-25T10:30:00"
}
```

### 3.2 工作区管理

#### 列出工作区

```
GET /api/workspaces
```

**响应**：
```json
{
  "workspaces": [
    {
      "uuid": "a1b2c3d4",
      "name": "default",
      "directory": "/path/to/workspace",
      "created_at": "2026-08-25 10:00:00"
    }
  ]
}
```

#### 创建工作区

```
POST /api/workspaces
Content-Type: application/json

{
  "name": "my-project",
  "directory": "/path/to/project"  // 可选
}
```

**响应**：
```json
{
  "uuid": "e5f6g7h8",
  "name": "my-project",
  "directory": "/path/to/project"
}
```

#### 更新工作区

```
PUT /api/workspaces/{uuid}
Content-Type: application/json

{
  "name": "new-name",      // 可选
  "directory": "/new/path" // 可选
}
```

#### 删除工作区

```
DELETE /api/workspaces/{uuid}
```

#### 重置工作区

```
POST /api/workspaces/{uuid}/reset
```

删除工作区数据（sessions、config），但保留用户文件。

### 3.3 会话管理

#### 列出会话

```
GET /api/workspaces/{uuid}/sessions
```

**响应**：
```json
{
  "sessions": [
    {
      "session_id": "a1b2c3d4",
      "name": "Python 讨论",
      "created_at": "2026-08-25 10:00:00",
      "updated_at": "2026-08-25 10:30:00",
      "message_count": 25,
      "subagent_count": 2,
      "preview": "帮我写一个异步爬虫..."
    }
  ]
}
```

会话按 `updated_at` 倒序排列（最新的在前）。

#### 获取会话详情

```
GET /api/workspaces/{uuid}/sessions/{id}
```

**响应**：完整的会话数据（包括所有消息）。

#### 创建会话

```
POST /api/workspaces/{uuid}/sessions
Content-Type: application/json

{
  "name": "New Session"
}
```

#### 删除会话

```
DELETE /api/workspaces/{uuid}/sessions/{id}
```

#### 重命名会话

```
POST /api/workspaces/{uuid}/sessions/{id}/rename
Content-Type: application/json

{
  "name": "新名称"
}
```

### 3.4 消息发送（SSE 流式）

```
POST /api/workspaces/{uuid}/sessions/{id}/messages
Content-Type: application/json

{
  "content": "帮我写一个 hello world"
}
```

**响应**：SSE 流（`text/event-stream`）

```
data: {"type": "thinking", "content": "用户想要 hello world..."}

data: {"type": "text", "content": "好的，"}

data: {"type": "text", "content": "我来帮你"}

data: {"type": "tool_use", "tool": "write", "input": {"file_path": "hello.py", "content": "..."}}

data: {"type": "tool_result", "tool": "write", "content": "File written successfully", "is_error": false}

data: {"type": "text", "content": "已创建 hello.py"}

data: {"type": "done"}
```

**SSE 事件类型**：

| 类型 | 说明 | 字段 |
|------|------|------|
| `thinking` | 思考过程 | `content` |
| `text` | 文本输出 | `content` |
| `tool_use` | 工具调用 | `tool`, `input`, `tool_use_id` |
| `tool_result` | 工具结果 | `tool`, `content`, `is_error`, `tool_use_id` |
| `subagent_start` | SubAgent 启动 | `exec_id`, `task_summary` |
| `retry_clear` | 413 重试清除 | （无） |
| `error` | 错误 | `content` |
| `done` | 完成 | （无） |

**事件说明**：
- `tool_use_id`：工具调用的唯一 ID，前端可用于关联 tool_use 和 tool_result 事件。
- `retry_clear`：当 LLM 返回 413（请求体过大）触发自动重试时发送，前端需清除已流式输出的文本，防止用户看到重复内容。

### 3.5 Agent 控制

#### 检查运行状态

```
GET /api/workspaces/{uuid}/sessions/{id}/status
```

**响应**：
```json
{
  "running": true
}
```

#### 停止 Agent

```
POST /api/workspaces/{uuid}/sessions/{id}/stop
```

**响应**：
```json
{
  "success": true,
  "message": "已发送停止信号"
}
```

#### AskUser 交互流程

`ask_user` 工具采用 **退出 Agent 循环 + 前端渲染 + 用户回答作为新消息** 模式：

1. Agent 调用 `ask_user` → 工具返回 `wait_for_user=True` 的 ToolResult
2. Agent 循环检测到 `wait_for_user`，保存消息后退出
3. 前端通过 SSE 的 `tool_use` 事件（`tool: "ask_user"`）渲染交互式问题卡片
4. `tool_result` 事件对 `ask_user` 跳过（不渲染结果气泡）
5. 用户选择答案后，格式化答案作为普通聊天消息发送（POST /messages）
6. 后端在下次 LLM 调用前清理 ask_user 占位符（tool_use + placeholder tool_result）

**无需独立 API 端点**，答案通过常规聊天流程发送。

### 3.6 SubAgent 执行日志

#### 列出执行记录

```
GET /api/workspaces/{uuid}/sessions/{id}/executions
```

**响应**：
```json
{
  "executions": [
    {
      "exec_id": "exec_20260825_103000_ab12",
      "task": "优化爬虫性能...",
      "summary": "已完成优化",
      "metadata": {
        "started_at": "2026-08-25 10:30:00",
        "ended_at": "2026-08-25 10:32:00",
        "status": "completed",
        "iterations": 5
      }
    }
  ]
}
```

#### 获取完整执行日志

```
GET /api/workspaces/{uuid}/sessions/{id}/executions/{exec_id}
```

**响应**：完整的执行日志（包括所有消息）。

#### 删除执行日志

```
DELETE /api/workspaces/{uuid}/sessions/{id}/executions/{exec_id}
```

### 3.7 工具输出流式读取

#### 读取工具输出新增内容

```
GET /api/workspaces/{uuid}/sessions/{id}/stream/{tool_use_id}?offset=0
```

前端轮询此接口实时显示工具输出（特别是 bash 等长时间运行的工具）。

**参数**：
- `tool_use_id`：工具调用 ID（仅允许字母、数字、下划线、短横线）
- `offset`：从第几个字节开始读取（前端记录上次位置）

**响应**：
```json
{
  "content": "新的输出内容...",
  "offset": 1024,
  "exists": true
}
```

文件命名格式为 `{tool_use_id}.txt` 或 `{tool_use_id}_{tool_name}.txt`，接口通过 glob 匹配查找。同时在 session 目录和 `exec_*` 子目录中搜索（SubAgent 的输出在 exec_* 目录）。

### 3.8 全局配置

#### 获取配置

```
GET /api/config
```

**响应**：
```json
{
  "config": {
    "model": {
      "name": "claude-sonnet-4-6",
      "interface_type": "anthropic",
      "api_key_masked": "sk-a...xyz",
      "base_url": "https://api.anthropic.com",
      "max_tokens": 16384,
      "max_context_tokens": 256000,
      "multimodal": true,
      "temperature": 0.2
    },
    "llm_model": {
      "name": "claude-haiku-4-5",
      ...
    },
    "system": {
      "pip_mirror": "https://mirrors.aliyun.com/pypi/simple/",
      "bash_path": ""
    }
  },
  "config_path": "/path/to/setting.json"
}
```

API Key 自动脱敏显示（`sk-a...xyz` 格式）。

#### 更新配置

```
PUT /api/config
Content-Type: application/json

{
  "model": {
    "name": "claude-sonnet-4-6",
    "api_key": "sk-ant-..."
  },
  "llm_model": {
    "name": "claude-haiku-4-5"
  },
  "system": {
    "pip_mirror": "https://mirrors.aliyun.com/pypi/simple/"
  }
}
```

更新成功后，自动通知所有缓存的 RootAgent 调用 `reload_config()` 重新加载配置（使新的 API Key / Model 立即生效）。

#### 测试连接

```
POST /api/config/test
Content-Type: application/json

{
  "config": {
    "name": "claude-sonnet-4-6",
    "api_key": "sk-ant-...",
    "base_url": "https://api.anthropic.com"
  }
}
```

**响应**：
```json
{
  "success": true,
  "message": "Connection successful",
  "interface_type": "anthropic"
}
```

### 3.9 文件服务

#### 获取工作区文件

```
GET /api/workspaces/{uuid}/files/{path}
```

或（不指定 UUID，自动扫描所有工作区）：

```
GET /api/workspace/files/{path}?workspace_uuid={uuid}
```

用于在 Web UI 中显示图片等文件。

#### 浏览目录

```
GET /api/browse?path=/path/to/dir
```

**响应**：
```json
{
  "path": "/path/to/dir",
  "directories": [
    {"name": "subdir1", "path": "/path/to/dir/subdir1"},
    {"name": "subdir2", "path": "/path/to/dir/subdir2"}
  ],
  "parent": "/path/to"
}
```

#### 列出工作区文件

```
GET /api/files?workspace_uuid={uuid}&path=relative/path
```

**响应**：
```json
{
  "path": "relative/path",
  "items": [
    {"name": "subdir", "path": "relative/path/subdir", "is_file": false},
    {"name": "file.py", "path": "relative/path/file.py", "is_file": true}
  ],
  "parent": "relative"
}
```

---

## 四、特殊命令

以下命令在服务端直接处理，不经过 LLM：

### 4.1 /help

显示帮助信息：

```
/help
```

**响应**：
```
**特殊命令：**

- `/help` - 显示本帮助信息
- `/status` - 显示当前会话状态（上下文长度、用量等）
- `/bash <command>` - 直接执行 bash 命令（例如：`/bash ls -la`）

**工具使用：**
直接描述你要完成的任务即可，AI 会自动选择合适的工具。
```

### 4.2 /status

显示当前会话状态：

```
/status
```

**响应**：
```
**当前会话状态：**
- **模型：** claude-sonnet-4-6 (anthropic)
- **上下文长度：** ~15000 tokens
- **请求体大小：** 1.2 MB
- **API 调用次数：** 8
- **输入 tokens：** 15,000
- **输出 tokens：** 3,000
- **缓存读取：** 12,000 tokens
- **缓存创建：** 5,000 tokens
```

### 4.3 /bash

直接执行 bash 命令：

```
/bash ls -la
```

**响应**：工具调用和结果（不经过 LLM）。

---

## 五、SSE 流式机制

### 5.1 同步到异步桥接

RootAgent 的回调是同步的，但 FastAPI 的 SSE 是异步的。使用 `queue.Queue` 桥接：

```python
def send_message():
    event_queue: queue.Queue[str | None] = queue.Queue()
    
    # 同步回调
    def on_text(text: str) -> None:
        # 413 重试时，Agent 发送特殊标记，前端需清除已输出文本
        if text == "\x00RETRY_CLEAR\x00":
            event = json.dumps({"type": "retry_clear"})
            event_queue.put(f"data: {event}\n\n")
            return
        event = json.dumps({"type": "text", "content": text})
        event_queue.put(f"data: {event}\n\n")
    
    # 异步生成器
    async def generate():
        loop = asyncio.get_running_loop()
        
        # 在后台线程运行 Agent
        def run_agent():
            agent.run(user_input=..., on_text=on_text, ...)
            event_queue.put(None)  # 结束信号
        
        task = asyncio.ensure_future(loop.run_in_executor(None, run_agent))
        
        # 从队列读取事件
        while True:
            event = await asyncio.to_thread(event_queue.get, True, 0.5)
            if event is None:
                break
            yield event
    
    return StreamingResponse(generate(), media_type="text/event-stream")
```

### 5.2 客户端断开处理

客户端断开连接时，Agent 继续在后台运行：

```python
except asyncio.CancelledError:
    # 客户端断开，Agent 继续在后台运行
    logger.info("Client disconnected, agent continues running in background")
    return
```

用户需要显式点击"停止"按钮（调用 `/stop` 端点）才能中断 Agent。

---

## 六、前端架构

### 6.1 技术栈

- **原生 JavaScript**（无框架）
- **marked.js**：Markdown 渲染
- **MathJax**：数学公式渲染
- **highlight.js**：代码高亮

### 6.2 主要功能

#### 工作区管理

- 侧边栏显示工作区列表
- 创建、编辑、删除工作区
- 切换工作区

#### 会话管理

- 会话列表（按更新时间倒序）
- 创建、重命名、删除会话
- 导出会话为 HTML（含 Markdown + 数学渲染）
- 查看会话信息（token 统计）

#### 聊天界面

- 消息气泡（用户/助手）
- 思考块（可折叠："思考中..." / "思考完成"）
- 工具调用卡片（显示工具名、输入、输出）
- SubAgent 卡片（状态占位符，展开懒加载详情）
- 代码块（语法高亮）
- 图片显示（通过 `/api/workspace/files/` URL）

#### 设置弹窗

- RootAgent 模型配置
- LLM 模型配置（可选）
- 测试连接按钮
- 系统配置（pip 镜像、Git Bash 路径）

### 6.3 SSE 事件处理

```javascript
// app.js
async function sendMessage(content) {
    const response = await fetch(`/api/workspaces/${uuid}/sessions/${id}/messages`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({content})
    });
    
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    
    while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        
        const text = decoder.decode(value);
        const lines = text.split('\n');
        
        for (const line of lines) {
            if (line.startsWith('data: ')) {
                const event = JSON.parse(line.slice(6));
                handleEvent(event);
            }
        }
    }
}

function handleEvent(event) {
    switch (event.type) {
        case 'thinking':
            appendThinkingBlock(event.content);
            break;
        case 'text':
            appendText(event.content);
            break;
        case 'tool_use':
            appendToolCall(event.tool, event.input);
            break;
        case 'tool_result':
            appendToolResult(event.tool, event.content, event.is_error);
            break;
        case 'subagent_start':
            appendSubAgentCard(event.exec_id, event.task_summary);
            break;
        case 'retry_clear':
            // 413 重试：清除已流式输出的文本，防止重复内容
            clearAssistantOutput();
            break;
        case 'done':
            finishMessage();
            break;
    }
}
```

---

## 七、生命周期管理

### 7.1 启动流程

```
main.py
│
├─ _init_settings()          # 初始化全局配置
│   └─ 初始化配置（如需要）
│
├─ start_scheduler()         # 启动 Cron 调度器
│   └─ 加载 core/cron.d/*.json
│
└─ uvicorn.run(app)          # 启动 FastAPI

web_api.py（模块导入时）
│
├─ 创建目录结构               # WORKSPACE_DATA_DIR, WORKSPACE_DIR, CHROME_DIR
│
├─ _ensure_default_workspace()  # 创建默认工作区（模块级执行）
│
├─ _auto_init_global_config()   # 验证全局配置（模块级执行）
│
└─ lifespan 进入
    └─ get_service()         # 创建 BrowserService 实例（Playwright 延迟启动）
```

### 7.2 关闭流程

```
lifespan exit
│
├─ stop_browser_service()    # 停止浏览器服务（Playwright + Chrome）
│
├─ stop_scheduler()          # 停止 Cron 调度器
│
└─ 清理所有 RootAgent
    └─ for agent in agents.values():
        ├─ agent.stop()      # 发送停止信号
        └─ agent.cleanup()   # 释放资源（浏览器、HTTP 客户端）
```

---

## 八、安全考虑

### 8.1 IP 访问控制

中间件基于客户端 IP 进行访问控制（仅信任 `request.client.host`，不检查可伪造的 Host 头）：

```python
_LOCALHOST_IPS = {"127.0.0.1", "::1", "localhost"}

@app.middleware("http")
async def check_access_control(request: Request, call_next):
    config = load_config()
    client_ip = request.client.host if request.client else ""

    # 始终允许 localhost
    if client_ip in _LOCALHOST_IPS:
        return await call_next(request)

    # 检查白名单（system.allowed_ips 配置）
    allowed_ips = config.system.allowed_ips or []
    if client_ip in allowed_ips:
        return await call_next(request)

    # 拒绝访问
    return JSONResponse(status_code=403, content={"detail": "Access denied: IP not allowed"})
```

### 8.2 CORS 限制

默认只允许 localhost 访问：

```python
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    ...
)
```

### 8.3 路径遍历防护

文件服务端点检查路径是否在允许的工作区内：

```python
def _serve_workspace_file(workspace_dir: str, file_path: str) -> FileResponse:
    workspace_path = Path(workspace_dir).resolve()
    file_full_path = (workspace_path / file_path).resolve()
    
    # 安全检查：确保文件在工作区内
    if not str(file_full_path).startswith(str(workspace_path)):
        raise HTTPException(status_code=403, detail="Access denied")
    
    return FileResponse(str(file_full_path))
```

### 8.4 API Key 脱敏

配置 API 返回时自动脱敏 API Key：

```python
def _mask_single_model(model: dict) -> dict:
    m = model.copy()
    api_key = m.get("api_key", "")
    if api_key:
        m["api_key_masked"] = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 8 else "***"
    m.pop("api_key", None)
    return m

def _mask_api_key(config: dict) -> dict:
    result = config.copy()
    for key in ("model", "llm_model"):
        if key in result and isinstance(result[key], dict):
            result[key] = _mask_single_model(result[key])
    return result
```

---

## 九、设计决策

### 9.1 为什么用 SSE 而不是 WebSocket？

- **简单**：SSE 是单向流，适合 Agent 输出场景
- **HTTP 兼容**：不需要额外的协议升级
- **重连**：浏览器自动处理重连

### 9.2 为什么 Agents Dict 用 LRU 淘汰？

- **内存控制**：每个 RootAgent 占用内存（工具、浏览器实例）
- **性能**：避免创建过多 Agent 实例
- **简单**：基于时间戳的 LRU 算法简单可靠

### 9.3 为什么特殊命令不经过 LLM？

- **速度**：/help、/status 立即响应
- **成本**：不消耗 API token
- **可靠性**：即使 LLM 不可用也能工作

### 9.4 为什么客户端断开时 Agent 继续运行？

- **长任务**：避免浏览器刷新或网络波动中断长时间任务
- **显式控制**：用户需要显式点击"停止"才能中断
- **数据完整性**：确保任务完成或显式取消

---

## 十、相关文件

| 文件 | 职责 |
|------|------|
| `web/web_api.py` | FastAPI 后端，所有 API 端点 |
| `web/static/index.html` | 主页面 |
| `web/static/app.js` | 前端逻辑（SSE、Markdown、交互） |
| `web/static/style.css` | 样式 |
| `core/root_agent.py` | RootAgent（被 web_api 调用） |
| `core/session.py` | SessionManager（数据层） |

---

**文档版本**: v1.1  
**创建时间**: 2026-08-25  
**更新时间**: 2026-08-28  
**状态**: 已实现
