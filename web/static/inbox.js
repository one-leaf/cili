// ── 收件箱 UI ──────────────────────────────────────────────
// 全局通知的唯一消费端：侧栏角标 + 两栏弹窗。
// 依赖 app.js/chat-empty-state.js 的 currentWorkspace / workspaceSelect /
// handleWorkspaceChange / loadSession；依赖 markdown-core.js 的
// renderMarkdownStrict；依赖 utils.js 的 showToast。
(function () {
    'use strict';

    const INTERFACE_LABELS = {
        web: '网页',
        qq: 'QQ',
        wechat: '微信',
        email: '邮件',
        cli: '终端',
    };

    const state = {
        open: false,
        status: 'active',
        items: [],
        selectedId: '',
        unreadCount: 0,
        listRequestId: 0,
        refreshTimer: null,
    };

    let els = null;

    function init() {
        els = {
            modal: document.getElementById('inbox-modal'),
            list: document.getElementById('inbox-list'),
            detail: document.getElementById('inbox-detail'),
            openBtn: document.getElementById('inbox-btn'),
            badge: document.getElementById('inbox-unread-badge'),
            openBadge: document.getElementById('inbox-open-badge'),
            filterCount: document.getElementById('inbox-unread-count-filter'),
            readAllBtn: document.getElementById('inbox-readall-btn'),
        };
        if (!els.modal || !els.list || !els.detail) return;

        if (els.openBtn) els.openBtn.addEventListener('click', open);
        if (els.readAllBtn) els.readAllBtn.addEventListener('click', readAll);

        els.modal.querySelectorAll('[data-modal="inbox-modal"]').forEach((el) => {
            el.addEventListener('click', close);
        });
        document.querySelectorAll('.inbox-filter').forEach((btn) => {
            btn.addEventListener('click', () => setStatus(btn.dataset.status || 'active'));
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && state.open) close();
        });

        refreshCount();
        // 没有会话时不会建立 SSE 连接；低频轮询兜底角标
        setInterval(() => {
            if (!document.hidden) refreshCount();
        }, 60000);
        // 没有会话时不会建立 SSE；面板打开时低频刷新列表兜底
        setInterval(() => {
            if (state.open && !document.hidden) refreshList();
        }, 30000);
    }

    async function api(path, options) {
        const res = await fetch(path, Object.assign({ credentials: 'same-origin' }, options || {}));
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(data.detail || `请求失败（${res.status}）`);
        }
        return data;
    }

    function updateBadge(count) {
        state.unreadCount = Math.max(0, Number(count) || 0);
        const text = state.unreadCount > 99 ? '99+' : String(state.unreadCount);
        [els.badge, els.openBadge].forEach((el) => {
            if (!el) return;
            el.textContent = text;
            el.hidden = state.unreadCount === 0;
        });
        if (els.filterCount) {
            els.filterCount.textContent = state.unreadCount ? String(state.unreadCount) : '';
        }
        if (els.openBtn) {
            els.openBtn.setAttribute(
                'aria-label',
                state.unreadCount ? `收件箱，${state.unreadCount} 条未读` : '收件箱'
            );
        }
    }

    async function refreshCount() {
        try {
            const data = await api('/api/inbox/count');
            updateBadge(data.unread_count);
        } catch (e) {
            console.warn('[inbox] 拉取未读数失败', e);
        }
    }

    async function open() {
        if (!els || !els.modal) return;
        state.open = true;
        state.selectedId = '';
        els.modal.style.display = 'flex';
        renderDetailPlaceholder('选择一条通知');
        await Promise.all([refreshCount(), refreshList()]);
    }

    function close() {
        if (!els || !els.modal) return;
        state.open = false;
        els.modal.style.display = 'none';
    }

    function setStatus(status) {
        state.status = status;
        state.selectedId = '';
        document.querySelectorAll('.inbox-filter').forEach((btn) => {
            btn.classList.toggle('active', btn.dataset.status === status);
        });
        renderDetailPlaceholder('选择一条通知');
        refreshList();
    }

    async function refreshList() {
        if (!els) return;
        const status = state.status;
        const requestId = ++state.listRequestId;
        try {
            const data = await api(
                `/api/inbox?status=${encodeURIComponent(status)}&limit=200`
            );
            // 请求期间用户切了筛选：丢弃旧响应，避免「未读」标签下显示全部
            if (requestId !== state.listRequestId || status !== state.status) return;
            state.items = data.items || [];
            updateBadge(data.unread_count);
            renderList();
        } catch (e) {
            if (requestId !== state.listRequestId || status !== state.status) return;
            els.list.innerHTML = '';
            els.list.appendChild(emptyNode('加载失败：' + e.message));
        }
    }

    function renderList() {
        if (!els) return;
        els.list.innerHTML = '';
        if (!state.items.length) {
            els.list.appendChild(emptyNode(state.status === 'deleted' ? '回收站是空的' : '没有通知'));
            return;
        }
        state.items.forEach((item) => {
            const row = document.createElement('div');
            row.className = 'inbox-item'
                + (item.read_at || item.deleted_at ? '' : ' unread')
                + (item.deleted_at ? ' deleted' : '')
                + (item.id === state.selectedId ? ' active' : '');
            row.dataset.id = item.id;
            row.tabIndex = 0;

            const title = document.createElement('div');
            title.className = 'inbox-item-title';
            if (!item.read_at && !item.deleted_at) {
                const dot = document.createElement('span');
                dot.className = 'inbox-dot';
                title.appendChild(dot);
            }
            title.appendChild(document.createTextNode(item.title || '（无标题）'));
            row.appendChild(title);

            const meta = document.createElement('div');
            meta.className = 'inbox-item-meta';
            [formatTime(item.created_at), sourceLabel(item), item.workspace_name || '未知工作区']
                .filter(Boolean)
                .forEach((text) => {
                    const span = document.createElement('span');
                    span.textContent = text;
                    meta.appendChild(span);
                });
            row.appendChild(meta);

            row.addEventListener('click', () => selectItem(item.id));
            row.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    selectItem(item.id);
                }
            });
            els.list.appendChild(row);
        });
    }

    async function selectItem(id) {
        state.selectedId = id;
        renderList();
        try {
            const data = await api(`/api/inbox/${encodeURIComponent(id)}`);
            let item = data.item;
            if (!item.read_at && !item.deleted_at) {
                try {
                    const read = await api(`/api/inbox/${encodeURIComponent(id)}/read`, {
                        method: 'POST',
                    });
                    if (read.item) item = read.item;
                    updateBadge(read.unread_count);
                    const row = state.items.find((x) => x.id === id);
                    if (row) row.read_at = item.read_at || ' ';
                    renderList();
                } catch (e) {
                    console.warn('[inbox] 标记已读失败', e);
                }
            }
            renderDetail(item);
        } catch (e) {
            renderDetailPlaceholder('无法打开：' + e.message);
            refreshCount();
        }
    }

    function renderDetailPlaceholder(text) {
        if (!els) return;
        els.detail.innerHTML = '';
        els.detail.appendChild(emptyNode(text));
    }

    function renderDetail(item) {
        if (!els) return;
        els.detail.innerHTML = '';

        const back = actionButton('← 返回列表', () => {
            state.selectedId = '';
            renderDetailPlaceholder('选择一条通知');
            renderList();
        }, 'btn btn-small inbox-back-btn');
        els.detail.appendChild(back);

        const title = document.createElement('div');
        title.className = 'inbox-detail-title';
        title.textContent = item.title || '（无标题）';
        els.detail.appendChild(title);

        const meta = document.createElement('div');
        meta.className = 'inbox-detail-meta';
        [
            formatTime(item.created_at),
            sourceLabel(item),
            item.workspace_name || '未知工作区',
            item.session_title || '',
        ].filter(Boolean).forEach((text) => {
            const span = document.createElement('span');
            span.textContent = text;
            meta.appendChild(span);
        });
        els.detail.appendChild(meta);

        const body = document.createElement('div');
        body.className = 'inbox-detail-body';
        if (item.body) {
            const html = typeof renderMarkdownStrict === 'function'
                ? renderMarkdownStrict(rewriteInboxLinks(item.body, item.workspace_uuid))
                : escapeHtml(item.body);
            body.innerHTML = html;
        } else {
            body.textContent = '（无正文）';
        }
        els.detail.appendChild(body);

        const actions = document.createElement('div');
        actions.className = 'inbox-actions';

        if (item.workspace_uuid && item.session_id && !item.deleted_at) {
            actions.appendChild(actionButton('打开会话', () => openSession(item)));
        }
        if (item.deleted_at) {
            actions.appendChild(actionButton('恢复', () => restoreItem(item)));
            actions.appendChild(actionButton('彻底删除', () => deleteItem(item, true)));
        } else {
            actions.appendChild(actionButton(
                item.read_at ? '标为未读' : '标为已读',
                () => toggleRead(item)
            ));
            actions.appendChild(actionButton('删除', () => deleteItem(item, false)));
        }
        els.detail.appendChild(actions);
    }

    function actionButton(text, onClick, className) {
        const btn = document.createElement('button');
        btn.className = className || 'btn btn-small';
        btn.textContent = text;
        btn.addEventListener('click', onClick);
        return btn;
    }

    async function toggleRead(item) {
        const action = item.read_at ? 'unread' : 'read';
        try {
            const data = await api(`/api/inbox/${encodeURIComponent(item.id)}/${action}`, {
                method: 'POST',
            });
            updateBadge(data.unread_count);
            const row = state.items.find((x) => x.id === item.id);
            if (row) row.read_at = action === 'read' ? (data.item && data.item.read_at) || ' ' : '';
            renderList();
            if (data.item) renderDetail(data.item);
        } catch (e) {
            showToast('操作失败：' + e.message);
        }
    }

    async function deleteItem(item, permanent) {
        const workspace = item.workspace_name || '未知工作区';
        if (permanent && !confirm(`彻底删除「${item.title}」（${workspace}）？此操作不可恢复。`)) return;
        const path = `/api/inbox/${encodeURIComponent(item.id)}`
            + (permanent ? '?permanent=true' : '');
        try {
            await api(path, { method: 'DELETE' });
            showToast(permanent ? '已彻底删除' : '已移到回收站');
            if (state.selectedId === item.id) {
                state.selectedId = '';
                renderDetailPlaceholder('选择一条通知');
            }
            await Promise.all([refreshCount(), refreshList()]);
        } catch (e) {
            showToast('删除失败：' + e.message);
        }
    }

    async function restoreItem(item) {
        try {
            await api(`/api/inbox/${encodeURIComponent(item.id)}/restore`, { method: 'POST' });
            showToast('已恢复');
            state.selectedId = item.id;
            await Promise.all([refreshCount(), refreshList()]);
            await selectItem(item.id);
        } catch (e) {
            showToast('恢复失败：' + e.message);
        }
    }

    async function readAll() {
        if (!state.unreadCount) {
            showToast('没有未读通知');
            return;
        }
        if (!confirm(`将标记全部工作区的 ${state.unreadCount} 条未读为已读？`)) return;
        try {
            await api('/api/inbox/read-all', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
            });
            state.selectedId = '';
            renderDetailPlaceholder('选择一条通知');
            await Promise.all([refreshCount(), refreshList()]);
        } catch (e) {
            showToast('操作失败：' + e.message);
        }
    }

    async function openSession(item) {
        close();
        try {
            if (typeof currentWorkspace === 'undefined' || !currentWorkspace
                    || currentWorkspace.uuid !== item.workspace_uuid) {
                if (typeof workspaceSelect === 'undefined'
                        || typeof handleWorkspaceChange !== 'function') {
                    showToast('无法切换工作区');
                    return;
                }
                workspaceSelect.value = item.workspace_uuid;
                await handleWorkspaceChange();
            }
            if (typeof currentWorkspace === 'undefined' || !currentWorkspace
                    || currentWorkspace.uuid !== item.workspace_uuid) {
                showToast('工作区已不存在');
                return;
            }
            if (typeof loadSession === 'function') {
                await loadSession(item.session_id);
            }
        } catch (e) {
            showToast('打开会话失败：' + e.message);
        }
    }

    function rewriteInboxLinks(text, workspaceUuid) {
        if (!text || !workspaceUuid) return text || '';
        return text.replace(
            /\[([^\]]*)\]\((?!https?:|\/api|data:|#|mailto:)([^)]+)\)/g,
            (match, label, url) => {
                const clean = url.startsWith('/') ? url.slice(1) : url;
                return `[${label}](/api/workspaces/${encodeURIComponent(workspaceUuid)}/files/${clean})`;
            }
        );
    }

    function sourceLabel(item) {
        const source = item.source || {};
        if (source.type === 'cron') return '定时';
        if (source.type === 'workflow') return '工作流';
        if (source.type === 'system') return '系统';
        let label = source.agent === 'worker' ? 'AI·子代理' : 'AI';
        if (source.interface) {
            label += '·' + (INTERFACE_LABELS[source.interface] || source.interface);
        }
        return label;
    }

    function formatTime(ts) {
        if (!ts) return '';
        const parts = String(ts).split(' ');
        const date = parts[0] || '';
        const time = (parts[1] || '').slice(0, 5);
        const now = new Date();
        const pad = (n) => String(n).padStart(2, '0');
        const today = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
        if (date === today) return time;
        if (date.slice(0, 4) === String(now.getFullYear())) {
            return `${date.slice(5)} ${time}`.trim();
        }
        return date;
    }

    function emptyNode(text) {
        const div = document.createElement('div');
        div.className = 'inbox-empty';
        div.textContent = text;
        return div;
    }

    function onBusEvent(event) {
        if (!event || event.type !== 'inbox_changed') return;
        refreshCount();
        if (!state.open) return;
        if (state.refreshTimer) clearTimeout(state.refreshTimer);
        state.refreshTimer = setTimeout(() => {
            state.refreshTimer = null;
            refreshList();
        }, 300);
    }

    window.Inbox = {
        open,
        close,
        refresh: refreshCount,
        onBusEvent,
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
