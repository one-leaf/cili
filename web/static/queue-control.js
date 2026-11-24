// ── 用户回合排队条、投递确认、停止/状态控制 ──
// 依赖 chat.js/app.js 的会话状态与发送工具；与 /queue、/stop、/status 端点对应。

// ── 排队队列条与投递 ──
// 队列状态在服务端：本地只缓存快照（queue_update / /status），操作均走 API。
const _deliveredQueueIds = new Set();  // 已投递批次幂等键（请求流 + 全局流双路去重）

function makeClientId() {
    try {
        if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    } catch (e) { /* 非安全上下文无 randomUUID */ }
    return `c-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function setQueueState(items, paused, reason) {
    queuedItems = items || [];
    queuePaused = !!paused;
    queueReason = reason || '';
    renderQueueBar();
}

function renderQueueBar() {
    if (!queuedMessagesEl) return;
    const hasSession = !!currentSession;
    // 仅在有排队项时显示；「暂停但 0 条」不显示——没东西可继续/清空，
    // 且下次发消息入队会自动恢复暂停态（设计 §3.4）
    const visible = hasSession && queuedItems.length > 0;
    queuedMessagesEl.hidden = !visible;
    if (!visible) {
        queuedListEl.innerHTML = '';
        return;
    }
    const reasonText = queueReason === 'error' ? '（上回合出错）' : (queuePaused ? '（已停止）' : '');
    queuedLabelEl.textContent = queuePaused
        ? `队列已暂停${reasonText} · ${queuedItems.length} 条`
        : `待发送 ${queuedItems.length} 条`;
    if (queueResumeBtn) queueResumeBtn.hidden = !queuePaused;
    queuedListEl.innerHTML = '';
    queuedItems.forEach(item => {
        const row = document.createElement('div');
        row.className = 'queued-item';

        const text = document.createElement('div');
        text.className = 'queued-item-text';
        text.textContent = item.content || '（图片）';
        text.title = item.content || '';
        row.appendChild(text);

        if (item.has_images) {
            const badge = document.createElement('span');
            badge.className = 'queued-item-badge';
            badge.textContent = '🖼';
            badge.title = '含图片';
            row.appendChild(badge);
        }

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'queued-item-remove';
        removeBtn.title = '移除该条';
        removeBtn.textContent = '✕';
        removeBtn.addEventListener('click', () => removeQueuedItem(item.item_id));
        row.appendChild(removeBtn);

        queuedListEl.appendChild(row);
    });
}

function upsertQueuedItem(item) {
    if (!item || !item.item_id) return;
    const idx = queuedItems.findIndex(it => it.item_id === item.item_id);
    if (idx >= 0) queuedItems[idx] = item;
    else queuedItems.push(item);
    renderQueueBar();
}

function handleDeliveredBatch(event) {
    // 同一批可能同时到达请求级帧与全局事件：按 item_ids 幂等
    const ids = event.item_ids || [];
    const fresh = ids.filter(id => !_deliveredQueueIds.has(id));
    ids.forEach(id => _deliveredQueueIds.add(id));
    if (_deliveredQueueIds.size > 500) _deliveredQueueIds.clear();
    if (ids.length) {
        queuedItems = queuedItems.filter(it => !ids.includes(it.item_id));
        renderQueueBar();
    }
    return fresh.length > 0;
}

async function removeQueuedItem(itemId) {
    if (!currentWorkspace || !currentSession) return;
    const sessionId = currentSession.session_id;
    try {
        await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}/queue/${itemId}`,
            { method: 'DELETE' }
        );
    } catch (e) { /* 失败时等下一次 queue_update 校正 */ }
    // 请求期间已切走：旧的队列快照不得改到新会话的队列条
    if (!currentSession || currentSession.session_id !== sessionId) return;
    queuedItems = queuedItems.filter(it => it.item_id !== itemId);
    renderQueueBar();
}

async function clearQueue() {
    if (!currentWorkspace || !currentSession) return;
    const sessionId = currentSession.session_id;
    try {
        const resp = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}/queue/clear`,
            { method: 'POST' }
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        // 请求期间已切走：旧的队列快照不得改到新会话的队列条
        if (!currentSession || currentSession.session_id !== sessionId) return;
        queuedItems = [];
        queuePaused = false;
        queueReason = '';
        renderQueueBar();
    } catch (e) {
        showToast('清空队列失败');
    }
}

async function resumeQueue() {
    if (!currentWorkspace || !currentSession) return;
    const sessionId = currentSession.session_id;
    try {
        const resp = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}/queue/resume`,
            { method: 'POST' }
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        if (!currentSession || currentSession.session_id !== sessionId) return;
        queuePaused = false;
        queueReason = '';
        renderQueueBar();
    } catch (e) {
        showToast('恢复队列失败');
    }
}

async function refreshSessionStatus(sessionId) {
    if (!currentWorkspace || !currentSession || currentSession.session_id !== sessionId) return;
    try {
        const resp = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}/status`);
        if (!resp.ok) return;
        const status = await resp.json();
        // 请求期间用户可能已切走：旧会话的队列/运行快照不得写进新会话
        if (!currentSession || currentSession.session_id !== sessionId) return;
        sessionRunning = !!status.running;
        sessionBackground = status.background || { agents: 0, shells: 0, active: 0, items: [] };
        queuedItems = status.queue_items || queuedItems;
        queuePaused = !!status.queue_paused;
        queueReason = status.queue_reason || '';
        updateComposerButtons();
    } catch (e) { /* 状态刷新失败时沿用本地推断 */ }
}

// Stop agent（直接结束当前正在运行的 runner；排队项不暂停，由泵继续）
async function stopAgent() {
    if (!currentWorkspace || !currentSession || isStopping) return;

    // 停止即收起推荐并作废在途请求（设计 §2.3）
    if (typeof hideSuggestBar === 'function') hideSuggestBar({ invalidate: true });
    isStopping = true;
    updateComposerButtons();

    let success = false;
    try {
        const response = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${currentSession.session_id}/stop`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ scope: 'turn' }),
            }
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const result = await response.json();
        if (result.success) {
            success = true;
        } else {
            console.warn('Stop failed:', result.message);
        }
    } catch (error) {
        console.error('Failed to stop agent:', error);
    }

    if (!success) {
        // 停止信号未送达（回合可能已自行结束）：恢复可点击
        isStopping = false;
        updateComposerButtons();
        return;
    }

    // 兜底超时：流长时间未关闭（如 LLM 重试卡住）时恢复可点击
    clearTimeout(window._stopPendingTimer);
    window._stopPendingTimer = setTimeout(() => {
        if (isStopping) {
            isStopping = false;
            updateComposerButtons();
        }
    }, 60000);

    // 禁用所有 pending 的 ask_user 问题卡片
    document.querySelectorAll('.ask-user-card').forEach(card => {
        const submitBtn = card.querySelector('.ask-submit');
        if (submitBtn && !submitBtn.classList.contains('ask-submitted')) {
            submitBtn.textContent = '已取消';
            submitBtn.disabled = true;
        }
        card.querySelectorAll('.ask-option').forEach(el => el.style.pointerEvents = 'none');
        card.querySelectorAll('input').forEach(el => el.disabled = true);
        settleAskUserCard(card);
    });
    updateComposerButtons();
}

// 停后台：黄方块/黄色徽标。只停 agent + shell，不动 master 回合。
async function stopBackground() {
    if (!currentWorkspace || !currentSession) return;
    const sessionId = currentSession.session_id;
    if (typeof hideSuggestBar === 'function') hideSuggestBar({ invalidate: true });
    try {
        const resp = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}/stop`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ scope: 'background' }),
            }
        );
        const result = await resp.json().catch(() => ({}));
        if (!resp.ok || !result.success) {
            showToast(result.message || '停止后台任务失败');
            return;
        }
        await refreshSessionStatus(sessionId);
    } catch (e) {
        console.error('Failed to stop background tasks:', e);
        showToast('停止后台任务失败');
    }
}

// Send message
