// State
let currentWorkspace = null;  // {uuid, name, directory}
let currentSession = null;
let sessions = [];
let isSending = false;
let isMultiSelectMode = false;
let showHiddenSessions = false;
let selectedSessions = new Set();
let pendingImages = [];  // [{ data: "base64...", media_type: "image/png", preview_url: "data:..." }]

// ── localStorage 位置持久化 ──
const POSITION_KEY = 'cili_last_position';

function savePosition({ workspace_uuid, session_id } = {}) {
    try {
        const current = JSON.parse(localStorage.getItem(POSITION_KEY) || '{}');
        if (workspace_uuid !== undefined) current.workspace_uuid = workspace_uuid;
        if (session_id !== undefined) current.session_id = session_id;
        localStorage.setItem(POSITION_KEY, JSON.stringify(current));
    } catch (e) { /* localStorage 不可用则忽略 */ }
}

function readPosition() {
    try { return JSON.parse(localStorage.getItem(POSITION_KEY) || '{}'); }
    catch (e) { return {}; }
}

function clearPosition() {
    try { localStorage.removeItem(POSITION_KEY); } catch (e) {}
}

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

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    loadWorkspaces();
    loadFooter();
    setupEventListeners();
});

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
    workspaceSelect.addEventListener('change', handleWorkspaceChange);
    workspaceSettingsBtn.addEventListener('click', openWorkspaceSettings);
    newWorkspaceBtn.addEventListener('click', handleNewWorkspace);
    newSessionBtn.addEventListener('click', createNewSession);
    sessionMenuBtn.addEventListener('click', toggleSessionPanelMenu);
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
    // Paste image support
    chatInput.addEventListener('paste', (e) => {
        const items = e.clipboardData?.items;
        if (!items) return;
        for (const item of items) {
            if (item.type.startsWith('image/')) {
                e.preventDefault();
                const file = item.getAsFile();
                if (!file) continue;
                const reader = new FileReader();
                reader.onload = (ev) => {
                    const dataUrl = ev.target.result;  // data:image/png;base64,xxx
                    const commaIdx = dataUrl.indexOf(',');
                    const data = dataUrl.substring(commaIdx + 1);
                    const mediaType = file.type || 'image/png';
                    pendingImages.push({ data, media_type: mediaType, preview_url: dataUrl });
                    renderImagePreviews();
                };
                reader.readAsDataURL(file);
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

// Render image preview thumbnails in the input area
function renderImagePreviews() {
    const area = document.getElementById('image-preview-area');
    if (!area) return;
    area.innerHTML = '';
    if (pendingImages.length === 0) {
        area.classList.remove('visible');
        return;
    }
    area.classList.add('visible');
    pendingImages.forEach((img, idx) => {
        const wrapper = document.createElement('div');
        wrapper.className = 'image-preview-thumb';

        const imgEl = document.createElement('img');
        imgEl.src = img.preview_url;
        imgEl.alt = '待发送图片';
        wrapper.appendChild(imgEl);

        const removeBtn = document.createElement('button');
        removeBtn.className = 'image-preview-remove';
        removeBtn.textContent = '×';
        removeBtn.title = '移除图片';
        removeBtn.onclick = () => {
            pendingImages.splice(idx, 1);
            renderImagePreviews();
        };
        wrapper.appendChild(removeBtn);
        area.appendChild(wrapper);
    });
}

// Load workspaces
async function loadWorkspaces() {
    try {
        const response = await fetch('/api/workspaces');
        const data = await response.json();

        workspaceSelect.innerHTML = '<option value="">选择工作区...</option>';

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
            return;
        }

        // 恢复上次访问的工作区
        const saved = readPosition();
        if (saved.workspace_uuid) {
            const wsExists = data.workspaces.some(ws => ws.uuid === saved.workspace_uuid);
            if (wsExists) {
                workspaceSelect.value = saved.workspace_uuid;
                await handleWorkspaceChange();
                // handleWorkspaceChange 会清空 session_id，用本地变量恢复
                if (saved.session_id && sessions.some(s => s.session_id === saved.session_id)) {
                    await loadSession(saved.session_id);
                }
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
        sessionsList.innerHTML = '<div class="empty-state">选择一个工作区</div>';
        chatMessages.innerHTML = '<div class="welcome-message"><h2>欢迎使用草履虫</h2><p>选择工作区并创建会话开始使用</p></div>';
        updateWorkspacePath();
        return;
    }

    // Enable workspace settings button
    workspaceSettingsBtn.disabled = false;

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
    savePosition({ workspace_uuid: selectedUuid, session_id: '' });

    if (!currentWorkspace) {
        updateWorkspacePath();
        sessionsList.innerHTML = '<div class="empty-state">工作区未找到</div>';
        return;
    }

    updateWorkspacePath();
    await loadSessions();
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
        <div class="session-dropdown-item" data-action="export">导出会话</div>
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
        case 'export':
            await exportSession(session);
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

// 导出会话为HTML
async function exportSession(session) {
    if (!currentWorkspace) return;

    try {
        // 获取完整会话数据
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${session.session_id}`);
        const data = await response.json();

        // 获取会话列表中的元数据
        const sessionMeta = sessions.find(s => s.session_id === session.session_id);

        // 创建新窗口
        const newWindow = window.open('', '_blank');
        if (!newWindow) {
            alert('无法打开新窗口，请检查浏览器是否阻止了弹出窗口');
            return;
        }

        // 生成消息HTML，助手消息存储原始markdown供JS渲染
        let messagesHtml = '';
        const messages = data.messages || [];
        const assistantTexts = [];

        // 辅助函数：检查消息是否包含工具调用
        function hasToolUse(content) {
            if (!Array.isArray(content)) return false;
            return content.some(block => block.type === 'tool_use' || block.type === 'tool_call');
        }

        for (const msg of messages) {
            if (msg.role === 'user') {
                // 用户消息：只提取纯文本，不包含工具结果
                const content = msg.content;
                let text = '';
                if (typeof content === 'string') {
                    text = content;
                } else if (Array.isArray(content)) {
                    // 检查是否包含工具结果，如果有则跳过
                    const hasToolResult = content.some(b => b.type === 'tool_result');
                    if (!hasToolResult) {
                        text = content
                            .filter(block => block.type === 'text')
                            .map(block => block.text || '')
                            .join('\n');
                    }
                }
                if (text) {
                    messagesHtml += `
                        <div class="message user">
                            <div class="message-role">用户</div>
                            <div class="message-content">${escapeHtml(text)}</div>
                        </div>
                    `;
                }
            } else if (msg.role === 'assistant') {
                // 助手消息：只显示不包含工具调用的纯文本消息
                const content = msg.content;
                if (!hasToolUse(content)) {
                    const text = extractTextContent(content);
                    if (text) {
                        const idx = assistantTexts.length;
                        assistantTexts.push(text);
                        messagesHtml += `
                            <div class="message assistant">
                                <div class="message-role">助手</div>
                                <div class="message-content md-content" data-idx="${idx}"></div>
                            </div>
                        `;
                    }
                }
            }
        }

        // 将助手原始文本序列化为JSON供页面JS读取
        const assistantTextsJson = JSON.stringify(assistantTexts);

        const html = `
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>${escapeHtml(sessionMeta?.preview || '会话导出')}</title>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"><\/script>
    <script>
        window.MathJax = {
            tex: {
                inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
                displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
                processEscapes: true,
                processEnvironments: true
            },
            options: {
                skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
            }
        };
    <\/script>
    <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"><\/script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: #f5f5f5;
            color: #333;
            line-height: 1.6;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            padding: 30px 20px;
        }
        .header {
            background: white;
            border-radius: 8px;
            padding: 20px 24px;
            margin-bottom: 20px;
            border-left: 4px solid #4a90e2;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        }
        .header h1 {
            color: #4a90e2;
            font-size: 20px;
            margin-bottom: 10px;
        }
        .header .meta {
            color: #888;
            font-size: 12px;
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
        }
        .message {
            margin-bottom: 16px;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        }
        .message.user {
            background: #e3f2fd;
            border-left: 3px solid #4a90e2;
        }
        .message.assistant {
            background: white;
            border-left: 3px solid #34a853;
        }
        .message-role {
            font-weight: 600;
            font-size: 12px;
            padding: 10px 16px 0;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .message.user .message-role { color: #4a90e2; }
        .message.assistant .message-role { color: #34a853; }
        .message-content {
            padding: 8px 16px 16px;
            font-size: 14px;
            line-height: 1.7;
            overflow-wrap: break-word;
        }
        .message.user .message-content {
            white-space: pre-wrap;
        }
        /* Markdown rendered content */
        .md-content p { margin: 0.6em 0; }
        .md-content h1, .md-content h2, .md-content h3,
        .md-content h4, .md-content h5, .md-content h6 {
            margin: 1em 0 0.5em;
            line-height: 1.3;
        }
        .md-content h1 { font-size: 1.4em; }
        .md-content h2 { font-size: 1.25em; }
        .md-content h3 { font-size: 1.1em; }
        .md-content ul, .md-content ol {
            margin: 0.5em 0;
            padding-left: 1.8em;
        }
        .md-content li { margin: 0.3em 0; }
        .md-content blockquote {
            margin: 0.6em 0;
            padding: 0.5em 1em;
            border-left: 3px solid #4a90e2;
            background: #f8f9fa;
            color: #555;
        }
        .md-content pre {
            background: #282c34;
            color: #abb2bf;
            padding: 12px 16px;
            border-radius: 6px;
            overflow-x: auto;
            font-size: 13px;
            line-height: 1.5;
            margin: 0.6em 0;
        }
        .md-content code {
            background: #f0f0f0;
            padding: 2px 5px;
            border-radius: 3px;
            font-size: 0.9em;
            font-family: 'SF Mono', Monaco, Consolas, monospace;
        }
        .md-content pre code {
            background: none;
            padding: 0;
            color: inherit;
        }
        .md-content table {
            border-collapse: collapse;
            width: 100%;
            margin: 0.6em 0;
        }
        .md-content th, .md-content td {
            border: 1px solid #e0e0e0;
            padding: 8px 12px;
            text-align: left;
        }
        .md-content th {
            background: #f5f5f5;
            font-weight: 600;
        }
        .md-content a {
            color: #4a90e2;
            text-decoration: none;
        }
        .md-content a:hover {
            text-decoration: underline;
        }
        .md-content hr {
            border: none;
            border-top: 1px solid #e0e0e0;
            margin: 1em 0;
        }
        .md-content img {
            max-width: 100%;
            border-radius: 4px;
        }
        .toolbar {
            position: fixed;
            top: 16px;
            right: 16px;
            background: white;
            border-radius: 6px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.15);
            padding: 6px;
            z-index: 100;
        }
        .toolbar button {
            background: none;
            border: none;
            cursor: pointer;
            padding: 6px 10px;
            border-radius: 4px;
            font-size: 13px;
            color: #555;
        }
        .toolbar button:hover {
            background: #f0f0f0;
            color: #333;
        }
        @media print {
            .toolbar { display: none; }
            body { background: white; }
            .message { box-shadow: none; break-inside: avoid; }
        }
    </style>
</head>
<body>
    <div class="toolbar">
        <button onclick="window.print()" title="打印">🖨️ 打印</button>
    </div>
    <div class="container">
        <div class="header">
            <h1>${escapeHtml(sessionMeta?.preview || '会话导出')}</h1>
            <div class="meta">
                <span>会话ID: ${session.session_id}</span>
                <span>消息数量: ${messages.length}</span>
                <span>创建时间: ${sessionMeta?.created_at || '未知'}</span>
            </div>
        </div>
        ${messagesHtml}
    </div>
    <script>
        // 渲染所有助手消息为markdown + math
        const texts = ${assistantTextsJson};
        document.querySelectorAll('.md-content').forEach(el => {
            const idx = parseInt(el.dataset.idx);
            const raw = texts[idx] || '';
            el.innerHTML = marked.parse(raw);
        });
        // 触发MathJax重新排版
        if (window.MathJax && window.MathJax.typesetPromise) {
            MathJax.typesetPromise();
        }
    <\/script>
</body>
</html>
        `;

        newWindow.document.write(html);
        newWindow.document.close();

    } catch (error) {
        console.error('导出会话失败:', error);
        alert('导出会话失败: ' + error.message);
    }
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

// 辅助函数：提取文本内容
function extractTextContent(content) {
    if (typeof content === 'string') {
        return content;
    }
    if (Array.isArray(content)) {
        return content
            .filter(block => block.type === 'text')
            .map(block => block.text || '')
            .join('\n');
    }
    return '';
}

// 辅助函数：HTML转义
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
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
function renderMessages(messages) {
    chatMessages.innerHTML = '';

    if (!messages || messages.length === 0) {
        chatMessages.innerHTML = '<div class="welcome-message"><h2>开始新对话</h2><p>输入消息开始使用</p></div>';
        return;
    }

    messages.forEach((msg, idx) => {
        const role = msg.role;
        if (role === 'system') return;

        // 处理SubAgent 引用
        if (role === '_subagent_ref') {
            renderSubagentRef(msg, idx);
            return;
        }

        const content = msg.content;
        const blocks = normalizeContent(content);

        // For user messages, group text + image blocks together
        if (role === 'user') {
            const textParts = [];
            const imageParts = [];
            blocks.forEach(block => {
                if (block.kind === 'text' && block.text) textParts.push(block.text);
                else if (block.kind === 'image') imageParts.push(block);
            });
            const combinedText = textParts.join('\n');
            const div = addMessage('user', combinedText);
            if (imageParts.length > 0) {
                const contentDiv = div.querySelector('.message-content');
                const imgContainer = document.createElement('div');
                imgContainer.className = 'user-images';
                imageParts.forEach(img => {
                    const imgEl = document.createElement('img');
                    imgEl.src = `data:${img.media_type};base64,${img.data}`;
                    imgEl.alt = '用户图片';
                    imgContainer.appendChild(imgEl);
                });
                contentDiv.insertBefore(imgContainer, contentDiv.firstChild);
            }
            return;  // Skip the generic block iteration below
        }

        blocks.forEach(block => {
            if (block.kind === 'text' && block.text) {
                addMessage(role, block.text);
            } else if (block.kind === 'image') {
                // Image blocks in non-user messages (shouldn't normally happen)
                // Render as an assistant message with the image
                const div = addMessage('assistant', '');
                const contentDiv = div.querySelector('.message-content');
                const imgEl = document.createElement('img');
                imgEl.src = `data:${block.media_type};base64,${block.data}`;
                imgEl.alt = '图片';
                contentDiv.appendChild(imgEl);
            } else if (block.kind === 'thinking' && block.text) {
                // Render thinking block
                const div = addMessage('assistant', '');
                div.classList.add('thinking');
                const contentDiv = div.querySelector('.message-content');
                const thinkTitle = document.createElement('div');
                thinkTitle.className = 'think-title';
                thinkTitle.textContent = '💭 思考过程';
                contentDiv.appendChild(thinkTitle);
                const thinkDiv = document.createElement('div');
                thinkDiv.className = 'think-content';
                thinkDiv.innerHTML = marked.parse(block.text);
                contentDiv.appendChild(thinkDiv);
            } else if (block.kind === 'tool_call') {
                const div = addMessage('assistant', '');
                div.classList.add('tool');
                const contentDiv = div.querySelector('.message-content');
                if (block.name === 'ask_user' && !block._answered) {
                    // Render interactive question card only if unanswered
                    renderAskUserQuestions(contentDiv, block.input, block.id);
                } else {
                    const pre = document.createElement('pre');
                    pre.textContent = JSON.stringify(block.input, null, 2);
                    const toolTitle = document.createElement('div');
                    toolTitle.className = 'tool-title';
                    toolTitle.textContent = `[调用工具: ${block.name}]`;
                    contentDiv.appendChild(toolTitle);
                    contentDiv.appendChild(pre);
                }
            } else if (block.kind === 'tool_result') {
                // Skip placeholder tool_result for ask_user
                if (block._wait_for_user) return;
                const text = typeof block.content === 'string' ? block.content : JSON.stringify(block.content, null, 2);
                const div = addMessage('assistant', '');
                div.classList.add('tool');
                if (block.is_error) {
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
        });
    });

    chatMessages.scrollTop = chatMessages.scrollHeight;

    // Render math formulas with MathJax
    if (window.MathJax && window.MathJax.typesetPromise) {
        MathJax.typesetPromise([chatMessages]).catch((err) => console.error('MathJax error:', err));
    }
}

// 渲染SubAgent 引用（可折叠卡片）
function renderSubagentRef(msg, idx) {
    const statusIcons = {
        'completed': '✅',
        'error': '❌',
        'failed': '❌',
        'timeout': '⏱️',
        'running': '🔄',
        'stopped': '⏹️'
    };
    const icon = statusIcons[msg.status] || '📋';

    const card = document.createElement('div');
    card.className = 'message assistant subagent-card';
    card.dataset.execId = msg.exec_id;

    const header = document.createElement('div');
    header.className = 'subagent-header';
    header.innerHTML = `
        <span class="sa-icon">${icon}</span>
        <span class="sa-title">SubAgent 执行</span>
        <span class="sa-task" title="${escapeHtml(msg.task_summary)}">${escapeHtml(msg.task_summary.substring(0, 60))}${msg.task_summary.length > 60 ? '...' : ''}</span>
        <span class="sa-meta">${msg.iterations || 0} 轮 · ${msg.message_count || 0} 条消息</span>
        <span class="sa-toggle">▶</span>
    `;

    const detail = document.createElement('div');
    detail.className = 'subagent-detail';
    detail.style.display = 'none';

    card.appendChild(header);
    card.appendChild(detail);
    chatMessages.appendChild(card);

    // 点击展开/折叠
    header.addEventListener('click', () => {
        if (detail.style.display === 'none') {
            detail.style.display = 'block';
            header.querySelector('.sa-toggle').textContent = '▼';
            // 运行中时每次都重新加载，完成后缓存
            const shouldReload = !detail.dataset.loaded || msg.status === 'running';
            if (shouldReload) {
                loadExecutionDetail(msg.exec_id, detail, header, msg);
            }
        } else {
            detail.style.display = 'none';
            header.querySelector('.sa-toggle').textContent = '▶';
            // 折叠时停止定时器并重置状态
            if (msg._refreshTimer) {
                clearInterval(msg._refreshTimer);
                msg._refreshTimer = null;
            }
            // 重置加载状态，再次展开时当作全新加载
            detail.dataset.loaded = 'false';
            detail.dataset.renderedCount = '0';
        }
    });
}

// 渲染任务清单（Todo List）
// TodoWrite UI 渲染逻辑
// 显示在聊天区域顶部，实时更新
function renderTodoList(todos) {
    if (!todos || !Array.isArray(todos) || todos.length === 0) {
        const existing = document.getElementById('todo-list');
        if (existing) existing.remove();
        return;
    }

    // 计算统计
    const total = todos.length;
    const completed = todos.filter(t => t.status === 'completed').length;
    const inProgress = todos.filter(t => t.status === 'in_progress').length;

    // 查找或创建 todo 容器
    let container = document.getElementById('todo-list');
    if (!container) {
        container = document.createElement('div');
        container.id = 'todo-list';
        container.className = 'todo-list-container';
        // 插入到聊天区域顶部
        chatMessages.insertBefore(container, chatMessages.firstChild);
    }

    // 构建 HTML
    const progress = total > 0 ? Math.round((completed / total) * 100) : 0;

    let html = `
        <div class="todo-header">
            <span class="todo-title">📋 任务清单</span>
            <span class="todo-progress">${completed}/${total} 完成</span>
        </div>
        <div class="todo-progress-bar">
            <div class="todo-progress-fill" style="width: ${progress}%"></div>
        </div>
        <ul class="todo-items">
    `;

    todos.forEach(todo => {
        const statusClass = `todo-${todo.status.replace('_', '-')}`;
        const statusIcon = todo.status === 'completed' ? '✓' :
                          todo.status === 'in_progress' ? '◉' : '○';
        const displayText = todo.status === 'in_progress' ?
                           (todo.activeForm || todo.content) : todo.content;
        html += `
            <li class="todo-item ${statusClass}">
                <span class="todo-status">${statusIcon}</span>
                <span class="todo-text">${escapeHtml(displayText)}</span>
            </li>
        `;
    });

    html += '</ul>';
    container.innerHTML = html;
}

// SubAgent 开始执行时立即渲染占位卡片（SSE 推送，无需等待完成）
function renderSubagentStart(execId, taskSummary) {
    const card = document.createElement('div');
    card.className = 'message assistant subagent-card';
    card.dataset.execId = execId;

    const header = document.createElement('div');
    header.className = 'subagent-header';
    header.innerHTML = `
        <span class="sa-icon">🔄</span>
        <span class="sa-title">SubAgent 执行中</span>
        <span class="sa-task" title="${escapeHtml(taskSummary)}">${escapeHtml(taskSummary.substring(0, 60))}${taskSummary.length > 60 ? '...' : ''}</span>
        <span class="sa-meta">0 轮 · 0 条消息</span>
        <span class="sa-toggle">▶</span>
    `;

    const detail = document.createElement('div');
    detail.className = 'subagent-detail';
    detail.style.display = 'none';

    card.appendChild(header);
    card.appendChild(detail);
    chatMessages.appendChild(card);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    // msg 对象用于 loadExecutionDetail 更新卡片状态
    const msg = { exec_id: execId, task_summary: taskSummary, status: 'running' };

    // 点击展开/折叠（运行中时每次都重新加载）
    header.addEventListener('click', () => {
        if (detail.style.display === 'none') {
            detail.style.display = 'block';
            header.querySelector('.sa-toggle').textContent = '▼';
            loadExecutionDetail(execId, detail, header, msg);
        } else {
            detail.style.display = 'none';
            header.querySelector('.sa-toggle').textContent = '▶';
            // 折叠时停止定时器并重置状态
            if (msg._refreshTimer) {
                clearInterval(msg._refreshTimer);
                msg._refreshTimer = null;
            }
            // 重置加载状态，再次展开时当作全新加载
            detail.dataset.loaded = 'false';
            detail.dataset.renderedCount = '0';
        }
    });
}

// 渲染 AskUser 问题卡片
function renderAskUserQuestions(container, input, toolUseId) {
    const questions = input.questions || [];
    if (!questions.length) return;

    const card = document.createElement('div');
    card.className = 'ask-user-card';
    if (toolUseId) card.dataset.toolUseId = toolUseId;

    const title = document.createElement('div');
    title.className = 'ask-title';
    title.textContent = '💬 Agent 需要你的输入';
    card.appendChild(title);

    // 每个问题一个区块
    const answers = {};  // question text → answer
    const questionBlocks = [];

    questions.forEach((q, idx) => {
        const qBlock = document.createElement('div');
        qBlock.className = 'ask-question';

        const header = document.createElement('span');
        header.className = 'ask-header';
        header.textContent = q.header || `问题 ${idx + 1}`;
        qBlock.appendChild(header);

        const qText = document.createElement('div');
        qText.className = 'ask-question-text';
        qText.textContent = q.question;
        qBlock.appendChild(qText);

        const optionsDiv = document.createElement('div');
        optionsDiv.className = 'ask-options';

        const multiSelect = q.multi_select || false;
        let selectedOptions = new Set();

        (q.options || []).forEach((opt, optIdx) => {
            const optEl = document.createElement('div');
            optEl.className = 'ask-option';
            optEl.dataset.optIdx = optIdx;

            const label = document.createElement('div');
            label.className = 'ask-option-label';
            label.textContent = opt.label;
            optEl.appendChild(label);

            const desc = document.createElement('div');
            desc.className = 'ask-option-desc';
            desc.textContent = opt.description;
            optEl.appendChild(desc);

            optEl.addEventListener('click', () => {
                if (multiSelect) {
                    if (selectedOptions.has(optIdx)) {
                        selectedOptions.delete(optIdx);
                        optEl.classList.remove('selected');
                    } else {
                        selectedOptions.add(optIdx);
                        optEl.classList.add('selected');
                    }
                } else {
                    // 单选：清除同问题的其他选项
                    optionsDiv.querySelectorAll('.ask-option.selected').forEach(el => el.classList.remove('selected'));
                    selectedOptions.clear();
                    selectedOptions.add(optIdx);
                    optEl.classList.add('selected');
                }
                // 更新答案
                const selectedLabels = [...selectedOptions].map(i => q.options[i].label);
                answers[q.question] = multiSelect ? selectedLabels.join(', ') : (selectedLabels[0] || '');
                updateSubmitBtn();
            });

            optionsDiv.appendChild(optEl);
        });

        // "Other" 自由输入
        const otherDiv = document.createElement('div');
        otherDiv.className = 'ask-option ask-other';
        otherDiv.innerHTML = '<div class="ask-option-label">其他</div>';
        const otherInput = document.createElement('input');
        otherInput.type = 'text';
        otherInput.className = 'ask-other-input';
        otherInput.placeholder = '输入自定义答案...';
        otherDiv.appendChild(otherInput);

        otherInput.addEventListener('focus', () => {
            // 清除选项选择
            optionsDiv.querySelectorAll('.ask-option.selected').forEach(el => el.classList.remove('selected'));
            selectedOptions.clear();
            otherDiv.classList.add('selected');
        });
        otherInput.addEventListener('input', () => {
            answers[q.question] = otherInput.value;
            updateSubmitBtn();
        });
        otherInput.addEventListener('blur', () => {
            if (!otherInput.value) {
                otherDiv.classList.remove('selected');
            }
        });

        qBlock.appendChild(optionsDiv);
        qBlock.appendChild(otherDiv);
        card.appendChild(qBlock);
        questionBlocks.push({ qBlock, otherInput });
    });

    // 提交按钮
    const submitBtn = document.createElement('button');
    submitBtn.className = 'btn btn-primary ask-submit';
    submitBtn.textContent = '提交';
    submitBtn.disabled = true;

    function updateSubmitBtn() {
        // 所有问题都有答案时启用
        const allAnswered = questions.every(q => {
            const a = answers[q.question];
            return a && a.trim() !== '';
        });
        submitBtn.disabled = !allAnswered;
    }

    function formatAnswers() {
        const parts = [];
        questions.forEach((q, idx) => {
            const answer = answers[q.question] || '';
            const otherInput = questionBlocks[idx]?.otherInput;
            const display = answer || (otherInput?.value || '');
            parts.push(`${q.question} ${display}`);
        });
        return parts.join('\n');
    }

    submitBtn.addEventListener('click', async () => {
        if (isSending) return;
        const answer = formatAnswers();
        if (!answer.trim()) return;

        submitBtn.textContent = '提交中...';
        submitBtn.disabled = true;

        try {
            // 调用 answer-ask-user API
            const response = await fetch(
                `/api/workspaces/${currentWorkspace.uuid}/sessions/${currentSession.session_id}/answer-ask-user`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ tool_use_id: toolUseId, answer })
                }
            );

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${await response.text()}`);
            }

            // 标记卡片为已提交
            submitBtn.textContent = '✓ 已提交';
            submitBtn.classList.add('ask-submitted');
            card.querySelectorAll('.ask-option').forEach(el => el.style.pointerEvents = 'none');
            card.querySelectorAll('input').forEach(el => el.disabled = true);

            // 处理 SSE 流
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            let assistantDiv = null;
            let assistantContent = '';
            let thinkDiv = null;
            let thinkContent = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';

                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    const dataStr = line.slice(6).trim();
                    if (!dataStr || dataStr === '[DONE]') continue;

                    try {
                        const event = JSON.parse(dataStr);

                        if (event.type === 'thinking') {
                            if (!thinkDiv) {
                                const div = addMessage('assistant', '');
                                div.classList.add('thinking');
                                const contentDiv = div.querySelector('.message-content');
                                const thinkTitle = document.createElement('div');
                                thinkTitle.className = 'think-title';
                                thinkTitle.textContent = '💭 思考中...';
                                contentDiv.appendChild(thinkTitle);
                                thinkDiv = document.createElement('div');
                                thinkDiv.className = 'think-content';
                                contentDiv.appendChild(thinkDiv);
                                thinkContent = '';
                            }
                            thinkContent += event.content;
                            thinkDiv.innerHTML = marked.parse(thinkContent);
                            chatMessages.scrollTop = chatMessages.scrollHeight;
                        } else if (event.type === 'text') {
                            if (thinkDiv) {
                                thinkDiv = null;
                                thinkContent = '';
                            }
                            assistantContent += event.content;
                            if (!assistantDiv) {
                                assistantDiv = addMessage('assistant', assistantContent);
                            } else {
                                assistantDiv.dataset.rawContent = assistantContent;
                                assistantDiv.querySelector('.message-content').innerHTML = marked.parse(assistantContent);
                                if (window.MathJax && window.MathJax.typesetPromise) {
                                    MathJax.typesetPromise([assistantDiv]).catch((err) => {});
                                }
                            }
                        } else if (event.type === 'tool_use') {
                            if (thinkDiv) {
                                thinkDiv = null;
                                thinkContent = '';
                            }
                            const div = addMessage('assistant', '');
                            div.classList.add('tool');
                            const contentDiv = div.querySelector('.message-content');
                            if (event.tool === 'ask_user') {
                                renderAskUserQuestions(contentDiv, event.input, event.tool_use_id);
                            } else {
                                const toolTitle = document.createElement('div');
                                toolTitle.className = 'tool-title';
                                toolTitle.textContent = `[调用工具: ${event.tool}]`;
                                contentDiv.appendChild(toolTitle);
                                const pre = document.createElement('pre');
                                pre.textContent = JSON.stringify(event.input, null, 2);
                                contentDiv.appendChild(pre);
                            }
                            assistantDiv = null;
                            assistantContent = '';
                        } else if (event.type === 'tool_result') {
                            if (event.tool === 'ask_user') {
                                const askCard = document.querySelector(`.ask-user-card[data-tool-use-id="${event.tool_use_id}"]`);
                                if (askCard) {
                                    const btn = askCard.querySelector('.ask-submit');
                                    if (btn && !btn.classList.contains('ask-submitted')) {
                                        btn.textContent = '✓ 已提交';
                                        btn.classList.add('ask-submitted');
                                        btn.disabled = true;
                                    }
                                    askCard.querySelectorAll('.ask-option').forEach(el => el.style.pointerEvents = 'none');
                                    askCard.querySelectorAll('input').forEach(el => el.disabled = true);
                                }
                                continue;
                            }
                            if (thinkDiv) {
                                thinkDiv = null;
                                thinkContent = '';
                            }
                            const div = addMessage('assistant', '');
                            div.classList.add('tool');
                            if (event.is_error) {
                                div.classList.add('tool-error');
                            } else {
                                div.classList.add('tool-result');
                            }
                            const contentDiv = div.querySelector('.message-content');
                            const resultTitle = document.createElement('div');
                            resultTitle.className = 'tool-title';
                            resultTitle.textContent = '[工具结果]';
                            contentDiv.appendChild(resultTitle);
                            const text = typeof event.content === 'string' ? event.content : JSON.stringify(event.content, null, 2);
                            const pre = document.createElement('pre');
                            pre.textContent = text;
                            contentDiv.appendChild(pre);
                            assistantDiv = null;
                            assistantContent = '';
                        } else if (event.type === 'subagent_start') {
                            renderSubagentStart(event.exec_id, event.task_summary);
                        } else if (event.type === 'todo_update') {
                            renderTodoList(event.todos);
                        } else if (event.type === 'retry_clear') {
                            if (assistantDiv) {
                                assistantDiv.remove();
                                assistantDiv = null;
                            }
                            assistantContent = '';
                            if (thinkDiv) {
                                thinkDiv = null;
                                thinkContent = '';
                            }
                        } else if (event.type === 'error') {
                            addMessage('assistant', `错误: ${escapeHtml(event.content)}`);
                        } else if (event.type === 'done') {
                            // Stream complete
                        }
                    } catch (e) {
                        console.error('Failed to parse SSE event:', e, dataStr);
                    }
                }
            }

            // Refresh session to get persisted state
            await loadSession(currentSession.session_id);

        } catch (error) {
            console.error('Failed to submit answer:', error);
            submitBtn.textContent = '提交失败';
            submitBtn.disabled = false;
            addMessage('assistant', '提交答案失败: ' + error.message);
        }
    });

    card.appendChild(submitBtn);
    container.appendChild(card);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// 加载执行详情（headerEl 和 msg 用于实时更新卡片状态）
async function loadExecutionDetail(execId, container, headerEl, msg) {
    // 如果正在刷新，先清除旧的定时器
    if (msg._refreshTimer) {
        clearInterval(msg._refreshTimer);
        msg._refreshTimer = null;
    }

    container.innerHTML = '<div class="sa-loading">加载中...</div>';

    // 实际加载函数
    const doLoad = async () => {
        try {
            const workspaceUuid = currentWorkspace?.uuid;
            const sessionId = currentSession.session_id;
            const url = `/api/workspaces/${workspaceUuid}/sessions/${sessionId}/executions/${execId}`;
            const resp = await fetch(url);

            if (!resp.ok) {
                container.innerHTML = '<div class="sa-error">加载失败</div>';
                return false;
            }

            const data = await resp.json();
            const isFirstLoad = container.dataset.loaded !== 'true';
            container.dataset.loaded = 'true';

            // 更新卡片 header 中的状态信息
            if (headerEl && msg) {
                // 更新 status（从运行中变为已完成等）
                if (data.metadata && data.metadata.status) {
                    msg.status = data.metadata.status;
                    const statusIcons = {
                        'completed': '✅',
                        'error': '❌',
                        'failed': '❌',
                        'timeout': '⏱️',
                        'running': '🔄',
                        'stopped': '⏹️'
                    };
                    headerEl.querySelector('.sa-icon').textContent = statusIcons[msg.status] || '📋';
                }
                // 更新 iterations 和 message_count
                if (data.metadata) {
                    const iters = data.metadata.iterations || 0;
                    const msgs = data.messages ? data.messages.length : 0;
                    const currentTool = data.metadata.current_tool || '';
                    const toolSuffix = currentTool ? ` · 正在: ${currentTool}` : '';
                    headerEl.querySelector('.sa-meta').textContent = `${iters} 轮 · ${msgs} 条消息${toolSuffix}`;
                }
            }

            // 首次加载时清空容器
            if (isFirstLoad) {
                container.innerHTML = '';

                // 渲染摘要（仅首次）
                if (data.summary) {
                    const summaryDiv = document.createElement('div');
                    summaryDiv.className = 'sa-summary';
                    summaryDiv.innerHTML = marked.parse(data.summary);
                    container.appendChild(summaryDiv);
                }
            }

            // 渲染消息（增量追加）
            const renderedCount = parseInt(container.dataset.renderedCount || '0');
            const messages = data.messages || [];

            if (messages.length > renderedCount) {
                // 获取或创建消息容器
                let msgsDiv = container.querySelector('.sa-messages');
                if (!msgsDiv) {
                    msgsDiv = document.createElement('div');
                    msgsDiv.className = 'sa-messages';
                    container.appendChild(msgsDiv);
                }

                // 只渲染新消息
                const newMessages = messages.slice(renderedCount);
                newMessages.forEach(message => {
                    const blocks = normalizeContent(message.content);
                    blocks.forEach(block => {
                        if (block.kind === 'text' && block.text) {
                            const div = document.createElement('div');
                            div.className = `sa-msg ${message.role}`;
                            div.innerHTML = marked.parse(block.text);
                            msgsDiv.appendChild(div);
                        } else if (block.kind === 'tool_call') {
                            const div = document.createElement('div');
                            div.className = 'sa-msg tool-use';
                            div.innerHTML = `<strong>[工具调用: ${block.name}]</strong><pre>${escapeHtml(JSON.stringify(block.input, null, 2))}</pre>`;
                            msgsDiv.appendChild(div);
                        } else if (block.kind === 'tool_result') {
                            const text = typeof block.content === 'string' ? block.content : JSON.stringify(block.content, null, 2);
                            const div = document.createElement('div');
                            div.className = 'sa-msg tool-result';
                            div.innerHTML = `<strong>[工具结果]</strong><pre>${escapeHtml(text.substring(0, 500))}${text.length > 500 ? '...' : ''}</pre>`;
                            msgsDiv.appendChild(div);
                        }
                    });
                });

                // 更新已渲染计数
                container.dataset.renderedCount = messages.length;

                // 自动滚动：只在用户已经在底部附近时才滚动
                const chatMessages = document.getElementById('chat-messages');
                if (chatMessages) {
                    const isNearBottom = chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight < 100;
                    if (isNearBottom) {
                        chatMessages.scrollTop = chatMessages.scrollHeight;
                    }
                }
            }

            // 返回当前状态
            return msg.status === 'running';
        } catch (err) {
            console.error('Failed to load execution detail:', err);
            if (container.dataset.loaded !== 'true') {
                container.innerHTML = '<div class="sa-error">加载失败: ' + escapeHtml(err.message) + '</div>';
            }
            return false;
        }
    };

    // 首次加载
    const isRunning = await doLoad();

    // 如果还在运行，设置定时刷新（每 3 秒）
    if (isRunning) {
        msg._refreshTimer = setInterval(async () => {
            const stillRunning = await doLoad();
            if (!stillRunning) {
                // 已完成，停止刷新
                clearInterval(msg._refreshTimer);
                msg._refreshTimer = null;
            }
        }, 3000);
    }
}

// Normalize message content into blocks
function normalizeContent(content) {
    const blocks = [];
    if (typeof content === 'string') {
        blocks.push({ kind: 'text', text: content });
    } else if (Array.isArray(content)) {
        content.forEach(block => {
            if (block.type === 'text') {
                blocks.push({ kind: 'text', text: block.text || '' });
            } else if (block.type === 'image') {
                const source = block.source || {};
                blocks.push({ kind: 'image', data: source.data || '', media_type: source.media_type || 'image/png' });
            } else if (block.type === 'thinking' || block.type === 'reasoning') {
                blocks.push({ kind: 'thinking', text: block.thinking || block.text || '' });
            } else if (block.type === 'tool_use' || block.type === 'tool_call') {
                // Handle both old format (input dict) and new format (arguments JSON string)
                let input = block.input;
                if (!input && block.arguments) {
                    try {
                        input = JSON.parse(block.arguments);
                    } catch (e) {
                        input = { _raw: block.arguments };
                    }
                }
                blocks.push({ kind: 'tool_call', name: block.name, input: input, id: block.id, _answered: block._answered || false });
            } else if (block.type === 'tool_result') {
                blocks.push({ kind: 'tool_result', content: block.content, is_error: block.is_error || false, _wait_for_user: block._wait_for_user || false, tool_use_id: block.tool_use_id || block.tool_call_id });
            }
        });
    }
    return blocks;
}

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

// Stop agent
async function stopAgent() {
    if (!currentWorkspace || !currentSession) return;

    // Disable button immediately after clicking
    sendBtn.disabled = true;
    sendBtn.textContent = '停止中...';

    try {
        const response = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${currentSession.session_id}/stop`,
            { method: 'POST' }
        );

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const result = await response.json();
        if (result.success) {
            console.log('Agent stop signal sent');
        } else {
            console.warn('Stop failed:', result.message);
        }
    } catch (error) {
        console.error('Failed to stop agent:', error);
    }

    // 立即恢复按钮状态，不再等待 SSE 流关闭（LLM 重试可能持续数分钟）
    // sendMessage() 的 finally 块在 SSE 流最终关闭时会再次重置，无冲突
    isSending = false;
    sendBtn.disabled = false;
    sendBtn.textContent = '发送';
    sendBtn.classList.remove('btn-danger');
    sendBtn.classList.add('btn-primary');
    chatInput.disabled = false;

    // 禁用所有 pending 的 ask_user 问题卡片
    document.querySelectorAll('.ask-user-card').forEach(card => {
        const submitBtn = card.querySelector('.ask-submit');
        if (submitBtn && !submitBtn.classList.contains('ask-submitted')) {
            submitBtn.textContent = '已取消';
            submitBtn.disabled = true;
        }
        card.querySelectorAll('.ask-option').forEach(el => el.style.pointerEvents = 'none');
        card.querySelectorAll('input').forEach(el => el.disabled = true);
    });
}

// Send message
async function sendMessage() {
    const message = chatInput.value.trim();
    const hasImages = pendingImages.length > 0;
    if (!message && !hasImages) return;

    if (!currentWorkspace || !currentSession) {
        alert('请先选择工作区并创建会话');
        return;
    }

    // Block sending if agent is running
    if (isSending) {
        return;
    }

    console.log('Sending message to session:', currentSession.session_id);

    // Capture images before clearing
    const imagesToSend = hasImages ? pendingImages.map(img => ({
        data: img.data,
        media_type: img.media_type,
    })) : null;

    // Clear input and change button to stop
    chatInput.value = '';
    pendingImages = [];
    renderImagePreviews();
    isSending = true;
    sendBtn.textContent = '停止';
    sendBtn.classList.remove('btn-primary');
    sendBtn.classList.add('btn-danger');

    // Add user message to UI (with images if any)
    const userDiv = addMessage('user', message);
    if (imagesToSend) {
        const contentDiv = userDiv.querySelector('.message-content');
        const imgContainer = document.createElement('div');
        imgContainer.className = 'user-images';
        imagesToSend.forEach(img => {
            const imgEl = document.createElement('img');
            imgEl.src = `data:${img.media_type};base64,${img.data}`;
            imgEl.alt = '用户图片';
            imgContainer.appendChild(imgEl);
        });
        contentDiv.insertBefore(imgContainer, contentDiv.firstChild);
    }

    // Create placeholder for assistant response
    let assistantDiv = null;
    let assistantContent = '';
    let thinkDiv = null;
    let thinkContent = '';

    // Close the current think block (shared by text, tool_use, tool_result handlers)
    function finalizeThinkBlock() {
        if (thinkDiv) {
            const thinkTitle = thinkDiv.parentElement.querySelector('.think-title');
            if (thinkTitle) {
                thinkTitle.textContent = '💭 思考完成';
            }
            thinkDiv = null;
            thinkContent = '';
        }
    }

    try {
        const requestBody = { content: message };
        if (imagesToSend) {
            requestBody.images = imagesToSend;
        }
        const response = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${currentSession.session_id}/messages`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(requestBody)
            }
        );

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${await response.text()}`);
        }

        // Handle streaming response
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });

            // Process complete lines
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';  // Keep incomplete last line

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;

                const dataStr = line.slice(6).trim();
                if (!dataStr || dataStr === '[DONE]') continue;

                try {
                    const event = JSON.parse(dataStr);

                    if (event.type === 'thinking') {
                        // 创建或更新 think 块
                        if (!thinkDiv) {
                            const div = addMessage('assistant', '');
                            div.classList.add('thinking');
                            const contentDiv = div.querySelector('.message-content');
                            const thinkTitle = document.createElement('div');
                            thinkTitle.className = 'think-title';
                            thinkTitle.textContent = '💭 思考中...';
                            contentDiv.appendChild(thinkTitle);
                            thinkDiv = document.createElement('div');
                            thinkDiv.className = 'think-content';
                            contentDiv.appendChild(thinkDiv);
                            thinkContent = '';
                        }
                        thinkContent += event.content;
                        thinkDiv.innerHTML = marked.parse(thinkContent);
                        chatMessages.scrollTop = chatMessages.scrollHeight;
                    } else if (event.type === 'text') {
                        // 如果之前有 think 块，标记完成
                        finalizeThinkBlock();
                        assistantContent += event.content;
                        if (!assistantDiv) {
                            assistantDiv = addMessage('assistant', assistantContent);
                        } else {
                            assistantDiv.dataset.rawContent = assistantContent;
                            assistantDiv.querySelector('.message-content').innerHTML = marked.parse(assistantContent);
                            // Render math formulas with MathJax
                            if (window.MathJax && window.MathJax.typesetPromise) {
                                MathJax.typesetPromise([assistantDiv]).catch((err) => {});
                            }
                        }
                    } else if (event.type === 'tool_use') {
                        // 如果之前有 think 块，标记完成
                        finalizeThinkBlock();
                        // 创建新的消息气泡
                        const div = addMessage('assistant', '');
                        div.classList.add('tool');
                        const contentDiv = div.querySelector('.message-content');

                        if (event.tool === 'ask_user') {
                            // 渲染交互式问题卡片
                            renderAskUserQuestions(contentDiv, event.input, event.tool_use_id);
                        } else {
                            const toolTitle = document.createElement('div');
                            toolTitle.className = 'tool-title';
                            toolTitle.textContent = `[调用工具: ${event.tool}]`;
                            contentDiv.appendChild(toolTitle);
                            const pre = document.createElement('pre');
                            pre.textContent = JSON.stringify(event.input, null, 2);
                            contentDiv.appendChild(pre);
                        }

                        // 重置 assistantDiv 用于后续文本
                        assistantDiv = null;
                        assistantContent = '';
                    } else if (event.type === 'tool_result') {
                        // ask_user 的 tool_result 事件：禁用问题卡片
                        if (event.tool === 'ask_user') {
                            const card = document.querySelector(`.ask-user-card[data-tool-use-id="${event.tool_use_id}"]`);
                            if (card) {
                                const submitBtn = card.querySelector('.ask-submit');
                                if (submitBtn && !submitBtn.classList.contains('ask-submitted')) {
                                    submitBtn.textContent = '✓ 已提交';
                                    submitBtn.classList.add('ask-submitted');
                                    submitBtn.disabled = true;
                                }
                                card.querySelectorAll('.ask-option').forEach(el => el.style.pointerEvents = 'none');
                                card.querySelectorAll('input').forEach(el => el.disabled = true);
                            }
                            continue;
                        }
                        // 如果之前有 think 块，标记完成
                        finalizeThinkBlock();
                        // 创建新的消息气泡
                        const div = addMessage('assistant', '');
                        div.classList.add('tool');
                        if (event.is_error) {
                            div.classList.add('tool-error');
                        } else {
                            div.classList.add('tool-result');
                        }
                        const contentDiv = div.querySelector('.message-content');
                        const resultTitle = document.createElement('div');
                        resultTitle.className = 'tool-title';
                        resultTitle.textContent = '[工具结果]';
                        contentDiv.appendChild(resultTitle);
                        const text = typeof event.content === 'string' ? event.content : JSON.stringify(event.content, null, 2);
                        const pre = document.createElement('pre');
                        pre.textContent = text;
                        contentDiv.appendChild(pre);
                        // 重置 assistantDiv 用于后续文本
                        assistantDiv = null;
                        assistantContent = '';
                    } else if (event.type === 'subagent_start') {
                        // SubAgent 开始执行，立即渲染占位卡片
                        renderSubagentStart(event.exec_id, event.task_summary);
                    } else if (event.type === 'todo_update') {
                        // Todo 列表更新，渲染任务清单
                        renderTodoList(event.todos);
                    } else if (event.type === 'retry_clear') {
                        // 413 重试：清除已流式输出的文本，防止用户看到重复内容
                        if (assistantDiv) {
                            assistantDiv.remove();
                            assistantDiv = null;
                        }
                        assistantContent = '';
                        finalizeThinkBlock();
                        thinkDiv = null;
                        thinkContent = '';
                    } else if (event.type === 'error') {
                        addMessage('assistant', `错误: ${escapeHtml(event.content)}`);
                    } else if (event.type === 'done') {
                        // Stream complete
                    }
                } catch (e) {
                    console.error('Failed to parse SSE event:', e, dataStr);
                }
            }
        }

        // Refresh session to get persisted state
        await loadSession(currentSession.session_id);

    } catch (error) {
        console.error('Failed to send message:', error);
        addMessage('assistant', '发送消息失败: ' + error.message);
    } finally {
        isSending = false;
        sendBtn.disabled = false;
        sendBtn.textContent = '发送';
        sendBtn.classList.remove('btn-danger');
        sendBtn.classList.add('btn-primary');
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }
}

// Add message to UI
function addMessage(role, content) {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${role}`;

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    if (content) {
        contentDiv.innerHTML = marked.parse(content);

        // 检查是否包含图片，如果包含则添加特殊样式
        if (contentDiv.querySelector('img')) {
            messageDiv.classList.add('has-image');
        }
    }

    messageDiv.appendChild(contentDiv);

    // 助手消息添加复制和导出按钮（仅非空文本消息）
    if (role === 'assistant' && content) {
        messageDiv.dataset.rawContent = content;

        // 创建按钮容器
        const actionsDiv = document.createElement('div');
        actionsDiv.className = 'message-actions';

        // 复制按钮
        const copyBtn = document.createElement('button');
        copyBtn.title = '复制';
        copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
        copyBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const raw = messageDiv.dataset.rawContent || messageDiv.querySelector('.message-content').textContent;
            navigator.clipboard.writeText(raw).then(() => {
                copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>';
                copyBtn.classList.add('copied');
                setTimeout(() => {
                    copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
                    copyBtn.classList.remove('copied');
                }, 2000);
            });
        });

        // 导出按钮
        const exportBtn = document.createElement('button');
        exportBtn.title = '导出';
        exportBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>';
        exportBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const raw = messageDiv.dataset.rawContent || messageDiv.querySelector('.message-content').textContent;
            exportToNewTab(raw);
        });

        actionsDiv.appendChild(copyBtn);
        actionsDiv.appendChild(exportBtn);
        messageDiv.appendChild(actionsDiv);
    }

    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    return messageDiv;
}

// Export message to new browser tab as rendered HTML
function exportToNewTab(markdownContent) {
    const newWindow = window.open('', '_blank');
    if (!newWindow) {
        alert('无法打开新窗口，请检查浏览器是否阻止了弹出窗口');
        return;
    }

    const html = `
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>消息导出</title>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"><\/script>
    <script>
        window.MathJax = {
            tex: {
                inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
                displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
                processEscapes: true,
                processEnvironments: true
            },
            options: {
                skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
            }
        };
    <\/script>
    <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"><\/script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: #f5f5f5;
            color: #333;
            line-height: 1.6;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            padding: 30px 20px;
        }
        .message {
            background: white;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            overflow: hidden;
        }
        .message-content {
            padding: 20px 24px;
            font-size: 14px;
            line-height: 1.7;
            overflow-wrap: break-word;
        }
        /* Markdown rendered content */
        .md-content p { margin: 0.6em 0; }
        .md-content h1, .md-content h2, .md-content h3,
        .md-content h4, .md-content h5, .md-content h6 {
            margin: 1em 0 0.5em;
            line-height: 1.3;
        }
        .md-content h1 { font-size: 1.4em; }
        .md-content h2 { font-size: 1.25em; }
        .md-content h3 { font-size: 1.1em; }
        .md-content ul, .md-content ol {
            margin: 0.5em 0;
            padding-left: 1.8em;
        }
        .md-content li { margin: 0.3em 0; }
        .md-content blockquote {
            margin: 0.6em 0;
            padding: 0.5em 1em;
            border-left: 3px solid #4a90e2;
            background: #f8f9fa;
            color: #555;
        }
        .md-content pre {
            background: #282c34;
            color: #abb2bf;
            padding: 12px 16px;
            border-radius: 6px;
            overflow-x: auto;
            font-size: 13px;
            line-height: 1.5;
            margin: 0.6em 0;
        }
        .md-content code {
            background: #f0f0f0;
            padding: 2px 5px;
            border-radius: 3px;
            font-size: 0.9em;
            font-family: 'SF Mono', Monaco, Consolas, monospace;
        }
        .md-content pre code {
            background: none;
            padding: 0;
            color: inherit;
        }
        .md-content table {
            border-collapse: collapse;
            width: 100%;
            margin: 0.6em 0;
        }
        .md-content th, .md-content td {
            border: 1px solid #e0e0e0;
            padding: 8px 12px;
            text-align: left;
        }
        .md-content th {
            background: #f5f5f5;
            font-weight: 600;
        }
        .md-content a {
            color: #4a90e2;
            text-decoration: none;
        }
        .md-content a:hover {
            text-decoration: underline;
        }
        .md-content hr {
            border: none;
            border-top: 1px solid #e0e0e0;
            margin: 1em 0;
        }
        .md-content img {
            max-width: 100%;
            border-radius: 4px;
        }
        .toolbar {
            position: fixed;
            top: 16px;
            right: 16px;
            background: white;
            border-radius: 6px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.15);
            padding: 6px;
            z-index: 100;
        }
        .toolbar button {
            background: none;
            border: none;
            cursor: pointer;
            padding: 6px 10px;
            border-radius: 4px;
            font-size: 13px;
            color: #555;
        }
        .toolbar button:hover {
            background: #f0f0f0;
            color: #333;
        }
        @media print {
            .toolbar { display: none; }
            body { background: white; }
            .message { box-shadow: none; }
        }
    </style>
</head>
<body>
    <div class="toolbar">
        <button onclick="window.print()" title="打印">🖨️ 打印</button>
    </div>
    <div class="container">
        <div class="message">
            <div class="message-content md-content"></div>
        </div>
    </div>
    <script>
        const markdown = ${JSON.stringify(markdownContent)};
        document.querySelector('.md-content').innerHTML = marked.parse(markdown);
        if (window.MathJax && window.MathJax.typesetPromise) {
            MathJax.typesetPromise();
        }
    <\/script>
</body>
</html>
    `;

    newWindow.document.write(html);
    newWindow.document.close();
}

// ---------- Settings ----------

async function openSettings() {
    document.getElementById('settings-modal').style.display = 'flex';
    document.getElementById('settings-status').textContent = '加载中...';

    // Reset to RootAgent model tab
    document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('.settings-tab[data-tab="root"]').classList.add('active');
    document.getElementById('tab-root').classList.add('active');

    try {
        const response = await fetch('/api/config');
        const data = await response.json();
        const cfg = data.config || {};
        const modelCfg = cfg.model || {};
        const llmModelCfg = cfg.llm_model || {};

        // ── RootAgent model settings ──
        document.getElementById('setting-api-key').value = '';
        document.getElementById('setting-base-url').value = modelCfg.base_url || '';
        document.getElementById('setting-model').value = modelCfg.name || '';
        document.getElementById('setting-interface-type').value = modelCfg.interface_type || 'anthropic';
        document.getElementById('setting-max-tokens').value = modelCfg.max_tokens || 16384;
        document.getElementById('setting-max-context-tokens').value = modelCfg.max_context_tokens || 256000;
        document.getElementById('setting-multimodal').checked = modelCfg.multimodal !== false;

        // Temperature slider
        const tempVal = modelCfg.temperature != null ? modelCfg.temperature : 0.2;
        document.getElementById('setting-temperature').value = tempVal;
        document.getElementById('setting-temperature-value').textContent = tempVal;

        // Show masked key
        const maskedHint = document.getElementById('setting-api-key-masked');
        if (modelCfg.api_key_masked) {
            maskedHint.textContent = `当前: ${modelCfg.api_key_masked} (留空保持不变)`;
        } else {
            maskedHint.textContent = '未配置 API Key';
        }

        // ── LLM model settings ──
        document.getElementById('llm-api-key').value = '';
        document.getElementById('llm-base-url').value = llmModelCfg.base_url || '';
        document.getElementById('llm-model').value = llmModelCfg.name || '';
        document.getElementById('llm-interface-type').value = llmModelCfg.interface_type || '';
        document.getElementById('llm-max-tokens').value = llmModelCfg.max_tokens || 16384;
        document.getElementById('llm-max-context-tokens').value = llmModelCfg.max_context_tokens || 256000;
        document.getElementById('llm-multimodal').checked = llmModelCfg.multimodal !== false;

        // LLM Temperature slider
        const llmTempVal = llmModelCfg.temperature != null ? llmModelCfg.temperature : 0.2;
        document.getElementById('llm-temperature').value = llmTempVal;
        document.getElementById('llm-temperature-value').textContent = llmTempVal;

        // Show LLM masked key
        const llmMaskedHint = document.getElementById('llm-api-key-masked');
        if (llmModelCfg.api_key_masked) {
            llmMaskedHint.textContent = `当前: ${llmModelCfg.api_key_masked} (留空保持不变)`;
        } else if (modelCfg.api_key_masked) {
            llmMaskedHint.textContent = '未单独配置（将使用 RootAgent 模型的 Key）';
        } else {
            llmMaskedHint.textContent = '未配置';
        }

        // ── System parameters ──
        const systemCfg = cfg.system || {};
        document.getElementById('system-pip-mirror').value = systemCfg.pip_mirror || '';
        document.getElementById('system-browser-path').value = systemCfg.browser_path || '';
        document.getElementById('system-search-engine').value = systemCfg.search_engine || 'bing';
        document.getElementById('system-allowed-ips').value = (systemCfg.allowed_ips || []).join(', ');

        document.getElementById('settings-status').textContent = `配置文件: ${data.config_path}`;
    } catch (error) {
        document.getElementById('settings-status').textContent = '加载失败: ' + error.message;
    }
}

async function saveSettings() {
    const statusEl = document.getElementById('settings-status');
    statusEl.textContent = '保存中...';

    const payload = {
        model: {},
        llm_model: {}
    };

    // ── RootAgent model settings ──
    const apiKey = document.getElementById('setting-api-key').value.trim();
    if (apiKey) payload.model.api_key = apiKey;

    const baseUrl = document.getElementById('setting-base-url').value.trim();
    if (baseUrl) payload.model.base_url = baseUrl;

    const model = document.getElementById('setting-model').value.trim();
    if (model) payload.model.name = model;

    const interfaceType = document.getElementById('setting-interface-type').value;
    payload.model.interface_type = interfaceType;

    const maxTokens = document.getElementById('setting-max-tokens').value;
    if (maxTokens) payload.model.max_tokens = parseInt(maxTokens);

    const maxContextTokens = document.getElementById('setting-max-context-tokens').value;
    if (maxContextTokens) payload.model.max_context_tokens = parseInt(maxContextTokens);

    payload.model.multimodal = document.getElementById('setting-multimodal').checked;
    payload.model.temperature = parseFloat(document.getElementById('setting-temperature').value);

    // ── LLM model settings ──
    const llmApiKey = document.getElementById('llm-api-key').value.trim();
    if (llmApiKey) payload.llm_model.api_key = llmApiKey;

    const llmBaseUrl = document.getElementById('llm-base-url').value.trim();
    if (llmBaseUrl) payload.llm_model.base_url = llmBaseUrl;

    const llmModel = document.getElementById('llm-model').value.trim();
    if (llmModel) payload.llm_model.name = llmModel;

    const llmInterfaceType = document.getElementById('llm-interface-type').value;
    if (llmInterfaceType) {
        payload.llm_model.interface_type = llmInterfaceType;
    }

    const llmMaxTokens = document.getElementById('llm-max-tokens').value;
    if (llmMaxTokens) payload.llm_model.max_tokens = parseInt(llmMaxTokens);

    const llmMaxContextTokens = document.getElementById('llm-max-context-tokens').value;
    if (llmMaxContextTokens) payload.llm_model.max_context_tokens = parseInt(llmMaxContextTokens);

    payload.llm_model.multimodal = document.getElementById('llm-multimodal').checked;
    payload.llm_model.temperature = parseFloat(document.getElementById('llm-temperature').value);

    // If LLM model has no name, clear it (unconfigured)
    if (!payload.llm_model.name) {
        payload.llm_model = { name: '', api_key: '' };  // Signal to backend to remove
    }

    // ── System parameters ──
    const pipMirror = document.getElementById('system-pip-mirror').value.trim();
    const browserPath = document.getElementById('system-browser-path').value.trim();
    const searchEngine = document.getElementById('system-search-engine').value;
    const allowedIpsStr = document.getElementById('system-allowed-ips').value.trim();
    const allowedIps = allowedIpsStr ? allowedIpsStr.split(',').map(ip => ip.trim()).filter(Boolean) : [];
    payload.system = {
        pip_mirror: pipMirror,
        browser_path: browserPath,
        search_engine: searchEngine,
        allowed_ips: allowedIps
    };

    try {
        const response = await fetch('/api/config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Failed to save');
        }

        statusEl.textContent = '✓ 保存成功';
        setTimeout(() => {
            document.getElementById('settings-modal').style.display = 'none';
        }, 800);
    } catch (error) {
        statusEl.textContent = '✗ 保存失败: ' + error.message;
    }
}

async function testSettings(which = 'model') {
    const statusEl = document.getElementById('settings-status');
    statusEl.textContent = '测试连接中...';

    const isLlmTab = which === 'llm';

    let payload = {};
    if (isLlmTab) {
        // Test LLM model config
        const llmModel = document.getElementById('llm-model').value.trim();
        const llmInterfaceType = document.getElementById('llm-interface-type').value;
        const llmBaseUrl = document.getElementById('llm-base-url').value.trim();
        const llmApiKey = document.getElementById('llm-api-key').value.trim();

        if (!llmModel) {
            statusEl.textContent = '✗ 请先填写 LLM 模型名称';
            return;
        }

        payload = {
            config: {
                name: llmModel,
                interface_type: llmInterfaceType || 'anthropic',
            }
        };
        if (llmBaseUrl) payload.config.base_url = llmBaseUrl;
        if (llmApiKey) payload.config.api_key = llmApiKey;
    } else {
        // Test Agent model config
        const model = document.getElementById('setting-model').value.trim();
        const interfaceType = document.getElementById('setting-interface-type').value;
        const baseUrl = document.getElementById('setting-base-url').value.trim();
        const apiKey = document.getElementById('setting-api-key').value.trim();

        if (!model) {
            statusEl.textContent = '✗ 请先填写模型名称';
            return;
        }

        payload = {
            config: {
                name: model,
                interface_type: interfaceType || 'anthropic',
            }
        };
        if (baseUrl) payload.config.base_url = baseUrl;
        if (apiKey) payload.config.api_key = apiKey;
    }

    try {
        const response = await fetch('/api/config/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();

        if (data.success) {
            const interfaceInfo = data.interface_type === 'anthropic' ? ' (Anthropic API)' :
                                 data.interface_type === 'openai' ? ' (OpenAI API)' : '';
            const modelLabel = isLlmTab ? 'LLM 模型' : 'Agent 模型';
            statusEl.textContent = `✓ ${modelLabel}连接成功${interfaceInfo}`;
        } else {
            statusEl.textContent = `✗ 连接失败: ${data.message}`;
        }
    } catch (error) {
        statusEl.textContent = '✗ 测试失败: ' + error.message;
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

// Directory browser state
let directoryBrowserCallback = null;

async function browseDirectory(event) {
    // 找到触发事件的输入框
    const targetInput = event.target.parentElement.querySelector('input[type="text"]');
    if (!targetInput) return;

    // 打开目录浏览器弹窗
    const currentPath = targetInput.value || '';
    openDirectoryBrowser(currentPath, (selectedPath) => {
        targetInput.value = selectedPath;
    });
}

async function openDirectoryBrowser(initialPath, callback) {
    directoryBrowserCallback = callback;

    // 创建或获取弹窗
    let modal = document.getElementById('directory-browser-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'directory-browser-modal';
        modal.className = 'modal';
        modal.innerHTML = `
            <div class="modal-backdrop" onclick="closeDirectoryBrowser()"></div>
            <div class="modal-content" style="max-width: 500px;">
                <div class="modal-header">
                    <h2>选择目录</h2>
                    <button class="modal-close" onclick="closeDirectoryBrowser()">&times;</button>
                </div>
                <div class="modal-body">
                    <div class="dir-browser-path" id="dir-browser-current-path"></div>
                    <div class="dir-browser-list" id="dir-browser-list"></div>
                </div>
                <div class="modal-footer">
                    <button class="btn" onclick="closeDirectoryBrowser()">取消</button>
                    <button class="btn btn-primary" onclick="confirmDirectorySelection()">选择此目录</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }

    modal.style.display = 'flex';
    await loadDirectory(initialPath || '');
}

async function loadDirectory(path) {
    const listEl = document.getElementById('dir-browser-list');
    const pathEl = document.getElementById('dir-browser-current-path');

    listEl.innerHTML = '<div class="dir-browser-loading">加载中...</div>';
    pathEl.textContent = path || '选择位置';

    try {
        const response = await fetch(`/api/browse?path=${encodeURIComponent(path)}`);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();

        // 更新当前路径显示
        pathEl.textContent = data.path || '选择位置';
        pathEl.dataset.currentPath = data.path || '';

        // 渲染目录列表
        listEl.innerHTML = '';

        // 显示 fallback 提示（原路径不存在，已跳转到上级目录）
        if (data.fallback) {
            const hintItem = document.createElement('div');
            hintItem.className = 'dir-browser-item dir-browser-hint';
            hintItem.style.color = '#e67e22';
            hintItem.style.fontStyle = 'italic';
            hintItem.textContent = data.fallback;
            listEl.appendChild(hintItem);
        }

        // 添加父目录按钮（如果有）
        if (data.parent !== null && data.parent !== undefined) {
            const parentItem = document.createElement('div');
            parentItem.className = 'dir-browser-item dir-browser-parent';
            parentItem.innerHTML = `<span>⬆️ ..</span>`;
            parentItem.onclick = () => loadDirectory(data.parent);
            listEl.appendChild(parentItem);
        }

        // 添加目录列表
        if (data.directories.length === 0) {
            const emptyItem = document.createElement('div');
            emptyItem.className = 'dir-browser-item dir-browser-empty';
            emptyItem.textContent = '（空目录）';
            listEl.appendChild(emptyItem);
        } else {
            for (const dir of data.directories) {
                const item = document.createElement('div');
                item.className = 'dir-browser-item';
                // Windows 驱动器根目录显示为驱动器名，其他目录显示文件夹名
                const displayName = dir.name.match(/^[A-Z]:\\$/) ? dir.name : `📁 ${dir.name}`;
                item.innerHTML = `<span>${displayName}</span>`;
                item.onclick = () => loadDirectory(dir.path);
                listEl.appendChild(item);
            }
        }
    } catch (err) {
        listEl.innerHTML = `<div class="dir-browser-error">加载失败: ${err.message}</div>`;
    }
}

function confirmDirectorySelection() {
    const pathEl = document.getElementById('dir-browser-current-path');
    const selectedPath = pathEl.dataset.currentPath || '';

    if (selectedPath && directoryBrowserCallback) {
        directoryBrowserCallback(selectedPath);
    }

    closeDirectoryBrowser();
}

function closeDirectoryBrowser() {
    const modal = document.getElementById('directory-browser-modal');
    if (modal) {
        modal.style.display = 'none';
    }
    directoryBrowserCallback = null;
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

// File browser for inserting file paths

async function openFileBrowser() {
    if (!currentWorkspace) {
        alert('请先选择工作区');
        return;
    }

    // 创建或获取文件浏览弹窗
    let modal = document.getElementById('file-browser-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'file-browser-modal';
        modal.className = 'modal';
        modal.innerHTML = `
            <div class="modal-backdrop" onclick="closeFileBrowser()"></div>
            <div class="modal-content" style="max-width: 500px;">
                <div class="modal-header">
                    <h2>选择文件</h2>
                    <button class="modal-close" onclick="closeFileBrowser()">&times;</button>
                </div>
                <div class="modal-body">
                    <div class="file-browser-path" id="file-browser-current-path"></div>
                    <div class="file-browser-list" id="file-browser-list"></div>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }

    modal.style.display = 'flex';
    await loadWorkspaceFiles('');
}

async function loadWorkspaceFiles(path) {
    const listEl = document.getElementById('file-browser-list');
    const pathEl = document.getElementById('file-browser-current-path');

    listEl.innerHTML = '<div class="file-browser-loading">加载中...</div>';
    pathEl.textContent = path || '工作区根目录';

    try {
        const response = await fetch(`/api/files?workspace_uuid=${currentWorkspace.uuid}&path=${encodeURIComponent(path)}`);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();

        // 更新当前路径显示
        pathEl.textContent = data.path || '工作区根目录';

        // 渲染文件列表
        listEl.innerHTML = '';

        // 添加父目录按钮（如果有）
        if (data.parent !== null && data.parent !== undefined) {
            const parentItem = document.createElement('div');
            parentItem.className = 'file-browser-item file-browser-parent';
            parentItem.innerHTML = `<span>⬆️ ..</span>`;
            parentItem.onclick = () => loadWorkspaceFiles(data.parent);
            listEl.appendChild(parentItem);
        }

        // 添加文件和目录列表
        if (data.items.length === 0) {
            const emptyItem = document.createElement('div');
            emptyItem.className = 'file-browser-item file-browser-empty';
            emptyItem.textContent = '（空目录）';
            listEl.appendChild(emptyItem);
        } else {
            for (const item of data.items) {
                const el = document.createElement('div');
                el.className = 'file-browser-item';
                if (item.is_file) {
                    el.innerHTML = `<span>📄 ${item.name}</span>`;
                    el.onclick = () => insertFilePath(item.path);
                } else {
                    el.innerHTML = `<span>📁 ${item.name}</span>`;
                    el.onclick = () => loadWorkspaceFiles(item.path);
                }
                listEl.appendChild(el);
            }
        }
    } catch (err) {
        listEl.innerHTML = `<div class="file-browser-error">加载失败: ${err.message}</div>`;
    }
}

function insertFilePath(filePath) {
    const chatInput = document.getElementById('chat-input');
    const cursorPos = chatInput.selectionStart;
    const textBefore = chatInput.value.substring(0, cursorPos);
    const textAfter = chatInput.value.substring(chatInput.selectionEnd);

    // 插入文件路径
    chatInput.value = textBefore + filePath + textAfter;

    // 移动光标到插入位置之后
    const newPos = cursorPos + filePath.length;
    chatInput.setSelectionRange(newPos, newPos);
    chatInput.focus();

    closeFileBrowser();
}

function closeFileBrowser() {
    const modal = document.getElementById('file-browser-modal');
    if (modal) {
        modal.style.display = 'none';
    }
}

// ─ 升级功能 ──

async function runUpgrade() {
    const btn = document.getElementById('upgrade-check-btn');
    const statusEl = document.getElementById('upgrade-status');
    const mirrorSelect = document.getElementById('upgrade-mirror');

    // 禁用按钮，显示进度
    btn.disabled = true;
    btn.textContent = '升级中...';
    statusEl.textContent = '正在拉取最新代码...';
    statusEl.style.color = 'var(--text-secondary)';

    try {
        const response = await fetch('/api/upgrade', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mirror: mirrorSelect.value })
        });

        const data = await response.json();

        if (data.success) {
            statusEl.textContent = data.message;
            statusEl.style.color = '#27ae60';

            if (data.merge_conflict) {
                statusEl.textContent += '（存在合并冲突，请手动解决）';
                statusEl.style.color = '#f39c12';
            }

            // 2 秒后提示重启
            setTimeout(() => {
                if (confirm('升级完成！是否现在重启服务以应用更新？')) {
                    // 刷新页面
                    location.reload();
                }
            }, 1000);
        } else {
            statusEl.textContent = '失败：' + data.error;
            statusEl.style.color = 'var(--error-color)';
        }
    } catch (error) {
        statusEl.textContent = '升级失败：' + error.message;
        statusEl.style.color = 'var(--error-color)';
    } finally {
        btn.disabled = false;
        btn.textContent = '检查并升级';
    }
}
