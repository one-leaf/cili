// State
let currentWorkspace = null;  // {uuid, name, directory}
let currentSession = null;
let sessions = [];
let isSending = false;
let isMultiSelectMode = false;
let showHiddenSessions = false;
let selectedSessions = new Set();
let pendingImages = [];  // [{ data: "base64...", media_type: "image/png", preview_url: "data:..." }]


// DOM elements
const workspaceSelect = document.getElementById('workspace-select');
const workspaceSettingsBtn = document.getElementById('workspace-settings-btn');
const newWorkspaceBtn = document.getElementById('new-workspace-btn');
const sessionsList = document.getElementById('sessions-list');
const chatMessages = document.getElementById('chat-messages');
const chatInput = document.getElementById('chat-input');
const sendBtn = document.getElementById('send-btn');
const newSessionBtn = document.getElementById('new-session-btn');
const sessionMenuBtn = document.getElementById('session-menu-btn');

// 聊天区域的链接在新标签页打开（防止离开聊天界面丢失状态）
chatMessages.addEventListener('click', (e) => {
    const a = e.target.closest('a');
    if (a) {
        e.preventDefault();
        window.open(a.href, '_blank', 'noopener,noreferrer');
    }
});

// 输入面板拖拽调整大小
const resizeHandle = document.querySelector('.input-resize-handle');
const chatInputArea = document.querySelector('.chat-input-area');
const INPUT_MIN_HEIGHT = 80;
const INPUT_MAX_HEIGHT = 500;
const INPUT_DEFAULT_HEIGHT = 0; // 0 = 使用默认内容高度
const STORAGE_KEY = 'chat-input-height';

function initInputResize() {
    // 恢复保存的高度
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
        const h = parseInt(saved);
        if (h >= INPUT_MIN_HEIGHT && h <= INPUT_MAX_HEIGHT) {
            chatInputArea.style.height = h + 'px';
            chatInput.style.height = (h - 40) + 'px'; // 减去 padding
        }
    }

    let startY, startH, dragging = false;

    resizeHandle.addEventListener('mousedown', (e) => {
        e.preventDefault();
        dragging = true;
        startY = e.clientY;
        startH = chatInputArea.getBoundingClientRect().height;
        resizeHandle.classList.add('dragging');
        document.body.style.cursor = 'ns-resize';
        document.body.style.userSelect = 'none';
    });

    document.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const delta = startY - e.clientY;
        const newH = Math.min(INPUT_MAX_HEIGHT, Math.max(INPUT_MIN_HEIGHT, startH + delta));
        chatInputArea.style.height = newH + 'px';
        chatInput.style.height = (newH - 40) + 'px'; // 减去上下 padding
        localStorage.setItem(STORAGE_KEY, newH);
    });

    document.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        resizeHandle.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
    });

    // 双击重置
    resizeHandle.addEventListener('dblclick', () => {
        chatInputArea.style.height = '';
        chatInput.style.height = '';
        localStorage.removeItem(STORAGE_KEY);
    });
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    loadWorkspaces();
    loadFooter();
    setupEventListeners();
    initInputResize();
    initSidebarState();
});

// Sidebar collapse / expand
function initSidebarState() {
    if (localStorage.getItem('sidebar-collapsed') === 'true') {
        const sidebar = document.querySelector('.sidebar');
        const toggleBtn = document.getElementById('sidebar-toggle-btn');
        const openBtn = document.getElementById('sidebar-open-btn');
        sidebar.classList.add('collapsed');
        toggleBtn.style.display = 'none';
        openBtn.style.display = '';
    }
}

function toggleSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const toggleBtn = document.getElementById('sidebar-toggle-btn');
    const openBtn = document.getElementById('sidebar-open-btn');
    const isCollapsed = sidebar.classList.toggle('collapsed');
    toggleBtn.style.display = isCollapsed ? 'none' : '';
    openBtn.style.display = isCollapsed ? '' : 'none';
    localStorage.setItem('sidebar-collapsed', isCollapsed);
}

// Load footer info from JSON
async function loadFooter() {
    try {
        const response = await fetch('/static/footer.json');
        const data = await response.json();
        const footerContent = document.getElementById('footer-content');
        if (footerContent) {
            footerContent.innerHTML = `
                <span class="footer-dev">${data.app_name}</span>
                <span class="footer-sep">by</span>
                <span class="footer-company">${data.author}</span>
                <span class="footer-sep">·</span>
                <span class="footer-version">${data.version}</span>
            `;
        }
    } catch (error) {
        console.error('Failed to load footer:', error);
    }
}

// Setup event listeners
function setupEventListeners() {
    // 文件管理器事件
    setupFileManagerEvents();

    workspaceSelect.addEventListener('change', handleWorkspaceChange);
    workspaceSettingsBtn.addEventListener('click', openWorkspaceSettings);
    newWorkspaceBtn.addEventListener('click', handleNewWorkspace);
    newSessionBtn.addEventListener('click', createNewSession);
    sessionMenuBtn.addEventListener('click', toggleSessionPanelMenu);
    document.getElementById('sidebar-toggle-btn').addEventListener('click', toggleSidebar);
    document.getElementById('sidebar-open-btn').addEventListener('click', toggleSidebar);
    sendBtn.addEventListener('click', () => {
        if (isSending) {
            stopAgent();
        } else {
            sendMessage();
        }
    });
    chatInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    // Paste image support (with compression)
    chatInput.addEventListener('paste', async (e) => {
        const items = e.clipboardData?.items;
        if (!items) return;
        for (const item of items) {
            if (item.type.startsWith('image/')) {
                e.preventDefault();
                const file = item.getAsFile();
                if (!file) continue;
                // 压缩图片
                const result = await compressImage(file);
                if (!result.data) continue;
                // 检查压缩后大小，超过2MB再降低质量
                if (result.size > MAX_IMAGE_SIZE_BYTES) {
                    const retry = await compressImage(file, 0.5);
                    if (retry.data) pendingImages.push({ data: retry.data, media_type: 'image/jpeg', preview_url: retry.preview_url });
                } else {
                    pendingImages.push({ data: result.data, media_type: 'image/jpeg', preview_url: result.preview_url });
                }
                renderImagePreviews();
                break;  // Only take the first image
            }
        }
    });

    // Global settings
    document.getElementById('settings-btn').addEventListener('click', openSettings);
    document.getElementById('settings-save-btn').addEventListener('click', saveSettings);
    document.getElementById('model-test-btn').addEventListener('click', () => testSettings('model'));
    document.getElementById('llm-test-btn').addEventListener('click', () => testSettings('llm'));

    // Upgrade
    document.getElementById('upgrade-check-btn').addEventListener('click', runUpgrade);

    // Workspace settings
    document.getElementById('workspace-settings-save-btn').addEventListener('click', saveWorkspaceSettings);
    document.getElementById('workspace-delete-btn').addEventListener('click', deleteWorkspaceConfig);
    document.getElementById('ws-browse-dir-btn').addEventListener('click', browseDirectory);
    document.getElementById('new-ws-browse-dir-btn').addEventListener('click', browseDirectory);

    // File insert button
    document.getElementById('insert-file-btn').addEventListener('click', () => {
        openFileBrowser();
    });

    // Settings tabs
    document.querySelectorAll('.settings-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById(`tab-${tab.dataset.tab}`).classList.add('active');
        });
    });

    // API Key visibility toggle
    document.querySelectorAll('.toggle-password').forEach(btn => {
        btn.addEventListener('click', () => {
            const targetId = btn.getAttribute('data-target');
            const input = document.getElementById(targetId);
            if (input.type === 'password') {
                input.type = 'text';
                btn.classList.add('active');
            } else {
                input.type = 'password';
                btn.classList.remove('active');
            }
        });
    });

    // Temperature slider live value display
    document.getElementById('setting-temperature').addEventListener('input', (e) => {
        document.getElementById('setting-temperature-value').textContent = e.target.value;
    });
    document.getElementById('llm-temperature').addEventListener('input', (e) => {
        document.getElementById('llm-temperature-value').textContent = e.target.value;
    });

    // New workspace
    document.getElementById('new-workspace-create-btn').addEventListener('click', createNewWorkspace);
    document.getElementById('new-workspace-cancel-btn').addEventListener('click', () => {
        document.getElementById('new-workspace-modal').style.display = 'none';
    });

    // Generic modal close handlers
    document.querySelectorAll('.modal-close').forEach(btn => {
        btn.addEventListener('click', () => {
            const modalId = btn.getAttribute('data-modal');
            document.getElementById(modalId).style.display = 'none';
        });
    });
    document.querySelectorAll('.modal-backdrop').forEach(backdrop => {
        backdrop.addEventListener('click', () => {
            const modalId = backdrop.getAttribute('data-modal');
            // 设置窗口不响应点击外部关闭
            if (modalId === 'settings-modal' || modalId === 'workspace-settings-modal') return;
            document.getElementById(modalId).style.display = 'none';
        });
    });
}

// Load workspaces
async function loadWorkspaces() {
    try {
        const response = await fetch('/api/workspaces');
        const data = await response.json();

        workspaceSelect.innerHTML = '<option value="">选择工作区...</option>';

        // 默认禁用工作区相关按钮
        workspaceSettingsBtn.disabled = true;
        document.getElementById('file-manager-btn').disabled = true;

        // 先渲染普通工作区
        data.workspaces.filter(ws => !ws.system).forEach(ws => {
            const option = document.createElement('option');
            option.value = ws.uuid;
            option.textContent = ws.name;
            workspaceSelect.appendChild(option);
        });

        // System 放在最后
        const systemWs = data.workspaces.filter(ws => ws.system);
        systemWs.forEach(ws => {
            const option = document.createElement('option');
            option.value = ws.uuid;
            option.textContent = ws.name;
            workspaceSelect.appendChild(option);
        });

        // If no workspaces, show hint
        if (data.workspaces.length === 0) {
            sessionsList.innerHTML = '<div class="empty-state">点击 + 创建工作区</div>';
            workspaceSettingsBtn.disabled = true;
            document.getElementById('file-manager-btn').disabled = true;
            return;
        }

        // 恢复上次访问的工作区
        const saved = readPosition();
        if (saved.workspace_uuid) {
            const wsExists = data.workspaces.some(ws => ws.uuid === saved.workspace_uuid);
            if (wsExists) {
                workspaceSelect.value = saved.workspace_uuid;
                await handleWorkspaceChange();  // 会自动加载保存的或第一个会话
            }
        }
    } catch (error) {
        console.error('Failed to load workspaces:', error);
    }
}

// Format workspace path for display (truncate middle if too long)
function formatWorkspacePath(path) {
    if (!path) return '';
    const maxLength = 30;
    if (path.length <= maxLength) return path;

    // Keep head and tail, truncate middle
    const headLength = Math.floor(maxLength / 2);
    const tailLength = maxLength - headLength - 3; // 3 for "..."
    const head = path.substring(0, headLength);
    const tail = path.substring(path.length - tailLength);
    return `${head}...${tail}`;
}

// Update workspace path display
function updateWorkspacePath() {
    const pathEl = document.getElementById('workspace-path');
    if (!pathEl) return;

    if (!currentWorkspace || !currentWorkspace.directory) {
        pathEl.textContent = '工作区路径';  // 显示占位文字
    } else {
        pathEl.textContent = formatWorkspacePath(currentWorkspace.directory);
    }
}

// Handle workspace change
async function handleWorkspaceChange() {
    const selectedUuid = workspaceSelect.value;

    if (!selectedUuid) {
        currentWorkspace = null;
        currentSession = null;
        clearPosition();
        workspaceSettingsBtn.disabled = true;
        document.getElementById('file-manager-btn').disabled = true;
        sessionsList.innerHTML = '<div class="empty-state">选择一个工作区</div>';
        chatMessages.innerHTML = '<div class="welcome-message"><h2>欢迎使用草履虫</h2><p>选择工作区并创建会话开始使用</p></div>';
        updateWorkspacePath();
        return;
    }

    // Enable workspace settings button
    workspaceSettingsBtn.disabled = false;
    document.getElementById('file-manager-btn').disabled = false;

    // Find the workspace object from the list
    try {
        const response = await fetch('/api/workspaces');
        const data = await response.json();
        currentWorkspace = data.workspaces.find(ws => ws.uuid === selectedUuid) || null;
    } catch (error) {
        console.error('Failed to get workspace info:', error);
        currentWorkspace = { uuid: selectedUuid, name: selectedUuid };
    }

    currentSession = null;
    isMultiSelectMode = false;
    showHiddenSessions = false;
    selectedSessions.clear();
    newSessionBtn.style.display = '';
    if (_footerOriginalHTML !== null) {
        document.getElementById('footer-content').innerHTML = _footerOriginalHTML;
        _footerOriginalHTML = null;
    }
    savePosition({ workspace_uuid: selectedUuid });

    if (!currentWorkspace) {
        updateWorkspacePath();
        sessionsList.innerHTML = '<div class="empty-state">工作区未找到</div>';
        return;
    }

    updateWorkspacePath();
    await loadSessions();

    // 自动加载最近的会话（如果有）
    if (sessions.length > 0) {
        // 优先加载该工作区上次访问的会话，否则加载第一个
        const saved = readPosition();
        const savedSessionId = saved.ws_sessions?.[selectedUuid];
        const targetSession = savedSessionId && sessions.some(s => s.session_id === savedSessionId)
            ? savedSessionId
            : sessions[0].session_id;
        await loadSession(targetSession);
    } else {
        // 没有会话时禁用输入框
        chatInput.disabled = true;
        sendBtn.disabled = true;
        chatMessages.innerHTML = '<div class="welcome-message"><h2>欢迎使用草履虫</h2><p>点击 + 创建新会话</p></div>';
    }
}

// Handle new workspace button
function handleNewWorkspace() {
    document.getElementById('new-workspace-modal').style.display = 'flex';
    document.getElementById('new-ws-name').value = '';
    document.getElementById('new-ws-directory').value = '';
    document.getElementById('new-workspace-status').textContent = '';
    document.getElementById('new-ws-name').focus();
}

// Create new workspace
async function createNewWorkspace() {
    const statusEl = document.getElementById('new-workspace-status');
    statusEl.textContent = '创建中...';

    const name = document.getElementById('new-ws-name').value.trim();
    const directory = document.getElementById('new-ws-directory').value.trim();

    if (!name) {
        statusEl.textContent = '请输入工作区名称';
        return;
    }

    try {
        const response = await fetch('/api/workspaces', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, directory })
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || '创建失败');
        }

        const workspace = await response.json();
        statusEl.textContent = '✓ 创建成功';

        // 关闭 modal
        document.getElementById('new-workspace-modal').style.display = 'none';

        // 刷新工作区列表
        await loadWorkspaces();

        // 选中新创建的工作区
        workspaceSelect.value = workspace.uuid;
        await handleWorkspaceChange();
    } catch (error) {
        statusEl.textContent = '✗ ' + error.message;
    }
}

// Load sessions
async function loadSessions() {
    if (!currentWorkspace) return;

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions`);
        const data = await response.json();
        sessions = data.sessions;
        renderSessions();
    } catch (error) {
        console.error('Failed to load sessions:', error);
        sessionsList.innerHTML = '<div class="empty-state">加载会话失败</div>';
    }
}

// Render sessions list
function renderSessions() {
    // 根据 showHiddenSessions 过滤
    const filtered = sessions.filter(s => !!s.hidden === showHiddenSessions);

    if (filtered.length === 0) {
        sessionsList.innerHTML = `<div class="empty-state">${showHiddenSessions ? '没有隐藏的会话' : '暂无会话'}</div>`;
        return;
    }

    sessionsList.innerHTML = '';
    filtered.forEach(session => {
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

        // 显示逻辑：如果name是"新会话"，则显示最后一句对话；否则显示name
        const displayName = (session.name === '新会话' && session.preview) ? session.preview : (session.name || session.preview || '未命名');
        const preview = document.createElement('div');
        preview.className = 'session-preview';
        preview.textContent = displayName;
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

        sessionsList.appendChild(div);
    });
}

// 保存的原始 footer 内容
let _footerOriginalHTML = null;

// 更新底部工具条（复用 footer-content）
function updateFooterToolbar() {
    const footerContent = document.getElementById('footer-content');
    if (isMultiSelectMode) {
        const count = selectedSessions.size;
        const hideLabel = showHiddenSessions ? '取消隐藏' : '隐藏';
        footerContent.innerHTML = `
            <button id="footer-batch-hide" class="btn btn-small" ${count === 0 ? 'disabled' : ''}>${hideLabel}</button>
            <button id="footer-batch-delete" class="btn btn-small btn-danger" ${count === 0 ? 'disabled' : ''}>删除</button>
            <button id="footer-exit-multi" class="btn btn-small">取消</button>
        `;
        document.getElementById('footer-batch-hide').addEventListener('click', batchHideUnhide);
        document.getElementById('footer-batch-delete').addEventListener('click', batchDelete);
        document.getElementById('footer-exit-multi').addEventListener('click', exitMultiSelect);
    } else if (_footerOriginalHTML !== null) {
        footerContent.innerHTML = _footerOriginalHTML;
        _footerOriginalHTML = null;
    }
}

// 退出多选模式
function exitMultiSelect() {
    isMultiSelectMode = false;
    selectedSessions.clear();
    newSessionBtn.style.display = '';
    updateFooterToolbar();
    renderSessions();
}

// 切换单个会话项的右键菜单
function toggleSessionItemMenu(session, btn) {
    // 关闭所有已打开的菜单
    document.querySelectorAll('.session-dropdown').forEach(menu => menu.remove());

    // 创建新的下拉菜单
    const dropdown = document.createElement('div');
    dropdown.className = 'session-dropdown show';

    const hideLabel = session.hidden ? '取消隐藏' : '隐藏会话';
    dropdown.innerHTML = `
        <div class="session-dropdown-item" data-action="rename">修改名称</div>
        <div class="session-dropdown-item" data-action="hide">${hideLabel}</div>
        <div class="session-dropdown-item" data-action="delete">删除会话</div>
        <div class="session-dropdown-item" data-action="share">分享会话</div>
        <div class="session-dropdown-item" data-action="info">会话信息</div>
    `;

    // 添加菜单项点击事件
    dropdown.querySelectorAll('.session-dropdown-item').forEach(item => {
        item.addEventListener('click', (e) => {
            e.stopPropagation();
            handleSessionAction(session, item.dataset.action);
            dropdown.remove();
        });
    });

    btn.parentElement.appendChild(dropdown);

    // 添加全局点击事件关闭菜单
    setTimeout(() => {
        document.addEventListener('click', closeSessionMenus, { once: true });
    }, 0);
}

// 关闭所有会话菜单
function closeSessionMenus() {
    document.querySelectorAll('.session-dropdown').forEach(menu => menu.remove());
}

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

    const viewLabel = showHiddenSessions ? '退出隐藏' : '隐藏';
    const multiLabel = isMultiSelectMode ? '退出多选' : '多选';

    dropdown.innerHTML = `
        <div class="session-dropdown-item" data-panel-action="toggle-view">${viewLabel}</div>
        <div class="session-dropdown-item" data-panel-action="toggle-multi">${multiLabel}</div>
    `;

    dropdown.querySelectorAll('.session-dropdown-item').forEach(item => {
        item.addEventListener('click', (ev) => {
            ev.stopPropagation();
            const action = item.dataset.panelAction;
            dropdown.remove();
            if (action === 'toggle-view') {
                showHiddenSessions = !showHiddenSessions;
                renderSessions();
            } else if (action === 'toggle-multi') {
                if (isMultiSelectMode) {
                    exitMultiSelect();
                } else {
                    isMultiSelectMode = true;
                    selectedSessions.clear();
                    newSessionBtn.style.display = 'none';
                    // 保存 footer 原始内容
                    const footerContent = document.getElementById('footer-content');
                    _footerOriginalHTML = footerContent.innerHTML;
                    updateFooterToolbar();
                    renderSessions();
                }
            }
        });
    });

    sessionMenuBtn.parentElement.appendChild(dropdown);

    setTimeout(() => {
        document.addEventListener('click', () => dropdown.remove(), { once: true });
    }, 0);
}

// 批量隐藏/取消隐藏
async function batchHideUnhide() {
    if (selectedSessions.size === 0) return;
    const action = showHiddenSessions ? 'unhide' : 'hide';
    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/batch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_ids: [...selectedSessions], action })
        });
        if (response.ok) {
            selectedSessions.clear();
            await loadSessions();
        }
    } catch (error) {
        console.error('Batch hide/unhide failed:', error);
    }
}

// 批量删除
async function batchDelete() {
    if (selectedSessions.size === 0) return;
    const count = selectedSessions.size;
    if (!confirm(`确定删除 ${count} 个会话？此操作不可恢复。`)) return;
    const deletingCurrent = currentSession && selectedSessions.has(currentSession.session_id);
    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/batch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_ids: [...selectedSessions], action: 'delete' })
        });
        if (response.ok) {
            selectedSessions.clear();
            if (deletingCurrent) {
                currentSession = null;
            }
            await loadSessions();
        }
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
        case 'delete':
            await deleteSession(session);
            break;
        case 'share':
            shareSession(session);
            break;
        case 'info':
            await showSessionInfo(session);
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
            renderSessions();
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
                savePosition({ session_id: '' });
                chatMessages.innerHTML = '<div class="empty-state">请选择或创建会话</div>';
                chatInput.disabled = true;
                sendBtn.disabled = true;
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

// 显示会话信息
async function showSessionInfo(session) {
    if (!currentWorkspace) return;

    try {
        // 获取完整会话数据
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${session.session_id}`);
        const data = await response.json();

        const messages = data.messages || [];
        const userMessages = messages.filter(m => m.role === 'user').length;
        const assistantMessages = messages.filter(m => m.role === 'assistant').length;
        const metadata = data.metadata || {};
        const usage = metadata.usage || {};

        // 创建模态框
        const modal = document.createElement('div');
        modal.className = 'modal';
        modal.innerHTML = `
            <div class="modal-backdrop" onclick="this.parentElement.remove()"></div>
            <div class="modal-content" style="max-width: 550px;">
                <div class="modal-header">
                    <h2>会话信息</h2>
                    <button class="modal-close" onclick="this.closest('.modal').remove()">&times;</button>
                </div>
                <div class="session-info-content">
                    <table class="session-info-table">
                        <tbody>
                            <tr><td class="info-key">会话ID</td><td class="info-value">${session.session_id}</td></tr>
                            <tr><td class="info-key">会话名称</td><td class="info-value">${session.name || '未命名'}</td></tr>
                            <tr><td class="info-key">创建时间</td><td class="info-value">${metadata.created_at || '未知'}</td></tr>
                            <tr><td class="info-key">更新时间</td><td class="info-value">${metadata.updated_at || '未知'}</td></tr>
                            <tr><td class="info-key">总消息数</td><td class="info-value">${messages.length} 条</td></tr>
                            <tr><td class="info-key">用户消息</td><td class="info-value">${userMessages} 条</td></tr>
                            <tr><td class="info-key">助手消息</td><td class="info-value">${assistantMessages} 条</td></tr>
                            <tr><td class="info-key">API调用次数</td><td class="info-value">${usage.api_calls || 0}</td></tr>
                            <tr><td class="info-key">输入Tokens</td><td class="info-value">${(usage.input_tokens || 0).toLocaleString()}</td></tr>
                            <tr><td class="info-key">输出Tokens</td><td class="info-value">${(usage.output_tokens || 0).toLocaleString()}</td></tr>
                            <tr><td class="info-key">缓存读取Tokens</td><td class="info-value">${(usage.cache_read_tokens || 0).toLocaleString()}</td></tr>
                            <tr><td class="info-key">缓存创建Tokens</td><td class="info-value">${(usage.cache_creation_tokens || 0).toLocaleString()}</td></tr>
                        </tbody>
                    </table>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-primary" onclick="this.closest('.modal').remove()">关闭</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);

    } catch (error) {
        console.error('获取会话信息失败:', error);
        alert('获取会话信息失败: ' + error.message);
    }
}

// Load session
async function loadSession(sessionId) {
    if (!currentWorkspace) return;

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}`);
        const session = await response.json();
        currentSession = session;
        savePosition({ session_id: sessionId });

        console.log('Session switched to:', currentSession.session_id);

        // 清理所有正在进行的工具输出轮询
        clearAllToolStreaming();

        // Enable input by default when selecting a session
        chatInput.disabled = false;
        sendBtn.disabled = false;
        sendBtn.textContent = '发送';
        sendBtn.classList.remove('btn-danger');
        sendBtn.classList.add('btn-primary');
        isSending = false;

        // Check if agent is running for this session
        const statusResponse = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}/status`);
        const status = await statusResponse.json();
        if (status.running) {
            // Agent is running, disable input and show stop button
            chatInput.disabled = true;
            sendBtn.textContent = '停止';
            sendBtn.classList.remove('btn-primary');
            sendBtn.classList.add('btn-danger');
            isSending = true;
        }

        renderSessions();
        renderMessages(session.messages || []);

        // Render initial todos if any
        const todos = session.metadata?.todos;
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

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: '新会话' })
        });

        const session = await response.json();
        currentSession = session;
        savePosition({ session_id: session.session_id });

        await loadSessions();
        chatMessages.innerHTML = '<div class="welcome-message"><h2>开始新对话</h2><p>输入消息开始使用</p></div>';
        chatInput.disabled = false;
        sendBtn.disabled = false;
        chatInput.focus();
    } catch (error) {
        console.error('Failed to create session:', error);
        alert('创建会话失败');
    }
}

// ---------- Workspace Settings ----------

async function openWorkspaceSettings() {
    if (!currentWorkspace) return;

    document.getElementById('workspace-settings-modal').style.display = 'flex';
    const statusEl = document.getElementById('workspace-settings-status');
    statusEl.textContent = `工作区 UUID: ${currentWorkspace.uuid}`;

    const nameInput = document.getElementById('ws-setting-name');
    const dirInput = document.getElementById('ws-setting-directory');
    const deleteBtn = document.getElementById('workspace-delete-btn');
    const saveBtn = document.getElementById('workspace-settings-save-btn');

    nameInput.value = currentWorkspace.name || '';
    dirInput.value = currentWorkspace.directory || '';

    // System workspace: read-only, no delete
    const isSystem = currentWorkspace.system || currentWorkspace.uuid === 'system';
    nameInput.disabled = isSystem;
    dirInput.disabled = isSystem;
    deleteBtn.style.display = isSystem ? 'none' : '';
    saveBtn.style.display = isSystem ? 'none' : '';

    if (isSystem) {
        statusEl.textContent = `工作区 UUID: ${currentWorkspace.uuid}（系统工作区，不可修改）`;
    }
}


async function saveWorkspaceSettings() {
    if (!currentWorkspace) return;

    const statusEl = document.getElementById('workspace-settings-status');
    statusEl.textContent = '保存中...';

    const name = document.getElementById('ws-setting-name').value.trim();
    const directory = document.getElementById('ws-setting-directory').value.trim();

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, directory })
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Failed to save');
        }

        statusEl.textContent = '✓ 保存成功';

        // Update current workspace info
        currentWorkspace.name = name;
        currentWorkspace.directory = directory;

        // Refresh workspace path display
        updateWorkspacePath();

        // Refresh workspace list to update the dropdown
        await loadWorkspaces();

        // Re-select the current workspace
        workspaceSelect.value = currentWorkspace.uuid;

        setTimeout(() => {
            document.getElementById('workspace-settings-modal').style.display = 'none';
        }, 1000);
    } catch (error) {
        statusEl.textContent = '✗ 保存失败: ' + error.message;
    }
}

async function deleteWorkspaceConfig() {
    if (!currentWorkspace) return;

    const wsName = currentWorkspace.name || currentWorkspace.uuid;
    const confirmed = confirm(
        `确定要删除工作区「${wsName}」的配置吗？\n\n` +
        `⚠ 将删除以下内容：\n` +
        `• 工作区配置文件\n` +
        `• 该工作区下的所有会话记录\n\n` +
        `✓ 工作区内的文件不会被删除\n\n` +
        `此操作不可恢复！`
    );

    if (!confirmed) return;

    const statusEl = document.getElementById('workspace-settings-status');
    statusEl.textContent = '删除中...';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/reset`, {
            method: 'POST'
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Failed to delete');
        }

        statusEl.textContent = '✓ 删除成功';

        // Close modal and reset UI
        setTimeout(() => {
            document.getElementById('workspace-settings-modal').style.display = 'none';
            currentWorkspace = null;
            currentSession = null;
            clearPosition();
            workspaceSelect.value = '';
            sessionsList.innerHTML = '<div class="empty-state">选择一个工作区</div>';
            chatMessages.innerHTML = '<div class="welcome-message"><h2>欢迎使用草履虫</h2><p>选择工作区并创建会话开始使用</p></div>';
            loadWorkspaces();
        }, 800);
    } catch (error) {
        statusEl.textContent = '✗ 删除失败: ' + error.message;
    }
}
