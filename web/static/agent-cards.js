// ── agent-cards.js ── 子代理卡片模型与渲染
// 依赖 app.js: chatMessages, currentWorkspace, currentSession
// 依赖 utils.js: escapeHtml, renderMarkdown
// 依赖 chat.js: normalizeContent
// sse-client.js 只负责事件分派；卡片状态、展开状态、详情渲染全部在这里。

const AgentCardStore = (() => {
    const MODELS = new Map();          // exec_id -> model
    let _sessionKey = null;
    let _seen = new Set();

    const EXPANDED_PREFIX = 'cili:agent-expanded:';
    const MAX_BLOCKS = 2000;
    const MAX_BLOCK_CHARS = 2 * 1024 * 1024;
    const STATUS_ICONS = {
        completed: '✅',
        error: '❌',
        failed: '❌',
        timeout: '⏱️',
        running: '🔄',
        stopped: '⏹️',
    };

    function _expandedKey() {
        return EXPANDED_PREFIX + (_sessionKey || '');
    }

    function _loadExpanded() {
        try {
            const raw = JSON.parse(sessionStorage.getItem(_expandedKey()) || '[]');
            return new Set(Array.isArray(raw) ? raw : []);
        } catch (e) {
            return new Set();
        }
    }

    function _saveExpanded(set) {
        try {
            sessionStorage.setItem(_expandedKey(), JSON.stringify([...set]));
        } catch (e) { /* sessionStorage 不可用时只保留内存状态 */ }
    }

    function _setExpanded(execId, expanded) {
        const set = _loadExpanded();
        if (expanded) set.add(execId);
        else set.delete(execId);
        _saveExpanded(set);
    }

    function _statsFrom(value) {
        if (!value || typeof value !== 'object') return null;
        const src = (value.stats && typeof value.stats === 'object') ? value.stats : value;
        const keys = ['iterations', 'message_count', 'tool_call_count'];
        if (!keys.some(k => Number.isFinite(src[k]))) return null;
        return {
            iterations: Number.isFinite(src.iterations) ? src.iterations : 0,
            message_count: Number.isFinite(src.message_count) ? src.message_count : 0,
            tool_call_count: Number.isFinite(src.tool_call_count) ? src.tool_call_count : 0,
        };
    }

    function _blocksFromMessages(messages) {
        const out = [];
        (messages || []).forEach(message => {
            const role = message.role || 'assistant';
            const epoch = (message._meta && message._meta.epoch) || 0;
            const blocks = typeof normalizeContent === 'function'
                ? normalizeContent(message.content)
                : [];
            blocks.forEach(b => {
                if (b.kind === 'text' && b.text) {
                    out.push({ kind: 'text', text: b.text, role, epoch });
                } else if (b.kind === 'thinking' && b.text) {
                    out.push({ kind: 'thinking', text: b.text, role, epoch });
                } else if (b.kind === 'tool_call') {
                    out.push({
                        kind: 'tool_use', tool: b.name, input: b.input,
                        toolUseId: b.id, output: '', outputOffset: 0, role, epoch,
                    });
                } else if (b.kind === 'tool_result') {
                    out.push({
                        kind: 'tool_result', content: b.content,
                        isError: b.is_error, toolUseId: b.tool_use_id, role, epoch,
                    });
                }
            });
        });
        return out;
    }

    function _createModel(execId, seed = {}) {
        const model = {
            execId,
            taskSummary: seed.task_summary || seed.task || '',
            status: seed.status || 'running',
            background: !!seed.background,
            expanded: _loadExpanded().has(execId),
            stats: _statsFrom(seed.stats),
            epoch: seed.epoch || 0,
            maxEpochs: seed.max_epochs || 0,
            currentTool: '',
            blocks: [],
            openBlock: null,
            loaded: false,
            needsFullReload: false,
            loading: false,
            cardEl: null,
            headerEl: null,
            detailEl: null,
            bodyEl: null,
            toolBlocks: new Map(),
            blockChars: 0,
        };
        MODELS.set(execId, model);
        return model;
    }

    function _renderHeader(model) {
        if (!model.headerEl) return;
        const stats = model.stats;
        let meta = stats
            ? `${stats.iterations} 轮 · ${stats.message_count} 条消息 · ${stats.tool_call_count} 次工具`
            : '统计中…';
        if (model.status === 'running' && model.currentTool) {
            meta += ` · 正在: ${model.currentTool}`;
        }
        const task = model.taskSummary || '';
        const title = model.status === 'running' ? '任务执行中' : '任务已结束';
        const mode = model.background
            ? '<span class="sa-mode-badge">异步</span>'
            : '<span class="sa-mode-badge sa-mode-sync">同步</span>';
        model.headerEl.innerHTML = `
            <span class="sa-icon">${STATUS_ICONS[model.status] || '📋'}</span>
            <span class="sa-title">${title}</span>
            ${mode}
            <span class="sa-task" title="${escapeHtml(task)}">${escapeHtml(task.substring(0, 60))}${task.length > 60 ? '...' : ''}</span>
            <span class="sa-meta">${escapeHtml(meta)}</span>
            <span class="sa-toggle">${model.expanded ? '▼' : '▶'}</span>
        `;
    }

    function _renderBlock(model, block) {
        if (block.kind === 'text') {
            const div = document.createElement('div');
            div.className = `sa-msg ${block.role === 'user' ? 'user' : 'assistant'}`;
            div.innerHTML = renderMarkdown(block.text || '');
            block.el = div;
            return div;
        }
        if (block.kind === 'thinking') {
            const div = document.createElement('div');
            div.className = 'sa-msg thinking';
            div.innerHTML = `<div class="think-content">${renderMarkdown(block.text || '')}</div>`;
            block.el = div;
            return div;
        }
        if (block.kind === 'tool_use') {
            const div = document.createElement('div');
            div.className = 'sa-msg tool-use';
            const input = block.input;
            const inputStr = typeof input === 'string' ? input : (JSON.stringify(input, null, 2) || '');
            div.innerHTML = `<strong>[工具调用: ${escapeHtml(block.tool || '')}]</strong><pre>${escapeHtml(inputStr)}</pre>`;
            block.el = div;
            if (block.toolUseId) model.toolBlocks.set(block.toolUseId, block);
            if (block.output) {
                const pre = document.createElement('pre');
                pre.className = 'sa-tool-output';
                pre.textContent = block.output;
                block.preEl = pre;
                div.appendChild(pre);
            }
            return div;
        }
        if (block.kind === 'tool_result') {
            const content = block.content;
            const text = typeof content === 'string'
                ? content
                : (JSON.stringify(content, null, 2) || '');
            const div = document.createElement('div');
            div.className = 'sa-msg tool-result';
            if (block.isError) div.classList.add('tool-error');
            div.innerHTML = `<strong>[工具结果]</strong><pre>${escapeHtml(text.substring(0, 500))}${text.length > 500 ? '...' : ''}</pre>`;
            block.el = div;
            return div;
        }
        return null;
    }

    function _appendEpochDivider(model, epoch) {
        const div = document.createElement('div');
        div.className = 'sa-epoch-divider';
        div.textContent = `第 ${epoch} 轮`;
        model.bodyEl.appendChild(div);
    }

    function _appendBlock(model, block) {
        if (!model.bodyEl) return;
        const el = _renderBlock(model, block);
        if (el) model.bodyEl.appendChild(el);
    }

    function _scrollIfNearBottom() {
        const el = document.getElementById('chat-messages');
        if (!el) return;
        if (el.scrollHeight - el.scrollTop - el.clientHeight < 100) {
            el.scrollTop = el.scrollHeight;
        }
    }

    function _renderDetail(model) {
        if (!model.bodyEl) return;
        model.bodyEl.innerHTML = '';
        model.toolBlocks.clear();
        let lastEpoch = null;
        (model.blocks || []).forEach(block => {
            if (block.epoch && block.epoch !== lastEpoch) {
                _appendEpochDivider(model, block.epoch);
                lastEpoch = block.epoch;
            }
            _appendBlock(model, block);
        });
        _scrollIfNearBottom();
    }

    function _trimBlocks(model) {
        if (model.blocks.length <= MAX_BLOCKS && model.blockChars <= MAX_BLOCK_CHARS) {
            return false;
        }
        model.needsFullReload = true;
        model.blocks = [];
        model.openBlock = null;
        model.blockChars = 0;
        if (model.expanded && model.bodyEl) model.bodyEl.innerHTML = '';
        return true;
    }

    function _pushBlock(model, block) {
        model.blockChars += (block.text ? block.text.length : 0)
            + (block.output ? block.output.length : 0);
        model.blocks.push(block);
        if (_trimBlocks(model)) return;
        if (model.expanded) {
            const prev = model.blocks.length > 1 ? model.blocks[model.blocks.length - 2] : null;
            if (block.epoch && (!prev || prev.epoch !== block.epoch)) {
                _appendEpochDivider(model, block.epoch);
            }
            _appendBlock(model, block);
            _scrollIfNearBottom();
        }
    }

    function _updateOpenText(model, kind, content) {
        if (model.openBlock && model.openBlock.kind === kind) {
            model.openBlock.text += content;
            if (model.expanded && model.openBlock.el) {
                if (kind === 'thinking') {
                    const tc = model.openBlock.el.querySelector('.think-content');
                    if (tc) tc.innerHTML = renderMarkdown(model.openBlock.text);
                } else {
                    model.openBlock.el.innerHTML = renderMarkdown(model.openBlock.text);
                }
            }
            return;
        }
        model.openBlock = { kind, text: content, epoch: model.epoch || 1 };
        _pushBlock(model, model.openBlock);
    }

    function _onToolUse(model, e) {
        model.openBlock = null;
        _pushBlock(model, {
            kind: 'tool_use',
            tool: e.tool,
            input: e.input,
            toolUseId: e.tool_use_id,
            output: '',
            outputOffset: 0,
            epoch: model.epoch || 1,
        });
    }

    function _onToolResult(model, e) {
        model.openBlock = null;
        _pushBlock(model, {
            kind: 'tool_result',
            content: e.content,
            isError: !!e.is_error,
            toolUseId: e.tool_use_id,
            epoch: model.epoch || 1,
        });
    }

    function _onToolOutput(model, e) {
        const block = model.toolBlocks.get(e.tool_use_id);
        if (!block) return;
        const offset = Number.isFinite(e.offset) ? e.offset : null;
        if (offset !== null && offset <= (block.outputOffset || 0)) return;
        block.output += e.content || '';
        if (offset !== null) {
            block.outputOffset = Math.max(block.outputOffset || 0, offset);
        }
        if (model.expanded) {
            if (!block.preEl && block.el) {
                const pre = document.createElement('pre');
                pre.className = 'sa-tool-output';
                block.el.appendChild(pre);
                block.preEl = pre;
            }
            if (block.preEl) block.preEl.textContent = block.output;
            _scrollIfNearBottom();
        }
    }

    async function _loadExecution(model) {
        if (model.loading) return;
        const ws = currentWorkspace && currentWorkspace.uuid;
        const sid = currentSession && currentSession.session_id;
        if (!ws || !sid) return;
        model.loading = true;
        try {
            const resp = await fetch(`/api/workspaces/${ws}/sessions/${sid}/executions/${model.execId}`);
            if (!resp.ok) return;
            const data = await resp.json();
            const stats = _statsFrom(data.stats || (data.metadata && data.metadata.stats));
            if (stats) model.stats = stats;
            model.blocks = _blocksFromMessages(data.messages || []);
            model.blockChars = model.blocks.reduce(
                (n, b) => n + ((b.text || '').length + (b.output || '').length), 0
            );
            model.toolBlocks.clear();
            model.blocks.forEach(b => {
                if (b.kind === 'tool_use' && b.toolUseId) {
                    model.toolBlocks.set(b.toolUseId, b);
                }
            });
            const last = model.blocks[model.blocks.length - 1];
            model.openBlock = last && (last.kind === 'text' || last.kind === 'thinking')
                ? last : null;
            model.loaded = true;
            model.needsFullReload = false;
            _renderHeader(model);
            if (model.expanded) _renderDetail(model);
        } catch (err) {
            console.error('加载执行详情失败:', err);
        } finally {
            model.loading = false;
        }
    }

    function _ensureCard(model, container = null) {
        if (!model.cardEl) {
            const card = document.createElement('div');
            card.className = 'message assistant session-card';
            card.dataset.execId = model.execId;

            const header = document.createElement('div');
            header.className = 'session-header';

            const detail = document.createElement('div');
            detail.className = 'session-detail';

            const body = document.createElement('div');
            body.className = 'sa-messages';
            detail.appendChild(body);

            card.appendChild(header);
            card.appendChild(detail);
            header.addEventListener('click', () => toggle(model.execId));

            model.cardEl = card;
            model.headerEl = header;
            model.detailEl = detail;
            model.bodyEl = body;
        }
        // 只在首次出现时挂载；同一 exec_id 的后续引用只更新模型，
        // 不得把已挂载的卡片移动到新的位置，否则历史顺序会被打乱。
        const target = container || chatMessages;
        if (target && !model.cardEl.parentNode) {
            target.appendChild(model.cardEl);
        }
        model.detailEl.style.display = model.expanded ? 'block' : 'none';
        _renderHeader(model);
        if (model.expanded) {
            if (model.loaded && !model.needsFullReload) _renderDetail(model);
            else _loadExecution(model);
        }
        return model.cardEl;
    }

    function toggle(execId) {
        const model = MODELS.get(execId);
        if (!model) return;
        model.expanded = !model.expanded;
        _setExpanded(execId, model.expanded);
        if (model.detailEl) {
            model.detailEl.style.display = model.expanded ? 'block' : 'none';
        }
        _renderHeader(model);
        if (model.expanded) {
            if (model.loaded && !model.needsFullReload) _renderDetail(model);
            else _loadExecution(model);
        }
    }

    function mountFromHistory(seed, container = null) {
        if (!seed || !seed.exec_id) return null;
        let model = MODELS.get(seed.exec_id);
        if (!model) {
            model = _createModel(seed.exec_id, seed);
        } else {
            if (seed.task_summary) model.taskSummary = seed.task_summary;
            if (seed.status) model.status = seed.status;
            if (seed.background !== undefined) model.background = !!seed.background;
            const stats = _statsFrom(seed.stats);
            if (stats) model.stats = stats;
        }
        _seen.add(seed.exec_id);
        _ensureCard(model, container);
        return model;
    }

    function onStart(e) {
        let model = MODELS.get(e.exec_id);
        if (model) {
            if (e.task_summary) model.taskSummary = e.task_summary;
            if (e.background !== undefined) model.background = !!e.background;
            model.status = 'running';
        } else {
            model = _createModel(e.exec_id, {
                task_summary: e.task_summary || '',
                status: 'running',
                background: !!e.background,
            });
        }
        _ensureCard(model);
    }

    function onProgress(e) {
        const model = MODELS.get(e.exec_id) || _createModel(e.exec_id, { status: 'running' });
        const stats = _statsFrom(e.stats);
        if (stats) model.stats = stats;
        if (e.status) model.status = e.status;
        if (e.epoch) model.epoch = e.epoch;
        model.currentTool = e.current_tool || '';
        _renderHeader(model);
    }

    function onEpochStarted(e) {
        const model = MODELS.get(e.exec_id) || _createModel(e.exec_id, { status: 'running' });
        const stats = _statsFrom(e.stats);
        if (stats) model.stats = stats;
        model.epoch = e.epoch || model.epoch;
        model.maxEpochs = e.max_epochs || model.maxEpochs;
        _renderHeader(model);
    }

    function onComplete(e) {
        const model = MODELS.get(e.exec_id) || _createModel(e.exec_id, {});
        const rank = { completed: 0, stopped: 1, error: 2, failed: 2, timeout: 2 };
        const cur = rank[model.status] !== undefined ? rank[model.status] : 0;
        const next = rank[e.status] !== undefined ? rank[e.status] : 0;
        if (next > cur || model.status === 'running') {
            model.status = e.status || 'completed';
        }
        const stats = _statsFrom(e.stats);
        if (stats) model.stats = stats;
        _renderHeader(model);
        if (model.expanded) _loadExecution(model);
    }

    function handle(e) {
        if (!e || !e.exec_id) return false;
        switch (e.type) {
            case 'session_start':
                onStart(e);
                return true;
            case 'session_progress':
                onProgress(e);
                return true;
            case 'agent_epoch_started':
                onEpochStarted(e);
                return true;
            case 'session_complete':
            case 'agent_run_settled':
                onComplete(e);
                return true;
            case 'text':
                _updateOpenText(
                    MODELS.get(e.exec_id) || _createModel(e.exec_id, { status: 'running' }),
                    'text', e.content || ''
                );
                return true;
            case 'thinking':
                _updateOpenText(
                    MODELS.get(e.exec_id) || _createModel(e.exec_id, { status: 'running' }),
                    'thinking', e.content || ''
                );
                return true;
            case 'tool_use':
                _onToolUse(
                    MODELS.get(e.exec_id) || _createModel(e.exec_id, { status: 'running' }),
                    e
                );
                return true;
            case 'tool_result':
                _onToolResult(
                    MODELS.get(e.exec_id) || _createModel(e.exec_id, { status: 'running' }),
                    e
                );
                return true;
            case 'tool_output': {
                const model = MODELS.get(e.exec_id);
                if (model) _onToolOutput(model, e);
                return true;
            }
            default:
                return false;
        }
    }

    function beginSession(key, { keepModels = false } = {}) {
        _sessionKey = key;
        _seen = new Set();
        if (!keepModels) clear();
    }

    function clear() {
        MODELS.forEach(model => {
            if (model.cardEl && model.cardEl.parentNode) model.cardEl.remove();
        });
        MODELS.clear();
    }

    function reconcile() {
        for (const [execId, model] of [...MODELS.entries()]) {
            if (_seen.has(execId) || model.status === 'running') continue;
            if (model.cardEl && model.cardEl.parentNode) model.cardEl.remove();
            MODELS.delete(execId);
        }
    }

    async function mountRunning() {
        const ws = currentWorkspace && currentWorkspace.uuid;
        const sid = currentSession && currentSession.session_id;
        if (!ws || !sid) return;
        try {
            const resp = await fetch(`/api/workspaces/${ws}/sessions/${sid}/executions`);
            if (!resp.ok) return;
            const data = await resp.json();
            (data.executions || []).forEach(log => {
                const meta = log.metadata || {};
                if (meta.status !== 'running') return;
                let model = MODELS.get(log.exec_id);
                if (!model) {
                    model = _createModel(log.exec_id, {
                        task_summary: log.task || log.summary || '',
                        status: 'running',
                        stats: log.stats || meta.stats,
                        epoch: meta.epoch || 0,
                        max_epochs: meta.max_epochs || 0,
                    });
                } else {
                    const stats = _statsFrom(log.stats || meta.stats);
                    if (stats) model.stats = stats;
                }
                _ensureCard(model);
            });
        } catch (e) { /* 事件流 agent_start 会兜底建卡 */ }
    }

    async function alignAfterReconnect() {
        for (const model of MODELS.values()) {
            if (model.expanded && model.status === 'running') {
                await _loadExecution(model);
            }
        }
    }

    return {
        beginSession,
        clear,
        reconcile,
        mountFromHistory,
        mountRunning,
        handle,
        toggle,
        onStart,
        onProgress,
        onEpochStarted,
        onComplete,
        alignAfterReconnect,
        get: execId => MODELS.get(execId),
    };
})();

window.AgentCardStore = AgentCardStore;
