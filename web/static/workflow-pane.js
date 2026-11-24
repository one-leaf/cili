/* workflow-pane.js — 侧栏工作流条目（与会话合并渲染）+ 中间栏工作流面板 + 聊天双标签 */
(function () {
    'use strict';

    const KEYS = {
        paneWidth: 'cili_workflows_pane_width',
    };
    const PANE_DEFAULT_WIDTH = 380;
    const PANE_MIN_WIDTH = 260;
    const PANE_MIN_CHAT_WIDTH = 320;

    const state = {
        workspaceUuid: '',
        workflows: [],
        currentWorkflowId: '',
        currentWorkflow: null,
        currentState: null,
        activeTab: 'edit',
        autoSwitchedRunId: '',
        polling: null,
        bound: false,
        drafts: {},
        dragIndex: -1,
    };

    const $ = (id) => document.getElementById(id);

    const PLACEHOLDER_DEFAULT = '输入消息... (Shift+Enter 换行, Ctrl+V 粘贴文件)';
    const PLACEHOLDER_EDIT = '告诉 AI 如何修改工作流…';
    const PLACEHOLDER_RESULTS = '问关于这次运行的问题，或做临时讨论…';

    function setComposerPlaceholder(text) {
        try {
            if (chatInput) chatInput.placeholder = text;
        } catch (e) { /* ignore */ }
    }

    function esc(value) {
        if (typeof escapeHtml === 'function') return escapeHtml(String(value == null ? '' : value));
        return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    async function api(path, options) {
        const response = await fetch(path, options);
        let data = {};
        try { data = await response.json(); } catch (e) { data = {}; }
        if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
        return data;
    }

    function toast(text) {
        if (typeof showToast === 'function') showToast(text);
        else console.log(text);
    }

    function ws() { return encodeURIComponent(state.workspaceUuid); }

    function workflowStatus(item) {
        return (item && item.state && item.state.status) || 'idle';
    }

    const ICONS = {
        idle: '○', running: '●', done: '✓', completed: '✓',
        failed: '✕', stopped: '◼', interrupted: '⚠', pending: '○',
    };

    function icon(status) { return ICONS[status] || '○'; }
    function iconClass(status) { return 'wf-status-' + (status || 'pending'); }

    // ── 初始化 ─────────────────────────────────────────────

    function init(workspaceUuid) {
        if (state.workspaceUuid === workspaceUuid && state.bound) {
            loadWorkflows();
            return;
        }
        reset(true);
        state.workspaceUuid = workspaceUuid || '';
        bindOnce();
        loadWorkflows();
    }

    function reset(keepWorkspace) {
        stopPolling();
        state.workflows = [];
        state.currentWorkflowId = '';
        state.currentWorkflow = null;
        state.currentState = null;
        state.activeTab = 'edit';
        state.drafts = {};
        if (!keepWorkspace) state.workspaceUuid = '';
        close();
    }

    function bindOnce() {
        if (state.bound) return;
        state.bound = true;

        $('chat-workflow-tabs')?.addEventListener('click', (event) => {
            const tab = event.target.closest('.wf-chat-tab');
            if (tab) switchTab(tab.dataset.tab);
        });

        window.addEventListener('cili:turn-complete', (event) => {
            const sid = event.detail?.session_id || '';
            onTurnComplete(sid);
        });

        initPaneResize();
    }

    // ── 工作流面板宽度（竖向分隔条） ─────────────────────────

    function readPaneWidth() {
        try {
            const value = parseInt(localStorage.getItem(KEYS.paneWidth) || '', 10);
            if (value > 0) return value;
        } catch (e) { /* ignore */ }
        return PANE_DEFAULT_WIDTH;
    }

    function paneMaxWidth() {
        const sidebar = document.querySelector('.sidebar');
        const sidebarHandle = document.querySelector('.sidebar-resize-handle');
        const sidebarWidth = sidebar ? sidebar.getBoundingClientRect().width : 0;
        const sidebarHandleWidth = sidebarHandle ? sidebarHandle.getBoundingClientRect().width : 0;
        const handle = $('workflow-resize-handle');
        const handleWidth = handle ? handle.getBoundingClientRect().width : 0;
        const available = Math.floor(window.innerWidth - sidebarWidth -
            sidebarHandleWidth - handleWidth - PANE_MIN_CHAT_WIDTH);
        return Math.max(PANE_MIN_WIDTH, available);
    }

    function clampPaneWidth(width) {
        return Math.max(PANE_MIN_WIDTH, Math.min(width, paneMaxWidth()));
    }

    function applyPaneWidth() {
        const pane = $('workflow-pane');
        if (!pane) return;
        pane.style.width = clampPaneWidth(readPaneWidth()) + 'px';
    }

    function initPaneResize() {
        const handle = $('workflow-resize-handle');
        const pane = $('workflow-pane');
        if (!handle || !pane) return;
        let dragging = false;
        let startX = 0;
        let startWidth = 0;

        handle.addEventListener('mousedown', (event) => {
            if (pane.hidden) return;
            event.preventDefault();
            dragging = true;
            startX = event.clientX;
            startWidth = pane.getBoundingClientRect().width;
            handle.classList.add('dragging');
            document.body.style.cursor = 'ew-resize';
            document.body.style.userSelect = 'none';
        });
        document.addEventListener('mousemove', (event) => {
            if (!dragging) return;
            pane.style.width = clampPaneWidth(startWidth + (event.clientX - startX)) + 'px';
        });
        document.addEventListener('mouseup', () => {
            if (!dragging) return;
            dragging = false;
            handle.classList.remove('dragging');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            const width = Math.round(pane.getBoundingClientRect().width);
            try { localStorage.setItem(KEYS.paneWidth, String(width)); } catch (e) { /* ignore */ }
        });
        handle.addEventListener('dblclick', () => {
            pane.style.width = `${PANE_DEFAULT_WIDTH}px`;
            try { localStorage.setItem(KEYS.paneWidth, String(PANE_DEFAULT_WIDTH)); } catch (e) { /* ignore */ }
        });
        window.addEventListener('resize', () => {
            if (!pane.hidden) applyPaneWidth();
        });
    }

    // ── 工作流列表 ───────────────────────────────────────────

    async function loadWorkflows() {
        if (!state.workspaceUuid) return;
        try {
            const data = await api(`/api/workspaces/${ws()}/workflows`);
            state.workflows = data.workflows || [];
        } catch (error) {
            console.warn('load workflows failed', error);
            state.workflows = [];
        }
        renderSidebar();
        const running = state.workflows.some((item) => workflowStatus(item) === 'running');
        if (running) startPolling(); else stopPolling();
    }

    function workflowName(item) { return (item.workflow && item.workflow.name) || '未命名工作流'; }

    function progressText(item) {
        const st = item.state || {};
        const total = (item.workflow?.steps || []).length;
        if (st.status === 'running') return `${Math.min(total, (st.current_index || 0) + 1)}/${total}`;
        return '';
    }

    // 会话与工作流共用一个列表容器：渲染交给 app.js 的合并渲染器
    function renderSidebar() {
        if (typeof window.renderSidebarList === 'function') window.renderSidebarList();
    }

    // 供合并渲染器读取的侧栏条目（会话/工作流按 updated_at 混排）
    function getSidebarEntries() {
        return state.workflows.map((item) => ({
            id: item.workflow.id,
            name: workflowName(item),
            hidden: !!item.workflow.hidden,
            status: workflowStatus(item),
            progress: progressText(item),
            updated_at: item.workflow.updated_at || '',
        }));
    }

    async function setHidden(workflowId, hidden) {
        try {
            await api(`/api/workspaces/${ws()}/workflows/${workflowId}/hidden`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ hidden }),
            });
            await loadWorkflows();
        } catch (error) { toast('操作失败：' + error.message); }
    }

    async function exportWorkflow(workflowId) {
        try {
            const data = await api(`/api/workspaces/${ws()}/workflows/${workflowId}`);
            const wf = data.workflow || {};
            const portable = {
                format: 'cili-workflow',
                version: 1,
                exported_at: new Date().toISOString(),
                workflow: {
                    name: wf.name || 'workflow',
                    description: wf.description || '',
                    inputs: wf.inputs || [],
                    steps: wf.steps || [],
                },
            };
            const blob = new Blob([JSON.stringify(portable, null, 2)],
                                  { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${String(wf.name || 'workflow').replace(/[<>:"/\|?*]/g, '_')}.json`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
            toast('工作流已导出');
        } catch (error) {
            toast('导出失败：' + (error.message || '未知错误'));
        }
    }

    async function importWorkflowFile(file) {
        try {
            const text = await file.text();
            const data = JSON.parse(text);
            const src = data && data.workflow ? data.workflow : data;
            if (!src || typeof src !== 'object' || !src.name
                    || !Array.isArray(src.steps) || !src.steps.length) {
                throw new Error('不是有效的工作流文件');
            }
            const created = await api(`/api/workspaces/${ws()}/workflows`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: src.name,
                    description: src.description || '',
                    inputs: src.inputs || [],
                    steps: src.steps,
                }),
            });
            const workflow = created.workflow || {};
            toast(`已导入工作流「${workflow.name || src.name}」`);
            await loadWorkflows();
            if (workflow.id) await openWorkflowById(workflow.id);
        } catch (error) {
            toast('导入失败：' + (error.message || '未知错误'));
        }
    }

    async function deleteWorkflow(workflowId) {
        const item = state.workflows.find((entry) => entry.workflow.id === workflowId);
        const name = item ? workflowName(item) : workflowId;
        if (!confirm(`删除工作流「${name}」？已产生的会话与文件不会被删除。`)) return;
        try {
            await api(`/api/workspaces/${ws()}/workflows/${workflowId}`, { method: 'DELETE' });
            if (state.currentWorkflowId === workflowId) close();
            loadWorkflows();
        } catch (error) {
            toast('删除失败：' + error.message);
        }
    }

    // ── 打开工作流 / 会话 ────────────────────────────────────

    async function newWorkflow() {
        if (!state.workspaceUuid) return;
        try {
            const data = await api(`/api/workspaces/${ws()}/workflows/panel-sessions`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
            });
            if (data.session_id) await loadSession(data.session_id);
        } catch (error) { toast('新建工作流失败：' + error.message); }
    }

    function openSessionInNewWindow(sessionId) {
        // 与「分享会话」一致：打开只读分享页，而不是完整应用窗口
        const url = `${window.location.origin}/s/${state.workspaceUuid}/${sessionId}`;
        window.open(url, '_blank', 'noopener,noreferrer');
    }

    async function openWorkflowById(workflowId) {
        if (!state.workspaceUuid) return;
        try {
            const data = await api(`/api/workspaces/${ws()}/workflows/panel-sessions`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ workflow_id: workflowId }),
            });
            if (data.session_id) await loadSession(data.session_id);
        } catch (error) { toast('打开工作流失败：' + error.message); }
    }

    function openForSession(session) {
        const metadata = session?.metadata || {};
        if (metadata.workflow_panel) {
            const workflowId = String(metadata.workflow_panel.workflow_id || '');
            if (workflowId) open(workflowId, 'edit');
            else openNew();
            return;
        }
        if (metadata.run_results && metadata.run_results.workflow_id) {
            open(String(metadata.run_results.workflow_id), 'results');
            return;
        }
        close();
    }

    async function open(workflowId, tab) {
        state.currentWorkflowId = workflowId;
        state.activeTab = tab || 'edit';
        try {
            const data = await api(`/api/workspaces/${ws()}/workflows/${workflowId}`);
            state.currentWorkflow = data.workflow;
            state.currentState = data.state;
            showPane();
            setComposerPlaceholder(state.activeTab === 'results'
                ? PLACEHOLDER_RESULTS : PLACEHOLDER_EDIT);
            renderPanel();
            renderTabs();
            renderSidebar();
            // 空会话欢迎块：工作流数据到位后示例可带上具体步骤名
            try {
                const meta = (typeof currentSession !== 'undefined'
                    && currentSession && currentSession.metadata) || {};
                const boundId = (meta.run_results && meta.run_results.workflow_id)
                    || (meta.workflow_panel && meta.workflow_panel.workflow_id) || '';
                if (boundId === workflowId && typeof newSessionWelcomeHtml === 'function'
                        && typeof chatMessages !== 'undefined'
                        && chatMessages.querySelector('.welcome-message')) {
                    chatMessages.innerHTML = newSessionWelcomeHtml();
                }
            } catch (e) { /* ignore */ }
            if (state.currentState?.status === 'running') {
                startPolling();
                maybeAutoSwitchResults();
            }
        } catch (error) {
            state.currentWorkflow = null;
            state.currentState = null;
            showPane();
            const cards = $('wf-cards');
            if (cards) cards.innerHTML = `<div class="wf-sidebar-empty">工作流已不存在或加载失败：${esc(error.message)}</div>`;
            renderTabs();
        }
    }

    function openNew() {
        state.currentWorkflowId = '';
        state.currentWorkflow = null;
        state.currentState = null;
        state.activeTab = 'edit';
        showPane();
        setComposerPlaceholder(PLACEHOLDER_EDIT);
        const title = $('wf-workflow-title');
        if (title) title.textContent = '新建工作流';
        const badge = $('wf-workflow-badge');
        if (badge) badge.hidden = true;
        const progress = $('wf-progress-text');
        if (progress) progress.hidden = true;
        const actions = $('wf-workflow-actions');
        if (actions) actions.innerHTML = '';
        const cards = $('wf-cards');
        if (cards) {
            cards.innerHTML = '<div class="wf-sidebar-empty">描述你的目标，AI 会在对话里生成步骤。</div>';
        }
        hideTabs();
        renderSidebar();
        setTimeout(() => { try { chatInput?.focus(); } catch (e) { /* ignore */ } }, 50);
    }

    function showPane() {
        const pane = $('workflow-pane');
        if (pane) pane.hidden = false;
        const handle = $('workflow-resize-handle');
        if (handle) handle.hidden = false;
        applyPaneWidth();
        const preview = $('preview-pane');
        if (preview && !preview.hidden) {
            state.previewWasOpen = true;
            try {
                const ctx = window.FileTree?.getEditorContext?.();
                state.previewPath = (ctx && ctx.path) || '';
            } catch (e) { state.previewPath = ''; }
            preview.hidden = true;
        }
    }

    function close(restorePreview = true) {
        const pane = $('workflow-pane');
        if (pane) pane.hidden = true;
        const handle = $('workflow-resize-handle');
        if (handle) handle.hidden = true;
        hideTabs();
        setComposerPlaceholder(PLACEHOLDER_DEFAULT);
        const preview = $('preview-pane');
        if (restorePreview && preview && state.previewWasOpen) {
            state.previewWasOpen = false;
            const path = state.previewPath;
            state.previewPath = '';
            if (path && window.FileTree?.openPreview) {
                try { FileTree.openPreview(path, { restore: true }); } catch (e) { /* ignore */ }
            }
        }
        state.currentWorkflowId = '';
        state.currentWorkflow = null;
        state.currentState = null;
        renderSidebar();
    }

    // ── 标签 ───────────────────────────────────────────────

    function showTabs() {
        const tabs = $('chat-workflow-tabs');
        if (tabs) tabs.hidden = false;
    }

    function hideTabs() {
        const tabs = $('chat-workflow-tabs');
        if (!tabs) return;
        tabs.hidden = true;
        tabs.querySelectorAll('.wf-chat-tab').forEach((tab) => {
            tab.classList.toggle('active', tab.dataset.tab === 'edit');
        });
    }

    function renderTabs() {
        const tabs = $('chat-workflow-tabs');
        if (!tabs) return;
        if (!state.currentWorkflow) { hideTabs(); return; }
        tabs.hidden = false;
        tabs.querySelectorAll('.wf-chat-tab').forEach((tab) => {
            tab.classList.toggle('active', tab.dataset.tab === state.activeTab);
        });
        const st = state.currentState || {};
        const progress = $('wf-tab-progress');
        if (progress) {
            const total = (state.currentWorkflow.steps || []).length;
            progress.hidden = st.status !== 'running';
            progress.textContent = st.status === 'running'
                ? `${Math.min(total, (st.current_index || 0) + 1)}/${total}` : '';
        }
    }

    function switchTab(tab) {
        if (!state.currentWorkflow || !state.currentWorkflowId) return;
        if (tab === state.activeTab && currentSession && sessionMatches(tab)) return;
        rememberDraft();
        state.activeTab = tab;
        renderTabs();
        const target = tab === 'results'
            ? ensureResultsThenLoad()
            : loadPanelSession();
        return target;
    }

    function rememberDraft() {
        try {
            if (currentSession && chatInput) state.drafts[currentSession.session_id] = chatInput.value;
        } catch (e) { /* ignore */ }
    }

    function restoreDraft(sessionId) {
        try {
            if (chatInput) {
                chatInput.value = state.drafts[sessionId] || '';
                chatInput.dispatchEvent(new Event('input'));
            }
        } catch (e) { /* ignore */ }
    }

    function sessionMatches(tab) {
        const st = state.currentState || {};
        const sid = tab === 'results' ? st.results_session_id : st.panel_session_id;
        return currentSession && currentSession.session_id === sid;
    }

    async function loadPanelSession() {
        const st = state.currentState || {};
        const sid = st.panel_session_id;
        if (!sid) return;
        rememberDraft();
        await loadSession(sid);
        restoreDraft(sid);
    }

    async function ensureResultsThenLoad() {
        const st = state.currentState || {};
        let sid = st.results_session_id;
        if (!sid) {
            try {
                const data = await api(`/api/workspaces/${ws()}/workflows/${state.currentWorkflowId}/results-session`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: '{}',
                });
                sid = data.session_id;
                st.results_session_id = sid;
            } catch (error) {
                toast('打开运行结果失败：' + error.message);
                return;
            }
        }
        rememberDraft();
        await loadSession(sid);
        restoreDraft(sid);
        renderTabs();
    }

    // ── 运行 / 停止 ────────────────────────────────────────

    async function launchRun(workflowId, inputs) {
        // 结果会话由 runner 在本次运行开始时新建（只展示最近一次运行）
        await api(`/api/workspaces/${ws()}/workflows/${workflowId}/run`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ inputs: inputs || {} }),
        });
        toast('已开始运行');
        await loadWorkflows();
        if (state.currentWorkflowId === workflowId) {
            await open(workflowId, 'results');
            await switchTab('results');
        }
    }

    async function startRunFor(workflowId) {
        if (!workflowId) return;
        const item = state.workflows.find((entry) => entry.workflow.id === workflowId);
        const workflow = item ? item.workflow : null;
        if (!workflow) {
            toast('工作流不存在或尚未加载');
            return;
        }
        const specs = workflow.inputs || [];
        if (!specs.length) {
            try { await launchRun(workflowId, {}); }
            catch (error) { toast('运行失败：' + error.message); }
            return;
        }
        openRunForm(workflow, workflowId, item);
    }

    function openRunForm(workflow, workflowId, item) {
        const last = (state.currentWorkflowId === workflowId
            ? (state.currentState || {}).run_inputs
            : (item && item.state && item.state.run_inputs)) || {};
        const modal = document.createElement('div');
        modal.className = 'modal';
        const fields = (workflow.inputs || []).map((spec) => {
            const value = last[spec.key] !== undefined ? last[spec.key] : (spec.default || '');
            const hint = spec.description
                ? `<div class="wf-edit-hint">${esc(spec.description)}</div>` : '';
            const input = spec.type === 'textarea'
                ? `<textarea class="wf-run-input" data-key="${esc(spec.key)}" rows="3">${esc(value)}</textarea>`
                : `<input type="text" class="wf-run-input" data-key="${esc(spec.key)}" value="${esc(value)}">`;
            return `<label class="wf-edit-field">${esc(spec.label)}${spec.required ? ' *' : ''}${hint}${input}</label>`;
        }).join('');
        modal.innerHTML = `
            <div class="modal-backdrop"></div>
            <div class="modal-content">
                <div class="modal-header">
                    <h2>运行工作流</h2>
                    <button type="button" class="modal-close" aria-label="关闭">×</button>
                </div>
                <div class="modal-body">
                    <div class="wf-edit-hint">「${esc(workflow.name)}」需要以下运行参数。</div>
                    <div class="wf-run-form">${fields}</div>
                </div>
                <div class="modal-footer">
                    <span class="wf-edit-status"></span>
                    <span class="wf-edit-footer-actions">
                        <button type="button" class="btn wf-run-cancel">取消</button>
                        <button type="button" class="btn btn-primary wf-run-start">开始运行</button>
                    </span>
                </div>
            </div>`;
        const close = () => {
            modal.remove();
            document.removeEventListener('keydown', onKey);
        };
        const onKey = (event) => { if (event.key === 'Escape') close(); };
        document.addEventListener('keydown', onKey);
        modal.querySelector('.modal-backdrop').addEventListener('click', close);
        modal.querySelector('.modal-close').addEventListener('click', close);
        modal.querySelector('.wf-run-cancel').addEventListener('click', close);
        modal.querySelector('.wf-run-start').addEventListener('click', async () => {
            const status = modal.querySelector('.wf-edit-status');
            const values = {};
            for (const spec of workflow.inputs) {
                const field = modal.querySelector(`.wf-run-input[data-key="${spec.key}"]`);
                const value = field ? field.value.trim() : '';
                if (spec.required && !value) {
                    status.textContent = `「${spec.label}」不能为空`;
                    if (field) field.focus();
                    return;
                }
                if (value) values[spec.key] = value;
            }
            const btn = modal.querySelector('.wf-run-start');
            btn.disabled = true;
            status.textContent = '启动中…';
            try {
                await launchRun(workflowId, values);
                close();
            } catch (error) {
                btn.disabled = false;
                status.textContent = '运行失败：' + (error.message || '未知错误');
            }
        });
        document.body.appendChild(modal);
        const first = modal.querySelector('.wf-run-input');
        if (first) first.focus();
    }

    async function stopRunFor(workflowId) {
        try {
            await api(`/api/workspaces/${ws()}/workflows/${workflowId}/stop`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
            });
            toast('已请求停止');
            loadWorkflows();
        } catch (error) { toast('停止失败：' + error.message); }
    }

    // ── 面板渲染 ───────────────────────────────────────────

    function statusLabel(status) {
        return {
            idle: '未运行', running: '运行中', completed: '已完成',
            pending: '等待', done: '完成', failed: '失败',
            stopped: '已停止', interrupted: '已中断',
        }[status] || status || '未运行';
    }

    function badgeClass(status) {
        if (status === 'completed') return 'ok';
        if (status === 'failed') return 'failed';
        if (status === 'running') return 'running';
        if (status === 'interrupted') return 'interrupted';
        return '';
    }

    function renderPanel() {
        const workflow = state.currentWorkflow;
        const st = state.currentState || {};
        if (!workflow) return;
        const title = $('wf-workflow-title');
        if (title) title.textContent = workflow.name || '工作流';
        const badge = $('wf-workflow-badge');
        if (badge) {
            badge.hidden = false;
            badge.textContent = statusLabel(st.status);
            badge.className = 'wf-badge ' + badgeClass(st.status);
        }
        const progress = $('wf-progress-text');
        if (progress) {
            const total = workflow.steps.length;
            progress.hidden = st.status !== 'running';
            progress.textContent = st.status === 'running'
                ? `${Math.min(total, (st.current_index || 0) + 1)}/${total}` : '';
        }
        const actions = $('wf-workflow-actions');
        if (actions) {
            if (st.status === 'running') {
                actions.innerHTML = '<button class="btn btn-small" data-act="stop">停止</button>' +
                    '<button class="btn btn-small" data-act="edit-workflow">编辑</button>' +
                    '<button class="btn btn-small" data-act="history">历史</button>' +
                    '<button class="btn btn-small btn-danger" data-act="delete">删除</button>';
            } else {
                actions.innerHTML = '<button class="btn btn-small" data-act="run">运行</button>' +
                    '<button class="btn btn-small" data-act="edit-workflow">编辑</button>' +
                    '<button class="btn btn-small" data-act="history">历史</button>' +
                    '<button class="btn btn-small btn-danger" data-act="delete">删除</button>';
            }
            actions.querySelectorAll('[data-act]').forEach((btn) => {
                btn.addEventListener('click', () => {
                    const act = btn.dataset.act;
                    if (act === 'run') startRunFor(workflow.id);
                    else if (act === 'stop') stopRunFor(workflow.id);
                    else if (act === 'edit-workflow') openWorkflowEdit(workflow);
                    else if (act === 'history') openRunHistory(workflow);
                    else if (act === 'delete') deleteWorkflow(workflow.id);
                });
            });
        }
        renderCards();
    }

    function indexMap(workflow) {
        const map = {};
        workflow.steps.forEach((step, index) => { map[step.id] = index; });
        return map;
    }

    function renderCards() {
        const container = $('wf-cards');
        const workflow = state.currentWorkflow;
        if (!container || !workflow) return;
        if (!workflow.steps.length) {
            container.innerHTML = '<div class="wf-sidebar-empty">描述你的目标，AI 会在对话里生成步骤。</div>';
            return;
        }
        const st = state.currentState || {};
        const items = st.items || {};
        const byId = indexMap(workflow);
        container.innerHTML = workflow.steps.map((stepDef, index) => {
            const run = items[stepDef.id] || { status: 'pending' };
            const status = run.status || 'pending';
            const sources = (stepDef.sources || []).map((sid) => {
                const i = byId[sid];
                const name = i === undefined ? sid : workflow.steps[i].name;
                return `<span class="wf-chip">${i === undefined ? '' : (i + 1) + ' '}${esc(name)}</span>`;
            }).join('') || '<span class="wf-chip">无前置</span>';
            const duration = run.duration_ms
                ? `<span>${Math.round(run.duration_ms / 1000)}s</span>` : '';
            const currentTool = status === 'running' && run.current_tool
                ? `<div class="wf-card-tool">当前：${esc(run.current_tool)}</div>` : '';
            const openBtn = run.session_id
                ? `<button data-act="open" data-session="${esc(run.session_id)}" title="打开会话">会话</button>` : '';
            const repairBtn = status === 'failed'
                ? `<button data-act="repair">修复</button>` : '';
            return `<div class="wf-card ${status}" data-index="${index}" data-step="${esc(stepDef.id)}">
                <div class="wf-card-head">
                    <span class="wf-card-drag" draggable="true" title="拖动调整顺序">⠿</span>
                    <span class="wf-card-idx">${index + 1}</span>
                    <span class="wf-card-name" title="${esc(stepDef.name)}">${esc(stepDef.name)}</span>
                    <span class="wf-card-status ${iconClass(status)}">${icon(status)}</span>
                </div>
                ${currentTool}
                <div class="wf-card-req">${esc(stepDef.requirement)}</div>
                <div class="wf-card-foot">
                    ${sources}${duration}
                    <span class="wf-card-actions">
                        ${repairBtn}
                        <button data-act="manual-edit">编辑</button>
                        <button data-act="edit">在对话中修改</button>
                        ${openBtn}
                    </span>
                </div>
            </div>`;
        }).join('');
        container.querySelectorAll('.wf-card').forEach((card) => {
            const index = Number(card.dataset.index);
            card.addEventListener('click', (event) => {
                const button = event.target.closest('button[data-act]');
                if (button) {
                    event.stopPropagation();
                    const act = button.dataset.act;
                    if (act === 'manual-edit') openManualEdit(index);
                    else if (act === 'edit') fillComposer(`修改第 ${index + 1} 步：`);
                    else if (act === 'repair') fillComposer(`修复第 ${index + 1} 步：`);
                    else if (act === 'open' && button.dataset.session) openSessionInNewWindow(button.dataset.session);
                    return;
                }
                card.classList.toggle('expanded');
            });

            const handle = card.querySelector('.wf-card-drag');
            if (handle) {
                handle.addEventListener('click', (event) => event.stopPropagation());
                handle.addEventListener('dragstart', (event) => {
                    if (st.status === 'running') { event.preventDefault(); return; }
                    state.dragIndex = index;
                    card.classList.add('dragging');
                    event.dataTransfer.effectAllowed = 'move';
                    try { event.dataTransfer.setData('text/plain', String(index)); } catch (e) { /* ignore */ }
                });
                handle.addEventListener('dragend', () => {
                    state.dragIndex = -1;
                    container.querySelectorAll('.wf-card').forEach((c) => {
                        c.classList.remove('dragging', 'drag-over-top', 'drag-over-bottom');
                    });
                });
            }
            const clearDragMarks = () => card.classList.remove('drag-over-top', 'drag-over-bottom');
            card.addEventListener('dragover', (event) => {
                if (state.dragIndex < 0 || state.dragIndex === index) return;
                event.preventDefault();
                event.dataTransfer.dropEffect = 'move';
                const rect = card.getBoundingClientRect();
                const after = event.clientY > rect.top + rect.height / 2;
                card.classList.toggle('drag-over-top', !after);
                card.classList.toggle('drag-over-bottom', after);
            });
            card.addEventListener('dragleave', clearDragMarks);
            card.addEventListener('drop', async (event) => {
                if (state.dragIndex < 0 || state.dragIndex === index) return;
                event.preventDefault();
                const rect = card.getBoundingClientRect();
                const after = event.clientY > rect.top + rect.height / 2;
                const dropIndex = index + (after ? 1 : 0);
                const from = state.dragIndex;
                state.dragIndex = -1;
                clearDragMarks();
                await moveStep(from, dropIndex);
            });
        });
    }

    async function moveStep(from, to) {
        if (from === to || from < 0) return;
        const workflow = state.currentWorkflow;
        if (!workflow) return;
        if ((state.currentState || {}).status === 'running') {
            toast('工作流正在运行，无法调整顺序');
            return;
        }
        const steps = workflow.steps.slice();
        const [moved] = steps.splice(from, 1);
        steps.splice(to, 0, moved);
        const pos = new Map(steps.map((s, i) => [s.id, i]));
        for (let i = 0; i < steps.length; i++) {
            for (const src of steps[i].sources) {
                const srcIndex = pos.get(src);
                if (srcIndex === undefined || srcIndex >= i) {
                    const srcName = (steps[srcIndex] || workflow.steps.find(s => s.id === src) || {}).name || src;
                    toast(`不能移动：「${steps[i].name}」依赖「${srcName}」，必须排在其后`);
                    renderCards();
                    return;
                }
            }
        }
        try {
            await api(`/api/workspaces/${ws()}/workflows/${workflow.id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: workflow.name, description: workflow.description, inputs: workflow.inputs || [], steps }),
            });
            await open(workflow.id, state.activeTab);
            toast('步骤顺序已更新');
        } catch (error) {
            toast('调整顺序失败：' + (error.message || '未知错误'));
            renderCards();
        }
    }

    function openRunHistory(workflow) {
        const modal = document.createElement('div');
        modal.className = 'modal';
        modal.innerHTML = `
            <div class="modal-backdrop"></div>
            <div class="modal-content wf-edit-modal">
                <div class="modal-header">
                    <h2>运行历史 · ${esc(workflow.name)}</h2>
                    <button type="button" class="modal-close" aria-label="关闭">×</button>
                </div>
                <div class="modal-body">
                    <div class="wf-history-list">加载中…</div>
                    <div class="wf-history-detail" hidden></div>
                </div>
                <div class="modal-footer">
                    <span class="wf-edit-status"></span>
                    <span class="wf-edit-footer-actions">
                        <button type="button" class="btn wf-history-close">关闭</button>
                    </span>
                </div>
            </div>`;
        const list = modal.querySelector('.wf-history-list');
        const detail = modal.querySelector('.wf-history-detail');
        const close = () => {
            modal.remove();
            document.removeEventListener('keydown', onKey);
        };
        const onKey = (event) => { if (event.key === 'Escape') close(); };
        document.addEventListener('keydown', onKey);
        modal.querySelector('.modal-backdrop').addEventListener('click', close);
        modal.querySelector('.modal-close').addEventListener('click', close);
        modal.querySelector('.wf-history-close').addEventListener('click', close);

        const showDetail = async (runId) => {
            detail.hidden = false;
            detail.innerHTML = '<div class="wf-edit-hint">加载中…</div>';
            try {
                const data = await api(`/api/workspaces/${ws()}/workflows/${workflow.id}/runs/${runId}`);
                const run = data.run || {};
                const items = (run.items && typeof run.items === 'object') ? run.items
                    : (Array.isArray(run.steps) ? {} : (run.steps || {}));
                const stepDefs = Array.isArray(run.steps) ? run.steps : [];
                const inputs = run.run_inputs || {};
                const inputLines = Object.keys(inputs).length
                    ? Object.entries(inputs).map(([k, v]) => `<div><span class="wf-history-key">${esc(k)}</span>：${esc(v)}</div>`).join('')
                    : '<div class="wf-edit-hint">无运行参数</div>';
                const stepLines = stepDefs.map((step, index) => {
                    const item = items[step.id] || {};
                    const status = item.status || 'pending';
                    const err = item.error ? `<div class="wf-history-error">${esc(item.error)}</div>` : '';
                    const artifact = item.artifact ? `<div class="wf-history-artifact">产物：${esc(item.artifact)}</div>` : '';
                    return `<div class="wf-history-step">
                        <span class="wf-history-step-idx">${index + 1}</span>
                        <span class="wf-history-step-name">${esc(step.name || '')}</span>
                        <span class="wf-history-step-status ${iconClass(status)}">${icon(status)} ${esc(statusLabel(status))}</span>
                        ${err}${artifact}
                    </div>`;
                }).join('') || '<div class="wf-edit-hint">没有步骤快照</div>';
                const resultLink = run.results_session_id
                    ? `<button type="button" class="btn btn-small wf-history-open-results" data-session="${esc(run.results_session_id)}">打开结果会话</button>`
                    : '';
                const sendBtn = `<button type="button" class="btn btn-small wf-history-send">发送结果</button>`;
                detail.innerHTML = `
                    <div class="wf-history-detail-head">
                        <span class="${iconClass(run.status)}">${icon(run.status)} ${esc(statusLabel(run.status))}</span>
                        <span>${esc(run.started_at || '')} → ${esc(run.finished_at || '')}</span>
                        <span class="wf-history-run-id">${esc(runId)}</span>
                        ${sendBtn}
                        ${resultLink}
                    </div>
                    ${run.reason ? `<div class="wf-history-error">${esc(run.reason)}</div>` : ''}
                    <div class="wf-history-section-title">运行参数</div>
                    ${inputLines}
                    <div class="wf-history-section-title">步骤</div>
                    ${stepLines}
                `;
                detail.querySelector('.wf-history-open-results')?.addEventListener('click', (event) => {
                    const sid = event.currentTarget.dataset.session;
                    if (sid) window.open(`${window.location.origin}/s/${state.workspaceUuid}/${sid}`, '_blank', 'noopener,noreferrer');
                });
                detail.querySelector('.wf-history-send')?.addEventListener('click', async (event) => {
                    const sid = currentSession && currentSession.session_id;
                    if (!sid) { toast('请先打开一个会话'); return; }
                    const btn = event.currentTarget;
                    btn.disabled = true;
                    try {
                        await api(`/api/workspaces/${ws()}/workflows/${workflow.id}/send-results`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ run_id: runId, session_id: sid }),
                        });
                        toast('结果文件已发送');
                        close();
                    } catch (error) {
                        btn.disabled = false;
                        toast('发送失败：' + (error.message || '未知错误'));
                    }
                });
            } catch (error) {
                detail.innerHTML = `<div class="wf-history-error">加载失败：${esc(error.message || '未知错误')}</div>`;
            }
        };

        (async () => {
            try {
                const data = await api(`/api/workspaces/${ws()}/workflows/${workflow.id}/runs`);
                const runs = data.runs || [];
                if (!runs.length) {
                    list.innerHTML = '<div class="wf-edit-hint">还没有运行记录。</div>';
                    return;
                }
                list.innerHTML = runs.map((run) => `
                    <button type="button" class="wf-history-row" data-run-id="${esc(run.run_id)}">
                        <span class="${iconClass(run.status)}">${icon(run.status)}</span>
                        <span class="wf-history-time">${esc(run.started_at || '')}</span>
                        <span class="wf-history-status">${esc(statusLabel(run.status))}</span>
                        <span class="wf-history-count">${Number(run.step_count) || 0} 步</span>
                    </button>`).join('');
                list.querySelectorAll('.wf-history-row').forEach((row) => {
                    row.addEventListener('click', () => {
                        list.querySelectorAll('.wf-history-row').forEach((r) => r.classList.remove('active'));
                        row.classList.add('active');
                        showDetail(row.dataset.runId);
                    });
                });
            } catch (error) {
                list.innerHTML = `<div class="wf-history-error">加载失败：${esc(error.message || '未知错误')}</div>`;
            }
        })();
        document.body.appendChild(modal);
    }

    function openWorkflowEdit(workflow) {
        if ((state.currentState || {}).status === 'running') {
            toast('工作流正在运行，无法修改');
            return;
        }
        const modal = document.createElement('div');
        modal.className = 'modal';
        modal.innerHTML = `
            <div class="modal-backdrop"></div>
            <div class="modal-content wf-edit-modal">
                <div class="modal-header">
                    <h2>编辑工作流</h2>
                    <button type="button" class="modal-close" aria-label="关闭">×</button>
                </div>
                <div class="modal-body">
                    <label class="wf-edit-field">名称
                        <input type="text" class="wf-wf-name" maxlength="50" value="${esc(workflow.name)}">
                    </label>
                    <label class="wf-edit-field">描述
                        <textarea class="wf-wf-desc" rows="3" maxlength="2000">${esc(workflow.description || '')}</textarea>
                    </label>
                    <div class="wf-edit-field">运行参数
                        <div class="wf-edit-hint">运行前需要用户填写；key 用字母/下划线开头。</div>
                        <div class="wf-wf-inputs"></div>
                        <button type="button" class="btn btn-small wf-wf-add-input">添加输入项</button>
                    </div>
                </div>
                <div class="modal-footer">
                    <span class="wf-edit-status"></span>
                    <span class="wf-edit-footer-actions">
                        <button type="button" class="btn wf-wf-cancel">取消</button>
                        <button type="button" class="btn btn-primary wf-wf-save">保存</button>
                    </span>
                </div>
            </div>`;
        const list = modal.querySelector('.wf-wf-inputs');
        const addRow = (spec = {}) => {
            const row = document.createElement('div');
            row.className = 'wf-wf-input-item';
            row.innerHTML = `
                <div class="wf-wf-input-top">
                    <input class="wf-wf-in-key" placeholder="key" maxlength="40" value="${esc(spec.key || '')}">
                    <input class="wf-wf-in-label" placeholder="名称" maxlength="60" value="${esc(spec.label || '')}">
                    <select class="wf-wf-in-type">
                        <option value="text">单行</option>
                        <option value="textarea">多行</option>
                    </select>
                    <label class="wf-wf-in-required"><input type="checkbox"> 必填</label>
                    <button type="button" class="btn-icon-flat wf-wf-in-remove" title="删除">×</button>
                </div>
                <input class="wf-wf-in-default" placeholder="默认值（可选）" maxlength="2000" value="${esc(spec.default || '')}">
                <input class="wf-wf-in-desc" placeholder="说明（可选）" maxlength="500" value="${esc(spec.description || '')}">
            `;
            row.querySelector('.wf-wf-in-type').value = spec.type === 'textarea' ? 'textarea' : 'text';
            row.querySelector('.wf-wf-in-required input').checked = !!spec.required;
            row.querySelector('.wf-wf-in-remove').addEventListener('click', () => row.remove());
            list.appendChild(row);
        };
        (workflow.inputs || []).forEach(addRow);
        modal.querySelector('.wf-wf-add-input').addEventListener('click', () => addRow());
        const close = () => {
            modal.remove();
            document.removeEventListener('keydown', onKey);
        };
        const onKey = (event) => { if (event.key === 'Escape') close(); };
        document.addEventListener('keydown', onKey);
        modal.querySelector('.modal-backdrop').addEventListener('click', close);
        modal.querySelector('.modal-close').addEventListener('click', close);
        modal.querySelector('.wf-wf-cancel').addEventListener('click', close);
        modal.querySelector('.wf-wf-save').addEventListener('click', async () => {
            const status = modal.querySelector('.wf-edit-status');
            const name = modal.querySelector('.wf-wf-name').value.trim();
            if (!name) { status.textContent = '名称不能为空'; return; }
            const inputDefs = [];
            for (const row of modal.querySelectorAll('.wf-wf-input-item')) {
                const key = row.querySelector('.wf-wf-in-key').value.trim();
                const label = row.querySelector('.wf-wf-in-label').value.trim();
                if (!key && !label) continue;
                inputDefs.push({
                    key,
                    label,
                    type: row.querySelector('.wf-wf-in-type').value,
                    required: row.querySelector('.wf-wf-in-required input').checked,
                    default: row.querySelector('.wf-wf-in-default').value,
                    description: row.querySelector('.wf-wf-in-desc').value.trim(),
                });
            }
            const btn = modal.querySelector('.wf-wf-save');
            btn.disabled = true;
            status.textContent = '保存中…';
            try {
                await api(`/api/workspaces/${ws()}/workflows/${workflow.id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        name,
                        description: modal.querySelector('.wf-wf-desc').value.trim(),
                        inputs: inputDefs,
                        steps: workflow.steps,
                    }),
                });
                close();
                await open(workflow.id, state.activeTab);
                toast('工作流已保存');
            } catch (error) {
                btn.disabled = false;
                status.textContent = '保存失败：' + (error.message || '未知错误');
            }
        });
        document.body.appendChild(modal);
        modal.querySelector('.wf-wf-name').focus();
    }

    function openManualEdit(index) {
        const workflow = state.currentWorkflow;
        const st = state.currentState || {};
        if (!workflow || !workflow.steps[index]) return;
        if (st.status === 'running') {
            toast('工作流正在运行，无法修改');
            return;
        }
        const stepDef = workflow.steps[index];
        const earlier = workflow.steps.slice(0, index);
        const modal = document.createElement('div');
        modal.className = 'modal';
        const sourceRows = earlier.length
            ? earlier.map((s, i) => `
                <label class="wf-edit-source-item">
                    <input type="checkbox" value="${esc(s.id)}"${stepDef.sources.includes(s.id) ? ' checked' : ''}>
                    <span>${i + 1}. ${esc(s.name)}</span>
                </label>`).join('')
            : '<div class="wf-edit-hint">这是第一步，没有可选的更早步骤。</div>';
        modal.innerHTML = `
            <div class="modal-backdrop"></div>
            <div class="modal-content wf-edit-modal">
                <div class="modal-header">
                    <h2>修改第 ${index + 1} 步</h2>
                    <button type="button" class="modal-close" aria-label="关闭">×</button>
                </div>
                <div class="modal-body">
                    <label class="wf-edit-field">标题
                        <input type="text" class="wf-edit-name" maxlength="60" value="${esc(stepDef.name)}">
                    </label>
                    <label class="wf-edit-field">内容
                        <textarea class="wf-edit-req" rows="7" maxlength="8000">${esc(stepDef.requirement)}</textarea>
                    </label>
                    <div class="wf-edit-field">前置选择
                        <div class="wf-edit-hint">可多选；只能选择本步之前的步骤。</div>
                        <div class="wf-edit-sources">${sourceRows}</div>
                    </div>
                </div>
                <div class="modal-footer">
                    <span class="wf-edit-status"></span>
                    <span class="wf-edit-footer-actions">
                        <button type="button" class="btn wf-edit-cancel">取消</button>
                        <button type="button" class="btn btn-primary wf-edit-save">保存</button>
                    </span>
                </div>
            </div>`;
        const close = () => {
            modal.remove();
            document.removeEventListener('keydown', onKey);
        };
        const onKey = (event) => { if (event.key === 'Escape') close(); };
        document.addEventListener('keydown', onKey);
        modal.querySelector('.modal-backdrop').addEventListener('click', close);
        modal.querySelector('.modal-close').addEventListener('click', close);
        modal.querySelector('.wf-edit-cancel').addEventListener('click', close);
        modal.querySelector('.wf-edit-save').addEventListener('click', async () => {
            const name = modal.querySelector('.wf-edit-name').value.trim();
            const requirement = modal.querySelector('.wf-edit-req').value.trim();
            const status = modal.querySelector('.wf-edit-status');
            if (!name || !requirement) {
                status.textContent = '标题和内容不能为空';
                return;
            }
            const sources = Array.from(
                modal.querySelectorAll('.wf-edit-sources input[type="checkbox"]:checked'))
                .map((cb) => cb.value);
            const steps = workflow.steps.map((s, i) => (i === index
                ? { ...s, name, requirement, sources }
                : s));
            const btn = modal.querySelector('.wf-edit-save');
            btn.disabled = true;
            status.textContent = '保存中…';
            try {
                await api(`/api/workspaces/${ws()}/workflows/${workflow.id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: workflow.name, description: workflow.description, inputs: workflow.inputs || [], steps }),
                });
                close();
                await open(workflow.id, state.activeTab);
                toast('步骤已保存');
            } catch (error) {
                btn.disabled = false;
                status.textContent = '保存失败：' + (error.message || '未知错误');
            }
        });
        document.body.appendChild(modal);
        modal.querySelector('.wf-edit-name').focus();
    }

    function ensureLatestResultsSession() {
        // 每次运行新建结果会话；如果当前正停留在旧的结果会话，切到最新的一个
        const st = state.currentState || {};
        if (state.activeTab !== 'results' || !st.results_session_id) return;
        if (!currentSession) return;
        const meta = currentSession.metadata || {};
        if (!meta.run_results) return;   // 编辑/普通会话不劫持
        if (currentSession.session_id === st.results_session_id) return;
        rememberDraft();
        loadSession(st.results_session_id).then(() => {
            restoreDraft(st.results_session_id);
        }).catch(() => { /* ignore */ });
    }

    function maybeAutoSwitchResults() {
        const st = state.currentState || {};
        if (st.status !== 'running' || !st.run_id) return;
        if (state.autoSwitchedRunId === st.run_id) return;
        state.autoSwitchedRunId = st.run_id;
        if (state.activeTab !== 'results') switchTab('results');
    }

    function fillComposer(text) {
        try {
            if (!chatInput) return;
            chatInput.value = text;
            chatInput.focus();
            chatInput.dispatchEvent(new Event('input'));
        } catch (e) { /* ignore */ }
    }

    // ── 轮询 / 回合结束 ────────────────────────────────────

    function startPolling() {
        if (state.polling) return;
        state.polling = setInterval(tick, 2000);
    }

    function stopPolling() {
        if (!state.polling) return;
        clearInterval(state.polling);
        state.polling = null;
    }

    async function tick() {
        if (!state.workspaceUuid) { stopPolling(); return; }
        try {
            await loadWorkflowsQuiet();
            if (state.currentWorkflowId) {
                const data = await api(`/api/workspaces/${ws()}/workflows/${state.currentWorkflowId}`);
                state.currentWorkflow = data.workflow;
                state.currentState = data.state;
                renderPanel();
                renderTabs();
                maybeAutoSwitchResults();
                ensureLatestResultsSession();
            }
        } catch (error) { /* 下一轮重试 */ }
        const running = state.workflows.some((item) => workflowStatus(item) === 'running')
            || state.currentState?.status === 'running';
        if (!running) stopPolling();
    }

    async function loadWorkflowsQuiet() {
        try {
            const data = await api(`/api/workspaces/${ws()}/workflows`);
            state.workflows = data.workflows || [];
            renderSidebar();
        } catch (e) { /* ignore */ }
    }

    async function onTurnComplete(sessionId) {
        if (!sessionId) return;
        if (!state.currentWorkflowId) {
            // 未绑定会话：可能刚刚通过 workflow.create 创建了工作流
            try {
                const data = await api(`/api/workspaces/${ws()}/workflows/panel-sessions/by-session/${encodeURIComponent(sessionId)}`);
                if (data.workflow_id) {
                    await open(data.workflow_id, 'edit');
                    await loadWorkflows();
                }
            } catch (e) { /* ignore */ }
            return;
        }
        try {
            const data = await api(`/api/workspaces/${ws()}/workflows/${state.currentWorkflowId}`);
            state.currentWorkflow = data.workflow;
            state.currentState = data.state;
            renderPanel();
            renderTabs();
            loadWorkflowsQuiet();
            maybeAutoSwitchResults();
            ensureLatestResultsSession();
        } catch (e) { /* ignore */ }
    }

    window.WorkflowPane = {
        init,
        reset,
        open,
        openNew,
        openForSession,
        openWorkflowById,
        newWorkflow: newWorkflow,
        switchTab,
        startRun: () => startRunFor(state.currentWorkflowId),
        close,
        refresh: () => loadWorkflows(),
        getSidebarEntries,
        getCurrentWorkflowId: () => state.currentWorkflowId,
        getCurrentWorkflow: () => state.currentWorkflow,
        getCurrentState: () => state.currentState,
        setHidden,
        importFile: importWorkflowFile,
        exportWorkflow,
        removeWorkflow: deleteWorkflow,
        onTurnComplete,
    };
})();
