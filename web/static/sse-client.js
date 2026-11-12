// ── sse-client.js ── 全局 SSE 事件流客户端（master 工具流 + 子代理事件路由）
// 子代理卡片的全部状态与渲染由 agent-cards.js 的 AgentCardStore 负责。
// 依赖 app.js: currentWorkspace, currentSession, chatMessages, loadSession,
//   setQueueState, _handleDeliveredBatch, addMessage, foregroundStreamSessionId,
//   isSending, isStopping, updateComposerButtons
// 依赖 utils.js: renderMarkdown
// 依赖 chat.js: _toolStreamTimers, _activeTurnContainers

// ── 全局状态 ──
let _eventSource = null;          // 当前 EventSource
let _esWasDisconnected = false;   // 曾断线（onopen 时用于对齐）
let _lastHidden = document.hidden;  // 上次可见性：重复派发/加载期事件据此短路

// master 自动恢复流式渲染（后台 agent 完成后 master 继续执行，text 逐 token 推送）
let _masterResumeOpenBlock = null; // {kind:'text'|'thinking', text, el}

// ── 事件流生命周期 ──

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
        if (_esWasDisconnected) {
            _esWasDisconnected = false;
            alignAfterReconnect();
        }
    };
    es.onerror = () => { _esWasDisconnected = true; };
    _eventSource = es;
}

function closeEventSource(markDisconnected = false) {
    const had = _eventSource !== null;
    if (_eventSource) {
        _eventSource.close();
        _eventSource = null;
    }
    _esWasDisconnected = markDisconnected && had;
}

// 标签页切到后台时断开事件流，回前台重连并按「刷新恢复」的路径自愈。
document.addEventListener('visibilitychange', () => {
    if (document.hidden === _lastHidden) return;
    _lastHidden = document.hidden;
    if (document.hidden) {
        closeEventSource(true);
        return;
    }
    connectEventSource();
    if (currentSession && foregroundStreamSessionId !== currentSession.session_id) {
        loadSession(currentSession.session_id);
    }
});

// 重连对齐：展开中的 running 卡片补拉详情 + master 工具流按 offset 补拉
async function alignAfterReconnect() {
    if (window.AgentCardStore) {
        await AgentCardStore.alignAfterReconnect();
    }
    await syncMasterToolStreams();
}

// ── 主路由 ──

let _loadSessionsTimer = null;
function scheduleLoadSessions() {
    clearTimeout(_loadSessionsTimer);
    _loadSessionsTimer = setTimeout(() => {
        if (typeof loadSessions === 'function') loadSessions();
    }, 300);
}

function handleBusEvent(e) {
    if (!e || typeof e !== 'object') return;
    if (!currentSession || e.session_id !== currentSession.session_id) return;

    // 队列事件必须在任何去重早退之前处理：队列条是服务端权威状态。
    if (e.type === 'queue_update') {
        setQueueState(e.items, e.paused, e.reason);
        return;
    }
    if (e.type === 'user_message') {
        const fresh = _handleDeliveredBatch(e);
        if (foregroundStreamSessionId !== currentSession.session_id && fresh && e.content) {
            addMessage('user', e.content);
        }
        return;
    }
    if (e.type === 'session_created' || e.type === 'session_fork_created') {
        scheduleLoadSessions();
        return;
    }

    // 后台 agent 完成后，如果 master 空闲，自动触发 master 继续处理通知。
    if (e.type === 'background_runner_resume') {
        if (typeof isSending !== 'undefined' && !isSending) {
            setTimeout(() => triggerResumeForBackgroundAgent(), 100);
        }
        return;
    }

    // 本回合已落盘（master run / resume 结束）
    if (e.type === 'turn_complete') {
        isStopping = false;
        updateComposerButtons();
        if (foregroundStreamSessionId !== currentSession.session_id) {
            loadSession(currentSession.session_id);
        }
        window.dispatchEvent(new CustomEvent('cili:turn-complete', {
            detail: { session_id: e.session_id },
        }));
        return;
    }

    // 带 exec_id 的事件全部属于子代理卡片：模型、统计、详情都由 store 处理。
    if (e.exec_id) {
        if (window.AgentCardStore) AgentCardStore.handle(e);
        return;
    }

    // master 工具实时输出（无 exec_id）
    if (e.type === 'tool_output') {
        handleMasterToolOutput(e);
        return;
    }

    // 无 exec_id 的 text/thinking/tool_use/tool_result：master 自动恢复循环。
    // 本页正持有当前会话的前台流时由请求级渲染负责，避免双份。
    if (foregroundStreamSessionId === currentSession.session_id) return;
    if (e.type === 'text') {
        _handleMasterResumeText(e.content || '');
    } else if (e.type === 'thinking') {
        _handleMasterResumeThinking(e.content || '');
    } else if (e.type === 'tool_use') {
        _handleMasterResumeToolUse(e);
    } else if (e.type === 'tool_result') {
        _handleMasterResumeToolResult(e);
    }
}

// ── master 工具流 ──

function handleMasterToolOutput(e) {
    const entry = _toolStreamTimers[e.tool_use_id];
    if (!entry) return;

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
        scrollChatIfNearBottom();
    }
}

// 创建 master 工具输出气泡（chat.js startToolStreaming 的延迟定时器也调用，幂等）
function ensureMasterToolBubble(toolUseId, toolName) {
    const entry = _toolStreamTimers[toolUseId];
    if (!entry || entry.div) return;
    if (!currentSession || entry.sessionId !== currentSession.session_id) return;

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
    const container = _activeTurnContainers.get(entry.sessionId) || chatMessages;
    container.appendChild(div);
    if (chatMessages.contains(div)) {
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    entry.div = div;
    entry.pre = pre;
}

// ── master 自动恢复流式渲染 ──

function resetMasterStreamBlocks() {
    _masterResumeOpenBlock = null;
}

function _handleMasterResumeText(content) {
    if (_masterResumeOpenBlock && _masterResumeOpenBlock.kind === 'text') {
        _masterResumeOpenBlock.text += content;
        if (_masterResumeOpenBlock.el) {
            const contentEl = _masterResumeOpenBlock.el.querySelector('.message-content');
            if (contentEl) contentEl.innerHTML = renderMarkdown(_masterResumeOpenBlock.text);
        }
    } else {
        _finalizeMasterResumeBlock();
        _masterResumeOpenBlock = { kind: 'text', text: content, el: null };
        _masterResumeOpenBlock.el = addMessage('assistant', content);
    }
    scrollChatIfNearBottom();
}

function _handleMasterResumeThinking(content) {
    if (_masterResumeOpenBlock && _masterResumeOpenBlock.kind === 'thinking') {
        _masterResumeOpenBlock.text += content;
        if (_masterResumeOpenBlock.el) {
            const tc = _masterResumeOpenBlock.el.querySelector('.think-content');
            if (tc) tc.innerHTML = renderMarkdown(_masterResumeOpenBlock.text);
        }
    } else {
        _finalizeMasterResumeBlock();
        _masterResumeOpenBlock = { kind: 'thinking', text: content, el: null };
        const div = addMessage('assistant', '');
        div.classList.add('thinking');
        const contentDiv = div.querySelector('.message-content');
        const thinkTitle = document.createElement('div');
        thinkTitle.className = 'think-title';
        thinkTitle.textContent = '💭 思考过程';
        contentDiv.appendChild(thinkTitle);
        const thinkDiv = document.createElement('div');
        thinkDiv.className = 'think-content';
        thinkDiv.innerHTML = renderMarkdown(content);
        contentDiv.appendChild(thinkDiv);
        _masterResumeOpenBlock.el = div;
    }
    scrollChatIfNearBottom();
}

function _handleMasterResumeToolUse(e) {
    _finalizeMasterResumeBlock();
    const div = addMessage('assistant', '');
    div.classList.add('tool');
    const contentDiv = div.querySelector('.message-content');
    const pre = document.createElement('pre');
    pre.textContent = typeof e.input === 'string' ? e.input : JSON.stringify(e.input, null, 2);
    const toolTitle = document.createElement('div');
    toolTitle.className = 'tool-title';
    toolTitle.textContent = `[调用工具: ${e.tool}]`;
    contentDiv.appendChild(toolTitle);
    contentDiv.appendChild(pre);
}

function _handleMasterResumeToolResult(e) {
    _finalizeMasterResumeBlock();
    const text = typeof e.content === 'string' ? e.content : JSON.stringify(e.content, null, 2);
    const div = addMessage('assistant', '');
    div.classList.add('tool');
    if (e.is_error) {
        div.classList.add('tool-error');
    } else {
        div.classList.add('tool-result');
    }
    const contentDiv = div.querySelector('.message-content');
    const resultTitle = document.createElement('div');
    resultTitle.className = 'tool-title';
    resultTitle.textContent = '[工具结果]';
    contentDiv.appendChild(resultTitle);
    const pre = document.createElement('pre');
    pre.textContent = text;
    contentDiv.appendChild(pre);
}

function _finalizeMasterResumeBlock() {
    _masterResumeOpenBlock = null;
}

// 重连时按已应用 offset 补拉 master 工具流（事件流丢失兜底）
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

// ── 后台 agent 完成后触发 master 继续 ──

async function triggerResumeForBackgroundAgent() {
    if (typeof isSending !== 'undefined' && isSending) return;
    if (!currentWorkspace || !currentSession) return;

    const wsUuid = currentWorkspace.uuid;
    const sessionId = currentSession.session_id;

    try {
        const resp = await fetch(`/api/workspaces/${wsUuid}/sessions/${sessionId}/resume`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        if (!resp.ok) {
            console.log('Resume request failed:', resp.status);
        }
    } catch (e) {
        console.log('Resume request error:', e);
    }
}
