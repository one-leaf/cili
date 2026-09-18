// ── sse-client.js ── 全局 SSE 事件流客户端（worker 消息 + 工具输出，去前端轮询）
// 依赖 app.js 全局变量: currentWorkspace, currentSession, chatMessages
// 依赖 app.js 函数: renderMarkdown, escapeHtml, showToast
// 依赖 chat.js 全局变量: _toolStreamTimers（master 工具流气泡注册表）
// 事件流（GET /api/events）按 workspace_uuid/session_id 过滤，本文件按 exec_id 路由。

// ── 全局状态 ──
let _eventSource = null;          // 当前 EventSource
let _esWasDisconnected = false;   // 曾断线（onopen 时用于对齐）
const _agentCards = {};           // exec_id -> entry（worker 卡片状态机）

const _STATUS_ICONS = {
    'completed': '✅',
    'error': '❌',
    'failed': '❌',
    'timeout': '⏱️',
    'running': '🔄',
    'stopped': '⏹️'
};
// 折叠期事件缓冲上限：防止长 worker 折叠时无限增长
const _PENDING_MAX = 5000;

// ── 卡片状态机：entry 结构 ──
// {
//   execId, taskSummary, status,
//   card, header, detail, msgsDiv,
//   expanded, loadedOnce,
//   closedBlocks,          // 事件流已定稿的 block 数（不含 openBlock）
//   openBlock: null|{kind:'text'|'thinking', text, el},
//   pending: [],           // 折叠期缓冲事件（展开时重放）
//   needsFullReload,       // 缓冲溢出等，展开时全量重建
//   logBlocksRendered,     // 全量拉取已渲染的 block 数
//   toolStreams: {},       // tool_use_id -> {pre, div, offset, lastAppliedOffset}
// }
//
// 坐标系约定：事件流的 block（text/thinking/tool_use/tool_result）全部来自
// worker 的 assistant 侧；全量拉取 messages 里第一条是 user（task 描述）。
// 因此全量拉取的 "assistant 块序列" 与事件流 block 序列一一对应。
// _eventBlockCount = closedBlocks + (openBlock ? 1 : 0) —— 全量拉取据此跳过
// 事件流已呈现的 assistant 块，避免重复渲染。

function _eventBlockCountOf(entry) {
    return entry.closedBlocks + (entry.openBlock ? 1 : 0);
}

// ── 事件流生命周期 ──

// 连接到当前会话的事件流；切会话时由 app.js 先 close 再 connect
function connectEventSource() {
    closeEventSource();
    if (!currentWorkspace || !currentSession) return;
    const params = new URLSearchParams({
        workspace_uuid: currentWorkspace.uuid,
        session_id: currentSession.session_id,
    });
    const token = getAccessToken();
    if (token) params.set('token', token);

    const es = new EventSource(`/api/events?${params.toString()}`);
    es.onmessage = (ev) => {
        try {
            handleBusEvent(JSON.parse(ev.data));
        } catch (e) {
            console.error('解析事件流失败:', e);
        }
    };
    es.onopen = () => {
        // 首次连接不触发对齐；断线自动重连成功后补齐丢失区间
        if (_esWasDisconnected) {
            _esWasDisconnected = false;
            alignAfterReconnect();
        }
    };
    es.onerror = () => { _esWasDisconnected = true; };
    _eventSource = es;
}

function closeEventSource() {
    if (_eventSource) {
        _eventSource.close();
        _eventSource = null;
    }
    _esWasDisconnected = false;
}

// 重连对齐：running 卡片的展开内容增量补拉 + master 工具流按 offset 补拉
async function alignAfterReconnect() {
    for (const execId of Object.keys(_agentCards)) {
        const entry = _agentCards[execId];
        if (entry.expanded && entry.status === 'running') {
            await loadExecutionFull(entry, { fresh: false });
        }
    }
    await syncMasterToolStreams();
}

// ── 主路由 ──

function handleBusEvent(e) {
    if (!e || typeof e !== 'object') return;
    // 只处理当前会话的事件，防止切会话后旧流串入
    if (!currentSession || e.session_id !== currentSession.session_id) return;

    if (e.type === 'agent_start') {
        ensureAgentCard(e.exec_id, e.task_summary || '', { status: 'running' });
        return;
    }
    if (e.type === 'agent_complete') {
        markAgentComplete(e.exec_id, e.status || 'completed');
        return;
    }

    // worker 工具输出（无 exec_id）→ master 工具流
    if (e.type === 'tool_output' && !e.exec_id) {
        handleMasterToolOutput(e);
        return;
    }

    // goal 级状态文本（无 exec_id：完成/暂停/错误）→ 主聊天直渲；
    // goal 轮次事件带 exec_id=exec_*，走下方 worker 卡路径
    if (!e.exec_id) {
        if (e.type === 'text') {
            handleMasterBusEvent(e);
        }
        return;
    }

    const entry = _agentCards[e.exec_id];
    if (!entry) return;  // agent_start 先于一切 worker 事件，卡片未建则忽略

    // 折叠期：只缓冲，展开时一次性重放（避免展开前状态重复累积）
    if (!entry.expanded) {
        if (entry.pending.length < _PENDING_MAX) {
            entry.pending.push(e);
        } else {
            entry.needsFullReload = true;  // 缓冲溢出，展开时全量重建兜底
        }
        return;
    }

    switch (e.type) {
        case 'text': handleWorkerText(entry, e.content || ''); break;
        case 'thinking': handleWorkerThinking(entry, e.content || ''); break;
        case 'tool_use': handleWorkerToolUse(entry, e); break;
        case 'tool_result': handleWorkerToolResult(entry, e); break;
        case 'tool_output': handleWorkerToolOutput(entry, e); break;
        default: break;
    }
}

// ── worker 卡片创建 / 完成（幂等，POST SSE 与事件流共用）──

// 创建占位卡片（或返回已存在的 entry）。opts: {status, iterations, message_count}
function ensureAgentCard(execId, taskSummary, opts = {}) {
    if (!execId || !currentSession) return null;
    const existing = _agentCards[execId];
    if (existing) return existing;

    const task = taskSummary || '';
    const card = document.createElement('div');
    card.className = 'message assistant agent-card';
    card.dataset.execId = execId;

    const header = document.createElement('div');
    header.className = 'agent-header';

    const detail = document.createElement('div');
    detail.className = 'agent-detail';
    detail.style.display = 'none';

    card.appendChild(header);
    card.appendChild(detail);
    chatMessages.appendChild(card);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    const entry = {
        execId,
        taskSummary: task,
        status: opts.status || 'running',
        card, header, detail,
        msgsDiv: null,
        expanded: false,
        loadedOnce: false,
        closedBlocks: 0,
        openBlock: null,
        pending: [],
        needsFullReload: false,
        logBlocksRendered: 0,
        toolStreams: {},
    };
    _agentCards[execId] = entry;

    _renderHeader(entry, {
        iterations: opts.iterations || 0,
        message_count: opts.message_count || 0,
    });
    header.addEventListener('click', () => toggleAgentEntry(execId));
    return entry;
}

// 子代理完成：定稿 openBlock、更新状态、展开则全量重建消除流式合并差异
// 状态不降级：事件流带真实 status（error/timeout/failed），POST SSE 固定 completed；
// 两者到达顺序不定，用 rank 保证错误状态不被 completed 覆盖
async function markAgentComplete(execId, status) {
    const entry = _agentCards[execId];
    if (!entry) return;
    const nextStatus = status || 'completed';
    const rank = { 'completed': 0, 'stopped': 1, 'error': 2, 'failed': 2, 'timeout': 2 };
    const cur = rank[entry.status] !== undefined ? rank[entry.status] : 0;
    const next = rank[nextStatus] !== undefined ? rank[nextStatus] : 0;
    if (next > cur || entry.status === 'running') {
        entry.status = nextStatus;
    }
    finalizeOpenBlock(entry);
    _renderHeader(entry);
    if (entry.expanded) {
        await loadExecutionFull(entry, { fresh: true });
    }
    // 折叠时无需处理：展开时 fresh 全量重建（见 openAgentEntry）
}

// chat.js renderAgentRef 委托：按历史消息渲染静态卡并注册状态机
function agentCardForMessage(msg, msgId) {
    const entry = ensureAgentCard(msg.exec_id, msg.task_summary || msg.task || '', {
        status: msg.status || 'completed',
        iterations: msg.iterations || 0,
        message_count: msg.message_count || 0,
    });
    if (msgId) entry.card.dataset.msgId = msgId;
    return entry;
}

// 展开/折叠卡片（chat.js 与卡片头部点击共用）
async function toggleAgentEntry(execId) {
    const entry = _agentCards[execId];
    if (!entry) return;
    if (entry.expanded) {
        closeAgentEntry(entry);
    } else {
        await openAgentEntry(entry);
    }
}

function closeAgentEntry(entry) {
    entry.expanded = false;
    entry.detail.style.display = 'none';
    _updateHeaderToggle(entry);
    // 折叠不清状态/缓冲，只切 DOM 显隐；事件流折叠期改为缓冲（handleBusEvent）
}

async function openAgentEntry(entry) {
    if (entry.expanded) return;
    entry.expanded = true;
    entry.detail.style.display = 'block';
    _updateHeaderToggle(entry);

    // 已完成或缓冲溢出：全量重建最干净（消除流式 text 合并差异、补齐丢失）
    if (entry.needsFullReload || entry.status !== 'running') {
        await loadExecutionFull(entry, { fresh: true });
        entry.needsFullReload = false;
        return;
    }

    // 运行中：先建 openBlock 的 DOM（折叠期只累积文本未建 DOM），再重放缓冲
    if (entry.openBlock && !entry.openBlock.el) {
        const msgsDiv = ensureMsgsDiv(entry);
        const div = document.createElement('div');
        div.className = 'sa-msg assistant';
        div.innerHTML = renderMarkdown(entry.openBlock.text);
        msgsDiv.appendChild(div);
        entry.openBlock.el = div;
        _scrollChatIfNearBottom();
    }
    replayPending(entry);

    // 首次展开：全量拉取对齐（跳过事件流已渲染的 assistant 块）
    if (!entry.loadedOnce) {
        await loadExecutionFull(entry, { fresh: false });
    }
}

// 重放折叠期缓冲（按顺序重新分派，事件流已按序缓冲）
function replayPending(entry) {
    if (!entry.pending.length) return;
    const pending = entry.pending;
    entry.pending = [];
    pending.forEach(e => {
        switch (e.type) {
            case 'text': handleWorkerText(entry, e.content || ''); break;
            case 'thinking': handleWorkerThinking(entry, e.content || ''); break;
            case 'tool_use': handleWorkerToolUse(entry, e); break;
            case 'tool_result': handleWorkerToolResult(entry, e); break;
            case 'tool_output': handleWorkerToolOutput(entry, e); break;
            default: break;
        }
    });
}

// ── worker 事件增量渲染 ──

function handleWorkerText(entry, content) {
    if (entry.openBlock && entry.openBlock.kind === 'text') {
        entry.openBlock.text += content;
        if (entry.openBlock.el) {
            entry.openBlock.el.innerHTML = renderMarkdown(entry.openBlock.text);
        }
    } else {
        finalizeOpenBlock(entry);
        entry.openBlock = { kind: 'text', text: content, el: null };
        const msgsDiv = ensureMsgsDiv(entry);
        const div = document.createElement('div');
        div.className = 'sa-msg assistant';
        div.innerHTML = renderMarkdown(content);
        msgsDiv.appendChild(div);
        entry.openBlock.el = div;
    }
    _scrollChatIfNearBottom();
}

function handleWorkerThinking(entry, content) {
    if (entry.openBlock && entry.openBlock.kind === 'thinking') {
        entry.openBlock.text += content;
        if (entry.openBlock.el) {
            const tc = entry.openBlock.el.querySelector('.think-content');
            if (tc) tc.innerHTML = renderMarkdown(entry.openBlock.text);
        }
    } else {
        finalizeOpenBlock(entry);
        entry.openBlock = { kind: 'thinking', text: content, el: null };
        const msgsDiv = ensureMsgsDiv(entry);
        const div = document.createElement('div');
        div.className = 'sa-msg thinking';
        div.innerHTML = `<div class="think-content">${renderMarkdown(content)}</div>`;
        msgsDiv.appendChild(div);
        entry.openBlock.el = div;
    }
    _scrollChatIfNearBottom();
}

function handleWorkerToolUse(entry, e) {
    finalizeOpenBlock(entry);
    const msgsDiv = ensureMsgsDiv(entry);
    const div = renderToolCall(msgsDiv, e.tool, e.input);
    entry.closedBlocks++;
    // bash/python 的实时输出挂在该 tool_use 块下
    if (e.tool === 'bash' || e.tool === 'python') {
        entry.toolStreams[e.tool_use_id] = { pre: null, div, lastAppliedOffset: 0 };
    }
    _scrollChatIfNearBottom();
}

function handleWorkerToolResult(entry, e) {
    finalizeOpenBlock(entry);
    const msgsDiv = ensureMsgsDiv(entry);
    renderToolResult(msgsDiv, e.content, e.is_error);
    entry.closedBlocks++;
    delete entry.toolStreams[e.tool_use_id];
    _scrollChatIfNearBottom();
}

// worker 工具实时输出：按 offset 去重，append 到 tool_use 块下的 pre
function handleWorkerToolOutput(entry, e) {
    const ts = entry.toolStreams[e.tool_use_id];
    if (!ts) return;
    if (e.offset <= ts.lastAppliedOffset) return;
    let pre = ts.pre;
    if (!pre) {
        pre = document.createElement('pre');
        pre.className = 'sa-tool-output';
        if (ts.div) ts.div.appendChild(pre);
        ts.pre = pre;
    }
    pre.textContent += e.content;
    ts.lastAppliedOffset = e.offset;
    _scrollChatIfNearBottom();
}

// 定稿当前 openBlock（不计入 closedBlocks 前的块转为定稿计数）
function finalizeOpenBlock(entry) {
    if (entry.openBlock) {
        entry.openBlock = null;
        entry.closedBlocks++;
    }
}

// ── 全量拉取（index.json）──

// 与事件流坐标系对齐的增量渲染。
// opts.fresh: 清空重建（agent_complete、缓冲溢出、已完成卡展开）。
async function loadExecutionFull(entry, opts = {}) {
    const ws = currentWorkspace && currentWorkspace.uuid;
    const sid = currentSession && currentSession.session_id;
    if (!ws || !sid || !entry.execId) return;

    try {
        const resp = await fetch(`/api/workspaces/${ws}/sessions/${sid}/executions/${entry.execId}`);
        if (!resp.ok) return;
        const data = await resp.json();
        const meta = data.metadata || {};
        const blocks = normalizeContentToBlocks(data.messages || []);
        updateHeaderMeta(entry, meta);

        if (opts.fresh) {
            if (entry.msgsDiv) entry.msgsDiv.remove();
            entry.msgsDiv = null;
            entry.closedBlocks = 0;
            entry.openBlock = null;
            entry.pending = [];
            entry.needsFullReload = false;
            entry.loadedOnce = true;
            const msgsDiv = ensureMsgsDiv(entry);
            blocks.forEach(b => renderBlock(msgsDiv, b));
            entry.logBlocksRendered = blocks.length;
        } else if (entry.logBlocksRendered > 0) {
            // 已渲染过全量：只追加新增总块（精确，无重复）
            if (blocks.length > entry.logBlocksRendered) {
                const msgsDiv = ensureMsgsDiv(entry);
                blocks.slice(entry.logBlocksRendered).forEach(b => renderBlock(msgsDiv, b));
                entry.logBlocksRendered = blocks.length;
            }
        } else {
            // 首次全量拉取：user 块照常渲染，跳过事件流已呈现的 assistant 块
            const fromAssistant = _eventBlockCountOf(entry);
            const msgsDiv = ensureMsgsDiv(entry);
            let assistantSeen = 0;
            blocks.forEach(b => {
                if (b.role === 'user') {
                    renderBlock(msgsDiv, b);
                    return;
                }
                if (assistantSeen < fromAssistant) {
                    assistantSeen++;
                    return;
                }
                assistantSeen++;
                renderBlock(msgsDiv, b);
            });
            entry.logBlocksRendered = blocks.length;
        }
        entry.loadedOnce = true;
        _scrollChatIfNearBottom();
    } catch (err) {
        console.error('加载执行详情失败:', err);
    }
}

// 全量 messages -> 扁平 block 列表（保留 role，user 块用于 task 描述展示）
function normalizeContentToBlocks(messages) {
    const blocks = [];
    (messages || []).forEach(message => {
        const role = message.role || 'assistant';
        normalizeContent(message.content).forEach(block => {
            blocks.push(Object.assign({}, block, { role }));
        });
    });
    return blocks;
}

function renderBlock(msgsDiv, block) {
    if (block.kind === 'text' && block.text) {
        const div = document.createElement('div');
        div.className = `sa-msg ${block.role === 'user' ? 'user' : 'assistant'}`;
        div.innerHTML = renderMarkdown(block.text);
        msgsDiv.appendChild(div);
    } else if (block.kind === 'thinking' && block.text) {
        const div = document.createElement('div');
        div.className = 'sa-msg thinking';
        div.innerHTML = `<div class="think-content">${renderMarkdown(block.text)}</div>`;
        msgsDiv.appendChild(div);
    } else if (block.kind === 'tool_call') {
        renderToolCall(msgsDiv, block.name, block.input);
    } else if (block.kind === 'tool_result') {
        renderToolResult(msgsDiv, block.content, block.is_error);
    }
    // image 块在 worker 日志中罕见，忽略
}

function renderToolCall(msgsDiv, name, input) {
    const div = document.createElement('div');
    div.className = 'sa-msg tool-use';
    const inputStr = typeof input === 'string' ? input : JSON.stringify(input, null, 2);
    div.innerHTML = `<strong>[工具调用: ${escapeHtml(name)}]</strong><pre>${escapeHtml(inputStr)}</pre>`;
    msgsDiv.appendChild(div);
    return div;
}

function renderToolResult(msgsDiv, content, isError) {
    const text = typeof content === 'string' ? content : JSON.stringify(content, null, 2);
    const div = document.createElement('div');
    div.className = 'sa-msg tool-result';
    if (isError) div.classList.add('tool-error');
    div.innerHTML = `<strong>[工具结果]</strong><pre>${escapeHtml(text.substring(0, 500))}${text.length > 500 ? '...' : ''}</pre>`;
    msgsDiv.appendChild(div);
}

// ── header 渲染 ──

function _renderHeader(entry, meta = {}) {
    const task = entry.taskSummary || '';
    entry.header.innerHTML = `
        <span class="sa-icon">${_STATUS_ICONS[entry.status] || '📋'}</span>
        <span class="sa-title">${entry.status === 'running' ? '子代理执行中' : '子代理执行'}</span>
        <span class="sa-task" title="${escapeHtml(task)}">${escapeHtml(task.substring(0, 60))}${task.length > 60 ? '...' : ''}</span>
        <span class="sa-meta">${meta.iterations || 0} 轮 · ${meta.message_count || 0} 条消息</span>
        <span class="sa-toggle">${entry.expanded ? '▼' : '▶'}</span>
    `;
}

function updateHeaderMeta(entry, meta = {}) {
    const iters = meta.iterations || 0;
    const msgs = meta.message_count || 0;
    const currentTool = meta.current_tool || '';
    const toolSuffix = currentTool ? ` · 正在: ${currentTool}` : '';
    const metaEl = entry.header.querySelector('.sa-meta');
    if (metaEl) metaEl.textContent = `${iters} 轮 · ${msgs} 条消息${toolSuffix}`;
}

function _updateHeaderToggle(entry) {
    const toggle = entry.header.querySelector('.sa-toggle');
    if (toggle) toggle.textContent = entry.expanded ? '▼' : '▶';
}

// ── master 工具流（无 exec_id 的 tool_output）──

function handleMasterToolOutput(e) {
    const entry = _toolStreamTimers[e.tool_use_id];
    if (!entry) return;  // 无对应气泡（工具瞬间完成或非 bash/python），忽略

    // 事件驱动：首次有输出立即建气泡，跳过 5 秒延迟（有输出说明工具在跑）
    if (entry.delayTimer) {
        clearTimeout(entry.delayTimer);
        entry.delayTimer = null;
    }
    if (!entry.div) {
        ensureMasterToolBubble(e.tool_use_id, e.tool);
    }
    if (entry.div && entry.pre) {
        entry.pre.textContent += e.content;
        entry.offset = Math.max(entry.offset || 0, e.offset);
        _scrollChatIfNearBottom();
    }
}

// 创建 master 工具输出气泡（chat.js startToolStreaming 的 5 秒延迟定时器也调用；
// 事件流与定时器两者都调，幂等）
function ensureMasterToolBubble(toolUseId, toolName) {
    const entry = _toolStreamTimers[toolUseId];
    if (!entry || entry.div) return;

    const div = document.createElement('div');
    div.className = 'message assistant tool-streaming-bubble';
    div.dataset.toolUseId = toolUseId;

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';

    const title = document.createElement('div');
    title.className = 'streaming-title';
    title.textContent = `⚡ ${toolName} 执行中...`;
    contentDiv.appendChild(title);

    const pre = document.createElement('pre');
    pre.className = 'tool-streaming-output';
    pre.textContent = '';
    contentDiv.appendChild(pre);

    div.appendChild(contentDiv);
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    entry.div = div;
    entry.pre = pre;
}

// ── goal 级状态文本（无 exec_id → 主聊天）──
// goal 轮次由每轮 worker 卡承载（exec_id），此处仅处理 goal 循环级
// 状态文本（完成/暂停/错误），以普通 assistant 气泡进主聊天。

function handleMasterBusEvent(e) {
    if (!e || e.type !== 'text') return;
    addMessage('assistant', e.content || '');
    _scrollChatIfNearBottom();
}

// 重连时按已应用 offset 补拉 master 工具流（/stream 增量，事件流丢失兜底）
async function syncMasterToolStreams() {
    if (!currentWorkspace || !currentSession) return;
    for (const id of Object.keys(_toolStreamTimers)) {
        const entry = _toolStreamTimers[id];
        if (!entry || !entry.div) continue;
        try {
            const url = `/api/workspaces/${currentWorkspace.uuid}/sessions/${currentSession.session_id}/stream/${id}?offset=${entry.offset || 0}`;
            const resp = await fetch(url);
            if (!resp.ok) continue;
            const data = await resp.json();
            if (data.content) {
                entry.pre.textContent += data.content;
                entry.offset = Math.max(entry.offset || 0, data.offset);
            }
        } catch (e) { /* 补拉失败静默，tool_result SSE 兜底 */ }
    }
}

// ── 会话切换清理 / 恢复 ──

// 清空 worker 卡片状态与 DOM（app.js 切会话时调用，修复现存泄漏）
function clearAllAgentStreaming() {
    for (const execId of Object.keys(_agentCards)) {
        const entry = _agentCards[execId];
        if (entry.card && entry.card.parentNode) entry.card.remove();
    }
    for (const k of Object.keys(_agentCards)) delete _agentCards[k];
}

// 加载会话时若该会话有运行中的 worker，恢复 running 卡片
// （历史 messages 里只有已完成的 agent_ref，running 的需从 /executions 恢复）
async function recoverRunningCards() {
    if (!currentWorkspace || !currentSession) return;
    try {
        const ws = currentWorkspace.uuid;
        const sid = currentSession.session_id;
        const resp = await fetch(`/api/workspaces/${ws}/sessions/${sid}/executions`);
        if (!resp.ok) return;
        const data = await resp.json();
        (data.executions || []).forEach(log => {
            const execId = log.exec_id;
            const status = log.metadata && log.metadata.status;
            if (execId && status === 'running') {
                ensureAgentCard(execId, log.task || log.summary || '', {
                    status: 'running',
                    iterations: (log.metadata && log.metadata.iterations) || 0,
                });
            }
        });
    } catch (e) { /* 恢复失败静默，事件流 agent_start 仍会建卡 */ }
}

// ── 内部工具函数 ──

function ensureMsgsDiv(entry) {
    if (entry.msgsDiv) return entry.msgsDiv;
    const msgsDiv = document.createElement('div');
    msgsDiv.className = 'sa-messages';
    entry.detail.appendChild(msgsDiv);
    entry.msgsDiv = msgsDiv;
    return msgsDiv;
}

function _scrollChatIfNearBottom() {
    const el = document.getElementById('chat-messages');
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
    if (near) el.scrollTop = el.scrollHeight;
}
