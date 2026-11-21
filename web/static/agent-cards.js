// ── agent-cards.js ── 子代理卡片模型与渲染
// 依赖 app.js: chatMessages, currentWorkspace, currentSession
// 依赖 utils.js: escapeHtml, renderMarkdown
// 依赖 chat.js: normalizeContent
// sse-client.js 只负责事件分派；卡片状态、展开状态、详情渲染全部在这里。

const AgentCardStore = (() => {
    const MODELS = new Map();          // exec_id -> model
    let _sessionKey = null;
    let _seen = new Set();

    const TERMINAL_STATUSES = new Set(['completed', 'failed', 'error', 'timeout', 'stopped', 'interrupted']);
    const STATUS_ICONS = {
        completed: '✅',
        error: '❌',
        failed: '❌',
        timeout: '⏱️',
        running: '🔄',
        stopped: '⏹️',
        interrupted: '⚠️',
        check: '🔍',
        handoff: '🔄',
        continuation: '🔄',
        queued: '⏳',
    };
    const PHASE_TITLES = {
        check: '任务检查中',
        handoff: '任务续跑中',
        finalizing: '任务收尾中',
        stopping: '任务停止中',
    };

    function isTerminalStatus(status) {
        return TERMINAL_STATUSES.has(status);
    }

    function _normalizeStatus(seed) {
        let status = seed && seed.status ? seed.status : 'running';
        let phase = (seed && seed.phase) || '';
        if (status === 'check') {
            status = 'running';
            phase = phase || 'check';
        } else if (status === 'handoff' || status === 'continuation') {
            status = 'running';
            phase = phase || 'handoff';
        }
        return { status, phase };
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
        const norm = _normalizeStatus(seed);
        const model = {
            execId,
            taskSummary: seed.task_summary || seed.task || '',
            workflowStep: !!seed.workflow_step,
            stepIndex: Number(seed.step_index) || 0,
            stepName: seed.step_name || '',
            executor: seed.executor || '',
            durationMs: Number(seed.duration_ms) || 0,
            pending: !!seed.pending,
            status: norm.status,
            phase: norm.phase,
            background: !!seed.background,
            // 刷新/切换会话一律折叠；展开只在本次页面内生效
            expanded: false,
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
        if (model.workflowStep) {
            _renderWorkflowHeader(model);
            return;
        }
        const stats = model.stats;
        const active = !isTerminalStatus(model.status);
        const statusText = active
            ? (model.currentTool ? `正在: ${model.currentTool}`
                : (PHASE_TITLES[model.phase] || '运行中'))
            : (model.status === 'failed' ? '失败'
                : model.status === 'stopped' ? '已停止'
                : model.status === 'interrupted' ? '已中断' : '完成');
        let meta = stats
            ? `${stats.iterations} 轮 · ${stats.tool_call_count} 次工具`
            : '统计中…';
        meta += ` · ${statusText}`;
        const task = model.taskSummary || '';
        const title = task || (active ? '任务执行中' : '任务已结束');
        const mode = model.background
            ? '<span class="sa-mode-badge">异步</span>'
            : '<span class="sa-mode-badge sa-mode-sync">同步</span>';
        model.headerEl.innerHTML = `
            <span class="sa-icon">${STATUS_ICONS[active ? (model.phase || model.status) : model.status] || (active ? '🔄' : '📋')}</span>
            ${mode}
            <span class="sa-title" title="${escapeHtml(task)}">${escapeHtml(title.length > 60 ? title.substring(0, 60) + '...' : title)}</span>
            <span class="sa-meta">${escapeHtml(meta)}</span>
            <span class="sa-toggle">${model.expanded ? '▼' : '▶'}</span>
        `;
    }

    function _renderWorkflowHeader(model) {
        const active = !isTerminalStatus(model.status);
        const statusText = active ? '运行中'
            : model.status === 'failed' ? '失败'
            : model.status === 'stopped' ? '已停止' : '完成';
        const icon = active ? '🔄' : (model.status === 'failed' ? '❌'
            : model.status === 'stopped' ? '⏹️'
            : model.status === 'interrupted' ? '⚠️' : '✅');
        const name = model.stepName || model.taskSummary || '步骤';
        const prefix = model.stepIndex ? `${model.stepIndex}. ` : '';
        const mode = model.background
            ? '<span class="sa-mode-badge">异步</span>'
            : '<span class="sa-mode-badge sa-mode-sync">同步</span>';
        const parts = [];
        if (model.executor) parts.push(model.executor);
        if (model.durationMs) parts.push(`${Math.round(model.durationMs / 1000)}s`);
        if (model.stats) {
            parts.push(`${model.stats.tool_call_count} 次工具`);
        }
        parts.push(statusText);
        model.headerEl.innerHTML = `
            <span class="sa-icon">${icon}</span>
            ${mode}
            <span class="sa-title">${escapeHtml(prefix + name)}</span>
            <span class="sa-meta">${escapeHtml(parts.join(' · '))}</span>
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
        // 第 1 轮固定在面板最开头，标「第 1 轮」没有信息量；只给续任轮次（>1）加分隔
        if (!epoch || epoch <= 1) return;
        const div = document.createElement('div');
        div.className = 'sa-epoch-divider';
        div.textContent = `第 ${epoch} 轮`;
        model.bodyEl.appendChild(div);
    }

    function _appendBlock(model, block) {
        if (!model.bodyEl) return;
        const el = _renderBlock(model, block);
        if (el) {
            model.bodyEl.appendChild(el);
            // 展开中的卡片逐块增高：贴底时继续跟随
            if (chatMessages.contains(model.bodyEl)) scrollChatIfNearBottom();
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
        scrollChatIfNearBottom();
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
            scrollChatIfNearBottom();
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
                // 展开中的文字块逐段增高：贴底时继续跟随
                if (chatMessages.contains(model.openBlock.el)) scrollChatIfNearBottom();
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
            scrollChatIfNearBottom();
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

    function _liveContainer() {
        try {
            if (typeof _activeTurnContainers !== 'undefined' && currentSession) {
                const container = _activeTurnContainers.get(currentSession.session_id);
                if (container) return container;
            }
        } catch (e) { /* 回退到 chatMessages */ }
        return chatMessages;
    }

    function _liveModel(execId) {
        let model = MODELS.get(execId);
        if (!model) {
            model = _createModel(execId, { status: 'running' });
            _ensureCard(model, _liveContainer());
        }
        return model;
    }

    function _ensureCard(model, container = null, forceMove = false) {
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

            if (model.workflowStep) card.classList.add('workflow-step');
            model.cardEl = card;
            model.headerEl = header;
            model.detailEl = detail;
            model.bodyEl = body;
        }
        // 历史首次出现时可把运行中卡片从本轮回合容器移回历史位置；
        // 同一 exec_id 的后续引用只更新模型，不再移动。
        const target = container || chatMessages;
        if (target && (forceMove || !model.cardEl.parentNode)) {
            if (model.cardEl.parentNode !== target) target.appendChild(model.cardEl);
        }
        model.cardEl.classList.toggle('workflow-step', !!model.workflowStep);
        model.detailEl.style.display = model.expanded ? 'block' : 'none';
        _renderHeader(model);
        if (model.expanded) {
            if (model.loaded && !model.needsFullReload) _renderDetail(model);
            else _loadExecution(model);
        }
        // 卡片插入会改变消息区高度：贴底时立即跟随，否则视图掉队后
        // 用户随便一滚就会被判成「在翻历史」，自动跟随从此关掉
        if (model.cardEl.parentNode && chatMessages.contains(model.cardEl)) {
            scrollChatIfNearBottom();
        }
        return model.cardEl;
    }

    function toggle(execId) {
        const model = MODELS.get(execId);
        if (!model) return;
        model.expanded = !model.expanded;
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
            if (seed.workflow_step !== undefined) model.workflowStep = !!seed.workflow_step;
            if (seed.step_index !== undefined) model.stepIndex = Number(seed.step_index) || 0;
            if (seed.step_name !== undefined) model.stepName = seed.step_name || '';
            if (seed.executor !== undefined) model.executor = seed.executor || '';
            if (seed.duration_ms !== undefined) model.durationMs = Number(seed.duration_ms) || 0;
            if (seed.pending !== undefined) model.pending = !!seed.pending;
            const norm = _normalizeStatus(seed);
            if ((seed.status || seed.phase)
                && (isTerminalStatus(norm.status) || !isTerminalStatus(model.status))) {
                model.status = norm.status;
                model.phase = norm.phase;
            }
            if (seed.background !== undefined) model.background = !!seed.background;
            const stats = _statsFrom(seed.stats);
            if (stats) model.stats = stats;
        }
        const firstInHistory = !_seen.has(seed.exec_id);
        _seen.add(seed.exec_id);
        _ensureCard(model, container, firstInHistory);
        return model;
    }

    function onStart(e) {
        let model = MODELS.get(e.exec_id);
        if (model) {
            if (e.task_summary) model.taskSummary = e.task_summary;
            if (e.workflow_step !== undefined) model.workflowStep = !!e.workflow_step;
            if (e.step_index !== undefined) model.stepIndex = Number(e.step_index) || 0;
            if (e.step_name !== undefined) model.stepName = e.step_name || '';
            if (e.executor !== undefined) model.executor = e.executor || '';
            if (e.background !== undefined) model.background = !!e.background;
            model.status = 'running';
            model.phase = 'running';
            if (model.workflowStep && model.cardEl) model.cardEl.classList.add('workflow-step');
        } else {
            model = _createModel(e.exec_id, {
                task_summary: e.task_summary || '',
                status: 'running',
                background: !!e.background,
                workflow_step: e.workflow_step,
                step_index: e.step_index,
                step_name: e.step_name,
                executor: e.executor,
            });
        }
        _ensureCard(model, _liveContainer());
    }

    function onProgress(e) {
        let model = MODELS.get(e.exec_id);
        if (!model) {
            model = _createModel(e.exec_id, { status: 'running' });
            _ensureCard(model, _liveContainer());
        }
        const stats = _statsFrom(e.stats);
        if (stats) model.stats = stats;
        // 终态不可被迟到的进度事件回退；续任由 agent_epoch_started 显式恢复 running
        if (e.status && (!isTerminalStatus(model.status) || e.status !== 'running')) {
            model.status = e.status;
        }
        if (e.phase !== undefined && !isTerminalStatus(model.status)) {
            model.phase = e.phase || '';
        }
        if (e.epoch) model.epoch = e.epoch;
        model.currentTool = e.current_tool || '';
        _renderHeader(model);
    }

    function onEpochStarted(e) {
        let model = MODELS.get(e.exec_id);
        if (!model) {
            model = _createModel(e.exec_id, { status: 'running' });
            _ensureCard(model, _liveContainer());
        }
        const stats = _statsFrom(e.stats);
        if (stats) model.stats = stats;
        model.epoch = e.epoch || model.epoch;
        model.maxEpochs = e.max_epochs || model.maxEpochs;
        // 续任 epoch 开始：AgentRun 仍在进行，状态必须回到 running
        if (isTerminalStatus(model.status)) {
            model.status = 'running';
        }
        model.phase = 'handoff';
        _renderHeader(model);
    }

    function onComplete(e) {
        const model = MODELS.get(e.exec_id) || _createModel(e.exec_id, {});
        if (e.duration_ms !== undefined) model.durationMs = Number(e.duration_ms) || 0;
        if (e.workflow_step !== undefined) model.workflowStep = !!e.workflow_step;
        if (e.step_index !== undefined) model.stepIndex = Number(e.step_index) || 0;
        if (e.step_name) model.stepName = e.step_name;
        if (e.executor) model.executor = e.executor;
        // 只有终态才能写 finished：非终态（如 running/handoff）一律忽略
        if (isTerminalStatus(e.status)) {
            const rank = { completed: 0, stopped: 1, error: 2, failed: 2, timeout: 2 };
            const cur = rank[model.status] !== undefined ? rank[model.status] : -1;
            const next = rank[e.status];
            if (next > cur || !isTerminalStatus(model.status)) {
                model.status = e.status;
                model.phase = 'done';
            }
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
                _updateOpenText(_liveModel(e.exec_id), 'text', e.content || '');
                return true;
            case 'thinking':
                _updateOpenText(_liveModel(e.exec_id), 'thinking', e.content || '');
                return true;
            case 'tool_use':
                _onToolUse(_liveModel(e.exec_id), e);
                return true;
            case 'tool_result':
                _onToolResult(_liveModel(e.exec_id), e);
                return true;
            case 'tool_output':
                _onToolOutput(_liveModel(e.exec_id), e);
                return true;
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
            if (_seen.has(execId) || !isTerminalStatus(model.status)) continue;
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
            // 拉取期间用户可能已切走：旧会话的执行记录不得挂到当前会话界面上
            if (!currentWorkspace || currentWorkspace.uuid !== ws) return;
            if (!currentSession || currentSession.session_id !== sid) return;
            (data.executions || []).forEach(log => {
                const meta = log.metadata || {};
                // 归一化旧数据：status 里可能残留 check/handoff 阶段值
                const norm = _normalizeStatus({ status: meta.status, phase: meta.phase });
                if (isTerminalStatus(norm.status)) return;
                let model = MODELS.get(log.exec_id);
                if (!model) {
                    model = _createModel(log.exec_id, {
                        task_summary: log.task || log.summary || '',
                        status: norm.status,
                        phase: norm.phase,
                        stats: log.stats || meta.stats,
                        epoch: meta.epoch || 0,
                        max_epochs: meta.max_epochs || 0,
                    });
                } else {
                    model.status = norm.status;
                    if (norm.phase) model.phase = norm.phase;
                    const stats = _statsFrom(log.stats || meta.stats);
                    if (stats) model.stats = stats;
                }
                _ensureCard(model, _liveContainer());
            });
            // 工作流结果会话：步骤不在 agent exec 日志里；刷新时按工作流 state
            // 恢复正在运行步骤的卡片，避免「刷新几次卡一会有一会没有」。
            const wfId = currentSession
                && currentSession.metadata
                && currentSession.metadata.run_results
                && currentSession.metadata.run_results.workflow_id;
            if (wfId) {
                try {
                    const wfResp = await fetch(`/api/workspaces/${ws}/workflows/${wfId}`);
                    if (!currentSession || currentSession.session_id !== sid) return;
                    if (wfResp.ok) {
                        const detail = await wfResp.json();
                        const wf = detail.workflow || {};
                        const st = detail.state || {};
                        // 只给「本次运行」的结果会话补挂；旧结果会话不能拿当前 state 卡片
                        if (st.results_session_id && st.results_session_id !== sid) return;
                        (wf.steps || []).forEach((step, index) => {
                            const item = (st.items || {})[step.id];
                            if (!item || item.status === 'pending') return;
                            const status = item.status === 'done' ? 'completed' : item.status;
                            // 历史已经渲染过同一会话的步骤卡：不要再补一张
                            if (item.session_id && _seen.has(item.session_id)) return;
                            let model = item.session_id ? MODELS.get(item.session_id) : null;
                            if (!model) {
                                // 先前的 tool_use 占位卡（无 tool_result）直接接管，避免重复
                                for (const [key, candidate] of [...MODELS.entries()]) {
                                    if (candidate.workflowStep && candidate.pending
                                            && candidate.stepIndex === index + 1) {
                                        MODELS.delete(key);
                                        if (item.session_id) {
                                            candidate.execId = item.session_id;
                                            if (candidate.cardEl) candidate.cardEl.dataset.execId = item.session_id;
                                            MODELS.set(item.session_id, candidate);
                                        } else {
                                            MODELS.set(key, candidate);
                                        }
                                        model = candidate;
                                        break;
                                    }
                                }
                            }
                            if (!model) {
                                const key = item.session_id || `workflow_step_${step.id}`;
                                model = _createModel(key, {
                                    task_summary: `第 ${index + 1} 步：${step.name}`,
                                    status,
                                    workflow_step: true,
                                    step_index: index + 1,
                                    step_name: step.name,
                                    executor: step.executor,
                                    stats: item.stats,
                                });
                            }
                            model.pending = false;
                            model.workflowStep = true;
                            model.stepIndex = index + 1;
                            model.stepName = step.name;
                            model.executor = step.executor;
                            model.status = status;
                            model.durationMs = Number(item.duration_ms) || model.durationMs || 0;
                            if (item.artifact) model.artifact = item.artifact;
                            const stats = _statsFrom(item.stats);
                            if (stats) model.stats = stats;
                            _ensureCard(model, _liveContainer());
                        });
                    }
                } catch (e) { /* ignore */ }
            }
        } catch (e) { /* 事件流 agent_start 会兜底建卡 */ }
    }

    async function alignAfterReconnect() {
        for (const model of MODELS.values()) {
            if (model.expanded && !isTerminalStatus(model.status)) {
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
    };
})();

window.AgentCardStore = AgentCardStore;
