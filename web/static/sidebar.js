// ── 会话/工作流侧栏列表与批量操作 ──
// 依赖 app.js: currentWorkspace, currentSession, showToast, setCurrentSession, chat 渲染等全局。

async function loadSessions() {
    if (!currentWorkspace) return;

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions`);
        const data = await response.json();
        sessions = data.sessions;
        renderSidebarList();
    } catch (error) {
        console.error('Failed to load sessions:', error);
        sessionsList.innerHTML = '<div class="empty-state">加载会话失败</div>';
    }
}

// ── 会话与工作流合并列表 ───────────────────────────────────

function sessionSidebarEntry(session) {
    return {
        type: 'session',
        id: session.session_id,
        name: session.display_name || session.name || session.preview || '未命名',
        hidden: !!session.hidden,
        updated_at: session.updated_at || '',
        session,
    };
}

function workflowSidebarEntry(item) {
    return {
        type: 'workflow',
        id: item.id,
        name: item.name || '未命名工作流',
        hidden: !!item.hidden,
        status: item.status || 'idle',
        progress: item.progress || '',
        updated_at: item.updated_at || '',
    };
}

// 当前类型下的侧栏条目，按 updated_at 倒序（工作流数据由 WorkflowPane 提供）
function collectSidebarEntries() {
    let entries;
    if (sidebarFilter === 'workflow') {
        entries = [];
        if (window.WorkflowPane && typeof WorkflowPane.getSidebarEntries === 'function') {
            for (const item of WorkflowPane.getSidebarEntries()) {
                if (!!item.hidden !== showHiddenSessions) continue;
                entries.push(workflowSidebarEntry(item));
            }
        }
    } else {
        entries = sessions
            .filter(session => !!session.hidden === showHiddenSessions)
            .map(sessionSidebarEntry);
    }
    entries.sort((a, b) => {
        if (a.updated_at !== b.updated_at) return a.updated_at < b.updated_at ? 1 : -1;
        return a.id < b.id ? 1 : -1;
    });
    return entries;
}

function emptySidebarText() {
    if (showHiddenSessions) return '没有隐藏项';
    return sidebarFilter === 'workflow' ? '暂无工作流' : '暂无会话';
}

let _sidebarRenderSig = '';

function renderSidebarList() {
    // 未选工作区时保留调用方写入的空状态文案
    if (!currentWorkspace || !sessionsList) return;
    const entries = collectSidebarEntries();

    // 工作流运行中每 2s 轮询都会触发重绘：内容没变就跳过，避免整列表闪动
    const currentWorkflowId = (window.WorkflowPane
        && typeof WorkflowPane.getCurrentWorkflowId === 'function')
        ? WorkflowPane.getCurrentWorkflowId() : '';
    const sig = [
        isMultiSelectMode ? '1' : '0',
        showHiddenSessions ? '1' : '0',
        sidebarFilter,
        currentSession ? currentSession.session_id : '',
        currentWorkflowId,
        entries.map(e => `${e.type}:${e.id}:${e.name}:${e.hidden ? 1 : 0}:${e.status || ''}:${e.progress || ''}`).join('|'),
    ].join('~');
    if (sig === _sidebarRenderSig) return;
    _sidebarRenderSig = sig;

    const countEl = document.getElementById('sessions-count');
    if (countEl) {
        countEl.textContent = String(entries.length);
        countEl.hidden = entries.length === 0;
    }

    if (entries.length === 0) {
        sessionsList.innerHTML = `<div class="empty-state">${emptySidebarText()}</div>`;
        return;
    }

    sessionsList.innerHTML = '';
    entries.forEach(entry => {
        sessionsList.appendChild(entry.type === 'workflow'
            ? buildWorkflowRow(entry)
            : buildSessionRow(entry.session));
    });
}

function buildSessionRow(session) {
    const div = document.createElement('div');
    div.className = 'session-item';
    const isSelected = selectedSessions.has(session.session_id);
    if (isSelected) {
        div.classList.add('selected');
    }
    if (currentSession && currentSession.session_id === session.session_id) {
        div.classList.add('active');
    }

    // 多选模式：添加选择框
    if (isMultiSelectMode) {
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'session-checkbox';
        checkbox.checked = isSelected;
        checkbox.addEventListener('click', (e) => {
            e.stopPropagation();
            if (checkbox.checked) {
                selectedSessions.add(session.session_id);
            } else {
                selectedSessions.delete(session.session_id);
            }
            updateFooterToolbar();
            div.classList.toggle('selected', checkbox.checked);
        });
        div.appendChild(checkbox);
    }

    // 展示名由后端统一给出（core/session.py 的 session_display_name），
    // 保证 Web 与 QQ 的 /session 列表口径一致；缺失时退回本地兜底。
    const displayName = session.display_name || session.name || session.preview || '未命名';
    const preview = document.createElement('div');
    preview.className = 'session-preview';
    // 通道徽标：只标非 Web 来源（QQ/微信/终端/定时），网页会话不占位
    const channelLabel = SESSION_CHANNEL_LABELS[session.channel];
    if (channelLabel && session.channel !== 'web') {
        const badge = document.createElement('span');
        badge.className = 'session-channel-badge';
        badge.textContent = channelLabel;
        preview.appendChild(badge);
    }
    preview.appendChild(document.createTextNode(displayName));
    div.appendChild(preview);

    if (!isMultiSelectMode) {
        // 非多选模式：添加三点菜单按钮
        const menuBtn = document.createElement('button');
        menuBtn.className = 'session-menu-btn';
        menuBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><circle cx="8" cy="3" r="1.5"/><circle cx="8" cy="8" r="1.5"/><circle cx="8" cy="13" r="1.5"/></svg>';
        menuBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleSessionItemMenu(session, menuBtn);
        });
        div.appendChild(menuBtn);
        div.addEventListener('click', () => loadSession(session.session_id));
    } else {
        // 多选模式：点击整行切换选中
        div.addEventListener('click', () => {
            const cb = div.querySelector('.session-checkbox');
            cb.checked = !cb.checked;
            if (cb.checked) {
                selectedSessions.add(session.session_id);
            } else {
                selectedSessions.delete(session.session_id);
            }
            updateFooterToolbar();
            div.classList.toggle('selected', cb.checked);
        });
    }

    return div;
}

function buildWorkflowRow(entry) {
    const div = document.createElement('div');
    div.className = 'session-item wf-item';
    const isSelected = selectedWorkflows.has(entry.id);
    if (isSelected) {
        div.classList.add('selected');
    }
    if (window.WorkflowPane && typeof WorkflowPane.getCurrentWorkflowId === 'function'
            && WorkflowPane.getCurrentWorkflowId() === entry.id) {
        div.classList.add('active');
    }

    if (isMultiSelectMode) {
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'session-checkbox';
        checkbox.checked = isSelected;
        checkbox.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleWorkflowSelection(entry.id, checkbox.checked, div);
        });
        div.appendChild(checkbox);
    }

    const dot = document.createElement('span');
    const statusClass = entry.status === 'completed' ? 'done' : (entry.status || 'idle');
    dot.className = 'wf-row-dot wf-status-' + statusClass;
    dot.textContent = '●';
    div.appendChild(dot);

    const preview = document.createElement('div');
    preview.className = 'session-preview';
    preview.appendChild(document.createTextNode(entry.name));
    div.appendChild(preview);

    if (entry.progress) {
        const progress = document.createElement('span');
        progress.className = 'wf-row-progress';
        progress.textContent = entry.progress;
        div.appendChild(progress);
    }

    if (!isMultiSelectMode) {
        const menuBtn = document.createElement('button');
        menuBtn.className = 'session-menu-btn';
        menuBtn.title = '更多';
        menuBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><circle cx="8" cy="3" r="1.5"/><circle cx="8" cy="8" r="1.5"/><circle cx="8" cy="13" r="1.5"/></svg>';
        menuBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleWorkflowItemMenu(entry, menuBtn);
        });
        div.appendChild(menuBtn);
        div.addEventListener('click', () => {
            if (window.WorkflowPane) WorkflowPane.openWorkflowById(entry.id);
        });
    } else {
        div.addEventListener('click', () => {
            const cb = div.querySelector('.session-checkbox');
            cb.checked = !cb.checked;
            toggleWorkflowSelection(entry.id, cb.checked, div);
        });
    }

    return div;
}

function toggleWorkflowSelection(workflowId, checked, rowEl) {
    if (checked) selectedWorkflows.add(workflowId);
    else selectedWorkflows.delete(workflowId);
    updateFooterToolbar();
    if (rowEl) rowEl.classList.toggle('selected', checked);
}

// 保存的原始 footer 内容
let _footerOriginalHTML = null;

// 多选时把批量操作面板移到文件列表上方，普通状态放回侧栏底部
function setFooterAboveFiles(above) {
    const footer = document.querySelector('.sidebar-footer');
    const files = document.getElementById('files-section');
    if (!footer || !files || !footer.parentElement) return;
    const parent = footer.parentElement;
    if (above) {
        if (footer.nextElementSibling !== files) parent.insertBefore(footer, files);
    } else if (footer !== parent.lastElementChild) {
        parent.appendChild(footer);
    }
}

// 更新底部工具条（复用 footer-content）
function updateFooterToolbar() {
    setFooterAboveFiles(isMultiSelectMode);
    const footerContent = document.getElementById('footer-content');
    if (isMultiSelectMode) {
        const count = selectedSessions.size + selectedWorkflows.size;
        const hideLabel = showHiddenSessions ? '取消隐藏' : '隐藏';
        // 青少年模式：批量删除硬禁（按钮隐藏）；批量隐藏受 deny_hide 约束
        const teenOn = window.teenStatus && window.teenStatus.enabled;
        const teenUnlocked = window.teenStatus && window.teenStatus.unlocked;
        const teenHideDenied = teenOn && !teenUnlocked &&
            (window.teenStatus.policy || {}).deny_hide;
        footerContent.innerHTML = `
            ${teenHideDenied && showHiddenSessions ? '' : `<button id="footer-batch-hide" class="btn btn-small" ${count === 0 ? 'disabled' : ''}>${hideLabel}</button>`}
            ${teenOn ? '' : `<button id="footer-batch-delete" class="btn btn-small btn-danger" ${count === 0 ? 'disabled' : ''}>删除</button>`}
            <button id="footer-exit-multi" class="btn btn-small">取消</button>
        `;
        const batchHideBtn = document.getElementById('footer-batch-hide');
        if (batchHideBtn) batchHideBtn.addEventListener('click', batchHideUnhide);
        const batchDeleteBtn = document.getElementById('footer-batch-delete');
        if (batchDeleteBtn) batchDeleteBtn.addEventListener('click', batchDelete);
        document.getElementById('footer-exit-multi').addEventListener('click', exitMultiSelect);
    } else if (_footerOriginalHTML !== null) {
        footerContent.innerHTML = _footerOriginalHTML;
        _footerOriginalHTML = null;
        // 恢复后重新绑定主题切换按钮
        const toggleBtn = document.getElementById('theme-toggle-btn');
        if (toggleBtn) toggleBtn.addEventListener('click', toggleTheme);
    }
}

// 退出多选模式
function exitMultiSelect() {
    isMultiSelectMode = false;
    selectedSessions.clear();
    selectedWorkflows.clear();
    newSessionBtn.style.display = '';
    updateFooterToolbar();
    renderSidebarList();
}

// 切换单个会话项的右键菜单
function toggleSessionItemMenu(session, btn) {
    // 同一个会话的菜单已打开时，再按一次关闭
    const existing = document.querySelector(
        `.session-dropdown[data-session-menu="${session.session_id}"]`);
    if (existing) {
        existing.remove();
        return;
    }
    // 关闭其它会话已打开的菜单
    document.querySelectorAll('.session-dropdown').forEach(menu => menu.remove());

    // 创建新的下拉菜单
    const dropdown = document.createElement('div');
    dropdown.className = 'session-dropdown show';
    dropdown.dataset.sessionMenu = session.session_id;

    // 青少年模式：删除（硬禁）与分享/隐藏（策略禁）不渲染，避免点了才报错
    const teenOn = window.teenStatus && window.teenStatus.enabled;
    const teenUnlocked = window.teenStatus && window.teenStatus.unlocked;
    const teenPolicy = (teenOn && window.teenStatus.policy) || {};
    const hideLabel = session.hidden ? '取消隐藏' : '隐藏会话';
    const showDelete = !teenOn;
    const showShare = !teenOn || !teenPolicy.deny_share || teenUnlocked;
    const showHide = !teenOn || (!session.hidden && (!teenPolicy.deny_hide || teenUnlocked)) || session.hidden;
    dropdown.innerHTML = `
        <div class="session-dropdown-item" data-action="rename">修改名称</div>
        ${showHide ? `<div class="session-dropdown-item" data-action="hide">${hideLabel}</div>` : ''}
        <div class="session-dropdown-item" data-action="fork">分支会话</div>
        <div class="session-dropdown-item" data-action="about">关于会话</div>
        ${showShare ? '<div class="session-dropdown-item" data-action="export">导出会话</div>' : ''}
        ${showShare ? '<div class="session-dropdown-item" data-action="share">分享会话</div>' : ''}
        ${showDelete ? '<div class="session-dropdown-item" data-action="delete">删除会话</div>' : ''}
    `;

    // 添加菜单项点击事件
    dropdown.querySelectorAll('.session-dropdown-item').forEach(item => {
        item.addEventListener('click', (e) => {
            e.stopPropagation();
            handleSessionAction(session, item.dataset.action);
            dropdown.remove();
        });
    });

    openAnchoredMenu(dropdown, btn, true);

    // 添加全局点击事件关闭菜单
    setTimeout(() => {
        document.addEventListener('click', closeSessionMenus, { once: true });
    }, 0);
}

// 关闭所有会话菜单
function closeSessionMenus() {
    document.querySelectorAll('.session-dropdown').forEach(menu => menu.remove());
}

// 工作流条目的菜单（与会话菜单同一套样式；运行入口在工作流面板头部）
function toggleWorkflowItemMenu(entry, btn) {
    const existing = document.querySelector(
        `.session-dropdown[data-workflow-menu="${entry.id}"]`);
    if (existing) {
        existing.remove();
        return;
    }
    document.querySelectorAll('.session-dropdown').forEach(menu => menu.remove());

    const dropdown = document.createElement('div');
    dropdown.className = 'session-dropdown show';
    dropdown.dataset.workflowMenu = entry.id;
    const hideLabel = entry.hidden ? '取消隐藏' : '隐藏工作流';
    dropdown.innerHTML = `
        <div class="session-dropdown-item" data-wf-action="hide">${hideLabel}</div>
        <div class="session-dropdown-item" data-wf-action="export">导出工作流</div>
        <div class="session-dropdown-item" data-wf-action="delete">删除工作流</div>
    `;

    dropdown.querySelectorAll('.session-dropdown-item').forEach(item => {
        item.addEventListener('click', (e) => {
            e.stopPropagation();
            const action = item.dataset.wfAction;
            dropdown.remove();
            if (!window.WorkflowPane) return;
            if (action === 'hide') WorkflowPane.setHidden(entry.id, !entry.hidden);
            else if (action === 'export') WorkflowPane.exportWorkflow(entry.id);
            else if (action === 'delete') WorkflowPane.removeWorkflow(entry.id);
        });
    });

    openAnchoredMenu(dropdown, btn, true);
    setTimeout(() => {
        document.addEventListener('click', closeSessionMenus, { once: true });
    }, 0);
}

// 会话导出（JSON，可再导入）
async function exportSession(session) {
    if (!currentWorkspace) return;
    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${session.session_id}/export`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        downloadJsonFile(data, `${data.name || '会话'}.json`);
        showToast('会话已导出');
    } catch (error) {
        showToast('导出失败：' + (error.message || '未知错误'));
    }
}

function downloadJsonFile(data, filename) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = String(filename).replace(/[<>:"/\\|?*]/g, '_');
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// 新建：跟随侧栏当前类型（会话 / 工作流）
function handleNewButton() {
    if (sidebarFilter === 'workflow') {
        if (window.WorkflowPane) WorkflowPane.newWorkflow();
        return;
    }
    createNewSession();
}

// 导入：跟随侧栏当前类型，不再按文件内容识别
async function handleImportFile(file) {
    if (!currentWorkspace) return;
    if (sidebarFilter === 'workflow') {
        if (window.WorkflowPane) await WorkflowPane.importFile(file);
        return;
    }
    let data;
    try {
        data = JSON.parse(await file.text());
    } catch (e) {
        showToast('不是有效的 JSON 文件');
        return;
    }
    if (!data || !Array.isArray(data.messages)) {
        showToast('不是有效的会话文件');
        return;
    }
    await importSessionData(data);
}

async function importSessionData(data) {
    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/import`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: data.name, messages: data.messages })
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.detail || (`HTTP ${response.status}`));
        showToast(`已导入会话「${result.name || '会话'}」`);
        await loadSessions();
        await loadSession(result.session_id);
    } catch (error) {
        showToast('导入失败：' + (error.message || '未知错误'));
    }
}

// 把下拉菜单挂到 body 并用 fixed 定位：
// 侧栏分区是 overflow:hidden，菜单挂在分区内会被文件区裁剪/遮挡
function openAnchoredMenu(dropdown, anchorEl, alignRight = false) {
    document.body.appendChild(dropdown);
    const rect = anchorEl.getBoundingClientRect();
    const margin = 6;
    dropdown.style.position = 'fixed';
    dropdown.style.right = 'auto';
    dropdown.style.left = '0px';
    dropdown.style.top = '0px';
    const width = dropdown.offsetWidth || 160;
    const height = dropdown.offsetHeight || 0;
    let left = alignRight ? rect.right - width : rect.left;
    left = Math.max(margin, Math.min(left, window.innerWidth - width - margin));
    let top = rect.bottom + margin;
    if (top + height > window.innerHeight - margin) {
        top = Math.max(margin, rect.top - height - margin);
    }
    dropdown.style.left = left + 'px';
    dropdown.style.top = top + 'px';
}

// 滚动关闭菜单：全局注册一次，避免每次打开菜单都新增一次性监听器
window.addEventListener('scroll', closeSessionMenus, { capture: true, passive: true });

// 切换会话面板头部菜单（活跃/隐藏切换 + 多选切换）
function toggleSessionPanelMenu(e) {
    e.stopPropagation();
    // 关闭已有的面板菜单
    const existing = document.querySelector('.panel-menu-dropdown');
    if (existing) {
        existing.remove();
        return;
    }

    const dropdown = document.createElement('div');
    dropdown.className = 'session-dropdown panel-menu-dropdown show';

    const viewLabel = showHiddenSessions ? '退出隐藏' : '显示隐藏项';
    const multiLabel = isMultiSelectMode ? '退出多选' : '多选';

    dropdown.innerHTML = `
        <div class="session-dropdown-item" data-panel-action="import">导入</div>
        <div class="session-dropdown-item" data-panel-action="toggle-view">${viewLabel}</div>
        <div class="session-dropdown-item" data-panel-action="toggle-multi">${multiLabel}</div>
    `;

    dropdown.querySelectorAll('.session-dropdown-item').forEach(item => {
        item.addEventListener('click', (ev) => {
            ev.stopPropagation();
            const action = item.dataset.panelAction;
            dropdown.remove();
            if (action === 'import') {
                document.getElementById('session-import-input')?.click();
            } else if (action === 'toggle-view') {
                showHiddenSessions = !showHiddenSessions;
                renderSidebarList();
            } else if (action === 'toggle-multi') {
                if (isMultiSelectMode) {
                    exitMultiSelect();
                } else {
                    isMultiSelectMode = true;
                    selectedSessions.clear();
                    selectedWorkflows.clear();
                    newSessionBtn.style.display = 'none';
                    // 保存 footer 原始内容
                    const footerContent = document.getElementById('footer-content');
                    _footerOriginalHTML = footerContent.innerHTML;
                    updateFooterToolbar();
                    renderSidebarList();
                }
            }
        });
    });

    openAnchoredMenu(dropdown, sessionMenuBtn, false);

    setTimeout(() => {
        document.addEventListener('click', () => dropdown.remove(), { once: true });
    }, 0);
}

// 批量隐藏/取消隐藏（会话走 batch 接口，工作流逐个改 hidden 字段）
async function batchHideUnhide() {
    if (!currentWorkspace || selectedSessions.size + selectedWorkflows.size === 0) return;
    const action = showHiddenSessions ? 'unhide' : 'hide';
    try {
        if (selectedSessions.size) {
            await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/batch`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_ids: [...selectedSessions], action })
            });
        }
        for (const workflowId of selectedWorkflows) {
            await fetch(`/api/workspaces/${currentWorkspace.uuid}/workflows/${encodeURIComponent(workflowId)}/hidden`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ hidden: action === 'hide' })
            });
        }
        selectedSessions.clear();
        selectedWorkflows.clear();
        await loadSessions();
        if (window.WorkflowPane) await WorkflowPane.refresh();
    } catch (error) {
        console.error('Batch hide/unhide failed:', error);
    }
}

// 批量删除（会话走 batch 接口，工作流逐个删除）
async function batchDelete() {
    const count = selectedSessions.size + selectedWorkflows.size;
    if (count === 0) return;
    if (!confirm(`确定删除 ${count} 个项目？此操作不可恢复。`)) return;
    const deletingCurrentSession = currentSession && selectedSessions.has(currentSession.session_id);
    const deletingCurrentWorkflow = window.WorkflowPane
        && selectedWorkflows.has(WorkflowPane.getCurrentWorkflowId());
    try {
        if (selectedSessions.size) {
            await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/batch`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_ids: [...selectedSessions], action: 'delete' })
            });
        }
        let failed = 0;
        for (const workflowId of selectedWorkflows) {
            const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/workflows/${encodeURIComponent(workflowId)}`, {
                method: 'DELETE'
            });
            if (!response.ok) failed += 1;
        }
        selectedSessions.clear();
        selectedWorkflows.clear();
        if (deletingCurrentSession) currentSession = null;
        if (deletingCurrentWorkflow) WorkflowPane.close();
        await loadSessions();
        if (window.WorkflowPane) await WorkflowPane.refresh();
        if (failed) showToast(`${failed} 个工作流删除失败（可能正在运行）`);
    } catch (error) {
        console.error('Batch delete failed:', error);
    }
}

// 处理会话操作
async function handleSessionAction(session, action) {
    if (!currentWorkspace) return;

    switch (action) {
        case 'rename':
            await renameSession(session);
            break;
        case 'hide':
            await toggleSessionHidden(session);
            break;
        case 'fork':
            await forkSession(session);
            break;
        case 'about':
            if (window.SessionAbout) window.SessionAbout.open(session);
            break;
        case 'export':
            await exportSession(session);
            break;
        case 'delete':
            await deleteSession(session);
            break;
        case 'share':
            shareSession(session);
            break;
    }
}

// 切换单个会话的隐藏状态
async function toggleSessionHidden(session) {
    try {
        const newHidden = !session.hidden;
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${session.session_id}/hidden`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hidden: newHidden })
        });
        if (response.ok) {
            session.hidden = newHidden;
            renderSidebarList();
        }
    } catch (error) {
        console.error('Failed to toggle hidden:', error);
    }
}

// 修改会话名称
async function renameSession(session) {
    const newName = prompt('请输入新的会话名称:', session.name || '');
    if (!newName || newName === session.name) return;

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${session.session_id}/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName })
        });

        const result = await response.json();
        if (result.success) {
            // 更新当前会话的名称
            if (currentSession && currentSession.session_id === session.session_id) {
                currentSession.name = newName;
            }
            // 刷新会话列表
            await loadSessions();
        } else {
            alert('修改失败: ' + (result.message || '未知错误'));
        }
    } catch (error) {
        console.error('修改会话名称失败:', error);
        alert('修改失败: ' + error.message);
    }
}

// 分支会话：克隆当前 master 会话的已完成回合上下文
async function forkSession(session) {
    if (!currentWorkspace) return;
    const next = prompt('下一阶段要执行什么？（留空只创建分支，不自动执行）', '');
    if (next === null) return;
    showToast('正在创建分支会话…');
    try {
        const resp = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${session.session_id}/fork`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ next_steps: next.trim() })
            }
        );
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            showToast('交接失败: ' + (data.detail || '未知错误'));
            return;
        }
        await loadSessions();
        await loadSession(data.session_id);
        showToast('已创建分支会话');
    } catch (error) {
        console.error('分支会话失败:', error);
        showToast('分支失败: ' + error.message);
    }
}

// 删除会话
async function deleteSession(session) {
    if (!confirm(`确定要删除会话 "${session.preview || session.session_id}" 吗？`)) {
        return;
    }

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${session.session_id}`, {
            method: 'DELETE'
        });
        const result = await response.json();

        if (result.success) {
            // 如果删除的是当前会话，清空聊天区域
            if (currentSession && currentSession.session_id === session.session_id) {
                currentSession = null;
                hideSuggestBar({ invalidate: true });
                savePosition({ session_id: '' });
                chatMessages.innerHTML = '<div class="empty-state">点击 + 新建会话开始</div>';
                sessionRunning = false;
                sessionBackground = { agents: 0, shells: 0, active: 0, items: [] };
                queuedItems = [];
                queuePaused = false;
                queueReason = '';
                updateComposerButtons();
                renderQueueBar();
            }
            // 重新加载会话列表
            await loadSessions();
        } else {
            alert('删除失败: ' + result.message);
        }
    } catch (error) {
        console.error('删除会话失败:', error);
        alert('删除失败: ' + error.message);
    }
}

// 分享会话链接
function shareSession(session) {
    if (!currentWorkspace) return;
    const url = `${window.location.origin}/s/${currentWorkspace.uuid}/${session.session_id}`;
    window.open(url, '_blank');
}

// Load session
async function loadSession(sessionId) {
    if (!currentWorkspace) return;

    if (window.CiliTTS) CiliTTS.stop();  // 切换会话：停止正在进行的朗读
    hideSuggestBar({ invalidate: true });  // 切换会话：收起推荐、作废在途请求
    if (window.ContextGauge) ContextGauge.close();  // 上下文面板随会话切换关闭
    const epoch = ++sessionLoadEpoch;
    const sessionKey = sessionKeyOf(currentWorkspace.uuid, sessionId);
    const sameSession = _renderedSessionKey === sessionKey;
    try {
        // 初始加载最近 50 条消息
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}?limit=50&offset=0`);
        const session = await response.json();
        if (epoch !== sessionLoadEpoch) return;
        currentSession = session;
        // 记录普通会话，切回「会话」模式时用于恢复
        {
            const metadata = session.metadata || {};
            if (!metadata.workflow_panel && !metadata.run_results) {
                _lastSessionModeSessionId = session.session_id;
            }
        }
        if (window.WorkflowPane && typeof WorkflowPane.openForSession === 'function') {
            try { WorkflowPane.openForSession(session); } catch (e) { console.warn('WorkflowPane open failed', e); }
        }
        // 记录已加载的消息偏移量（从末尾算起）
        currentSessionLoadedOffset = session.messages.length;
        currentSessionHasMore = session.has_more;
        savePosition({ session_id: sessionId });

        console.log('Session switched to:', currentSession.session_id);

        if (window.AgentCardStore) {
            AgentCardStore.beginSession(sessionKey, { keepModels: sameSession });
        }

        // 清理 master 工具流；子代理卡片由 AgentCardStore 按会话保留/清理
        clearAllToolStreaming();

        // 重连全局事件流到当前会话（worker 消息 + 工具输出实时推送）
        connectEventSource();

        // 输入框常开：运行中不再禁用，由按钮状态与队列条表达（设计 §5.7.1）
        isSending = false;
        isStopping = false;
        chatInput.disabled = false;

        // Check if agent is running for this session（同时取队列快照）
        const statusResponse = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}/status`);
        const status = await statusResponse.json();
        if (epoch !== sessionLoadEpoch) return;
        sessionRunning = !!status.running;
        sessionBackground = status.background || { agents: 0, shells: 0, active: 0, items: [] };
        queuedItems = status.queue_items || [];
        queuePaused = !!status.queue_paused;
        queueReason = status.queue_reason || '';
        updateComposerButtons();
        refreshModelSelect();
        if (window.ContextGauge) ContextGauge.refresh();
        renderQueueBar();
        renderSidebarList();
        renderMessages(session.messages || []);

        if (window.AgentCardStore) {
            AgentCardStore.reconcile();
            await AgentCardStore.mountRunning();
        }
        _renderedSessionKey = sessionKey;

        // Render initial todos if any（服务端返回的独立 todo 文件优先；
        // metadata.todos 仅旧格式兜底）
        const todos = session.todos ?? session.metadata?.todos;
        if (todos && Array.isArray(todos) && todos.length > 0) {
            renderTodoList(todos);
        } else {
            // Clear any existing todo display
            const existingTodo = document.getElementById('todo-list');
            if (existingTodo) {
                existingTodo.remove();
            }
        }
    } catch (error) {
        console.error('Failed to load session:', error);
    }
}

// Render messages
// Create new session
async function createNewSession() {
    if (!currentWorkspace) {
        alert('请先选择工作区');
        return;
    }

    hideSuggestBar({ invalidate: true });  // 新建会话：收起推荐、作废在途请求
    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: '新会话' })
        });

        const session = await response.json();
        currentSession = session;
        _lastSessionModeSessionId = session.session_id;
        savePosition({ session_id: session.session_id });

        await loadSessions();
        chatMessages.innerHTML = newSessionWelcomeHtml();

        // 完整重置发送/停止状态（可能在旧会话运行中创建新会话）
        clearAllToolStreaming();
        if (window.AgentCardStore) {
            AgentCardStore.clear();
        }
        _renderedSessionKey = null;
        // 重连事件流到新会话
        connectEventSource();
        chatInput.disabled = false;
        isSending = false;
        sessionRunning = false;
        sessionBackground = { agents: 0, shells: 0, active: 0, items: [] };
        queuedItems = [];
        queuePaused = false;
        queueReason = '';
        isStopping = false;
        updateComposerButtons();
        refreshModelSelect();
        if (window.ContextGauge) ContextGauge.refresh();
        renderQueueBar();
        chatInput.focus();
    } catch (error) {
        console.error('Failed to create session:', error);
        alert('创建会话失败');
    }
}

// ---------- Workspace Settings ----------
