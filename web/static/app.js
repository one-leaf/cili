// State
let currentWorkspace = null;  // {uuid, name, directory}
let currentSession = null;
let sessions = [];
let isSending = false;
// 本页持有的前台请求级 SSE 流属于哪个 session（null = 本页未持有）。
// 用途一：事件流的渲染去重 —— 本页正用请求级流渲染该会话时，忽略全局流的
//   同名事件，避免双份渲染。用 isSending 不行：loadSession 查到 /status.running
//   也会把它置 true（刷新页面后即如此），会把刷新后的全局渲染一并挡掉。
// 用途二：切走后旧流仍在推送，据此丢弃不属于当前会话的帧。
// 必须是 session 作用域而非布尔：切到 B 会话时 A 的流仍在读，若用布尔值，
// 会把 B 会话自己的全局事件也误判为「已有前台流」而去重掉。
let foregroundStreamSessionId = null;
let isMultiSelectMode = false;
let showHiddenSessions = false;
let selectedSessions = new Set();
let pendingImages = [];  // [{ data: "base64...", media_type: "image/png", preview_url: "data:..." }]
// 消息分页加载状态
let currentSessionLoadedOffset = 0;  // 已加载的消息数（从末尾算起）
let currentSessionHasMore = false;   // 是否还有更多历史消息
let isLoadingMore = false;           // 是否正在加载更多消息


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

// 侧栏宽度拖拽调整（与输入框拖拽一致的交互：拖动调整、双击重置）
const sidebarResizeHandle = document.querySelector('.sidebar-resize-handle');
const SIDEBAR_MIN_WIDTH = 180;
const SIDEBAR_MAX_WIDTH = 500;
const SIDEBAR_DEFAULT_WIDTH = 270;
const LEGACY_SIDEBAR_DEFAULT_WIDTH = 300;
const SIDEBAR_WIDTH_KEY = 'sidebar-width';

function getSidebarEl() {
    return document.querySelector('.sidebar');
}

function initSidebarResize() {
    if (!sidebarResizeHandle) return;

    let startX, startW, dragging = false;

    sidebarResizeHandle.addEventListener('mousedown', (e) => {
        e.preventDefault();
        dragging = true;
        startX = e.clientX;
        startW = getSidebarEl().getBoundingClientRect().width;
        getSidebarEl().classList.add('dragging');
        sidebarResizeHandle.classList.add('dragging');
        document.body.style.cursor = 'ew-resize';
        document.body.style.userSelect = 'none';
    });

    document.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const delta = e.clientX - startX;
        const newW = Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, startW + delta));
        getSidebarEl().style.width = newW + 'px';
        localStorage.setItem(SIDEBAR_WIDTH_KEY, newW);
    });

    document.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        getSidebarEl().classList.remove('dragging');
        sidebarResizeHandle.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
    });

    // 双击重置默认宽度
    sidebarResizeHandle.addEventListener('dblclick', () => {
        getSidebarEl().style.width = SIDEBAR_DEFAULT_WIDTH + 'px';
        localStorage.setItem(SIDEBAR_WIDTH_KEY, SIDEBAR_DEFAULT_WIDTH);
    });
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    loadWorkspaces();
    loadFooter();
    setupEventListeners();
    initInputResize();
    initSidebarResize();
    initSidebarState();
});

// Sidebar collapse / expand
function restoreSidebarWidth() {
    let saved = parseInt(localStorage.getItem(SIDEBAR_WIDTH_KEY));
    // 移除文件管理按钮后默认宽度收窄 30px；旧默认值一次性迁移，保留自定义宽度
    if (saved === LEGACY_SIDEBAR_DEFAULT_WIDTH) {
        saved = SIDEBAR_DEFAULT_WIDTH;
        localStorage.setItem(SIDEBAR_WIDTH_KEY, String(SIDEBAR_DEFAULT_WIDTH));
    }
    if (saved >= SIDEBAR_MIN_WIDTH && saved <= SIDEBAR_MAX_WIDTH) {
        getSidebarEl().style.width = saved + 'px';
    }
}

function initSidebarState() {
    if (localStorage.getItem('sidebar-collapsed') === 'true') {
        const sidebar = document.querySelector('.sidebar');
        const toggleBtn = document.getElementById('sidebar-toggle-btn');
        const openBtn = document.getElementById('sidebar-open-btn');
        sidebar.classList.add('collapsed');
        sidebar.style.width = '0px';
        toggleBtn.style.display = 'none';
        openBtn.style.display = '';
        sidebarResizeHandle.classList.add('hidden');
    } else {
        restoreSidebarWidth();
        sidebarResizeHandle.classList.remove('hidden');
    }
}

function toggleSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const toggleBtn = document.getElementById('sidebar-toggle-btn');
    const openBtn = document.getElementById('sidebar-open-btn');
    const isCollapsed = sidebar.classList.toggle('collapsed');
    if (isCollapsed) {
        sidebar.style.width = '0px';
        sidebarResizeHandle.classList.add('hidden');
    } else {
        sidebar.style.width = '';
        restoreSidebarWidth();
        sidebarResizeHandle.classList.remove('hidden');
    }
    toggleBtn.style.display = isCollapsed ? 'none' : '';
    openBtn.style.display = isCollapsed ? '' : 'none';
    localStorage.setItem('sidebar-collapsed', isCollapsed);
}

// Load footer info from JSON
async function loadFooter() {
    try {
        // 版本文件带时间戳缓存破除：自动升级更新版本号后，浏览器不再展示旧版
        const response = await fetch(`/static/version.json?t=${Date.now()}`);
        const data = await response.json();
        const footerContent = document.getElementById('footer-content');
        if (footerContent) {
            footerContent.innerHTML = `
                <button class="theme-toggle" id="theme-toggle-btn" title="切换亮色/暗色主题">
                    ${getThemeIcon()}
                </button>
                <span class="footer-dev">${data.app_name}</span>
                <span class="footer-sep">by</span>
                <span class="footer-company">${data.author}</span>
                <span class="footer-sep">·</span>
                <span class="footer-version">${data.version}</span>
            `;
            document.getElementById('theme-toggle-btn').addEventListener('click', toggleTheme);
        }
    } catch (error) {
        console.error('Failed to load footer:', error);
    }
}

// 返回当前主题对应的 SVG 图标（亮色=月亮，暗色=太阳）
function getThemeIcon() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    if (isDark) {
        // 太阳图标
        return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>';
    } else {
        // 月亮图标
        return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
    }
}

// 切换主题
function toggleTheme() {
    const html = document.documentElement;
    const isDark = html.getAttribute('data-theme') === 'dark';
    if (isDark) {
        html.removeAttribute('data-theme');
        localStorage.setItem('cili-theme', 'light');
    } else {
        html.setAttribute('data-theme', 'dark');
        localStorage.setItem('cili-theme', 'dark');
    }
    // 更新按钮图标
    const btn = document.getElementById('theme-toggle-btn');
    if (btn) btn.innerHTML = getThemeIcon();
    // 切换主题后重新初始化并渲染 mermaid 图表
    if (typeof mermaid !== 'undefined') {
        const newTheme = html.getAttribute('data-theme') === 'dark' ? 'dark' : 'default';
        mermaid.initialize({
            startOnLoad: false,
            theme: newTheme,
            securityLevel: 'loose',
        });
        // renderMermaidIn 会保存原始源码并用新主题重新渲染
        if (typeof renderMermaidIn === 'function') {
            renderMermaidIn(document.body);
        }
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

    // Workspace settings
    document.getElementById('workspace-settings-save-btn').addEventListener('click', saveWorkspaceSettings);
    document.getElementById('workspace-delete-btn').addEventListener('click', deleteWorkspaceConfig);
    document.getElementById('ws-browse-dir-btn').addEventListener('click', browseDirectory);

    // 项目提示词相关按钮
    document.getElementById('instructions-generate-btn').addEventListener('click', generateInstructions);
    document.getElementById('instructions-template-select').addEventListener('change', loadTemplate);

    // File insert button
    document.getElementById('insert-file-btn').addEventListener('click', () => {
        openFileBrowser();
    });

    // 特殊指令帮助按钮
    document.getElementById('help-btn').addEventListener('click', () => {
        document.getElementById('help-modal').style.display = 'flex';
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

    // New workspace wizard
    document.getElementById('wizard-back-btn').addEventListener('click', wizardGoBack);
    document.getElementById('wizard-next-btn').addEventListener('click', wizardGoNext);
    document.getElementById('wizard-location').addEventListener('change', updateWizardLocationHint);
    document.getElementById('wizard-custom-directory').addEventListener('input', updateWizardLocationHint);
    document.getElementById('wizard-browse-dir-btn').addEventListener('click', () => {
        const input = document.getElementById('wizard-custom-directory');
        openDirectoryBrowser(input.value.trim(), (selectedPath) => {
            input.value = selectedPath;
            updateWizardLocationHint();
        });
    });
    document.getElementById('wizard-slug').addEventListener('input', updateWizardLocationHint);
    document.getElementById('wizard-name').addEventListener('input', updateWizardLocationHint);

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
        document.getElementById('memory-btn').disabled = true;

        // 先渲染普通工作区（青少年模式：受限工作区带锁标记）
        data.workspaces.filter(ws => !ws.system).forEach(ws => {
            const option = document.createElement('option');
            option.value = ws.uuid;
            option.textContent = ws.locked ? `🔒 ${ws.name}（需家长解锁）` : ws.name;
            workspaceSelect.appendChild(option);
        });

        // System 放在最后
        const systemWs = data.workspaces.filter(ws => ws.system);
        systemWs.forEach(ws => {
            const option = document.createElement('option');
            option.value = ws.uuid;
            option.textContent = ws.locked ? `🔒 ${ws.name}（需家长解锁）` : ws.name;
            workspaceSelect.appendChild(option);
        });

        // If no workspaces, show hint
        if (data.workspaces.length === 0) {
            sessionsList.innerHTML = '<div class="empty-state">点击 + 创建工作区</div>';
            workspaceSettingsBtn.disabled = true;
            document.getElementById('memory-btn').disabled = true;
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
        closeEventSource();  // 无工作区：断开事件流
        clearPosition();
        workspaceSettingsBtn.disabled = true;
        document.getElementById('memory-btn').disabled = true;
        sessionsList.innerHTML = '<div class="empty-state">选择一个工作区</div>';
        chatMessages.innerHTML = '<div class="welcome-message"><h2>欢迎使用草履虫</h2><p>选择工作区并创建会话开始使用</p></div>';
        updateWorkspacePath();
        if (window.FileTree) FileTree.setWorkspace(null);
        return;
    }

    // Enable workspace settings button
    workspaceSettingsBtn.disabled = false;
    document.getElementById('memory-btn').disabled = false;

    // Find the workspace object from the list
    try {
        const response = await fetch('/api/workspaces');
        const data = await response.json();
        currentWorkspace = data.workspaces.find(ws => ws.uuid === selectedUuid) || null;
    } catch (error) {
        console.error('Failed to get workspace info:', error);
        currentWorkspace = { uuid: selectedUuid, name: selectedUuid };
    }

    // 青少年模式：选中受限工作区 → 弹家长密码按工作区解锁（默认 15 分钟）
    if (currentWorkspace && currentWorkspace.locked) {
        const wsName = currentWorkspace.name || selectedUuid;
        const pw = prompt(`工作区「${wsName}」已被青少年模式锁定，请输入家长密码解锁：`);
        if (!pw) {
            workspaceSelect.value = '';
            handleWorkspaceChange();
            return;
        }
        try {
            const unlockResp = await fetch('/api/teen/unlock', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: pw, scope: `ws:${selectedUuid}` })
            });
            const unlockData = await unlockResp.json().catch(() => ({}));
            if (!unlockResp.ok) {
                alert(unlockData.detail || '解锁失败');
                workspaceSelect.value = '';
                handleWorkspaceChange();
                return;
            }
            currentWorkspace.locked = false;
            loadTeenStatus();
        } catch (e) {
            alert('解锁请求失败: ' + e.message);
            workspaceSelect.value = '';
            handleWorkspaceChange();
            return;
        }
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
        if (window.FileTree) FileTree.setWorkspace(null);
        sessionsList.innerHTML = '<div class="empty-state">工作区未找到</div>';
        return;
    }

    updateWorkspacePath();
    if (window.FileTree) FileTree.setWorkspace(currentWorkspace);
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

function joinPreviewPath(parent, child) {
    if (!parent) return child;
    if (/[\\/]$/.test(parent)) return parent + child;
    return parent + (parent.includes('\\') ? '\\' : '/') + child;
}

function updateWizardLocationHint() {
    if (!wizardState.options) return;
    const location = document.getElementById('wizard-location').value;
    const item = (wizardState.options.locations || []).find(entry => entry.id === location);
    const slug = document.getElementById('wizard-slug').value.trim() ||
        document.getElementById('wizard-name').value.trim();
    const parent = item?.parent || '';
    const customDir = document.getElementById('wizard-custom-directory').value.trim();
    const hint = document.getElementById('wizard-location-hint');
    if (hint) hint.textContent = location === 'custom' ? '' : `父目录：${parent}`;
    const customHint = document.getElementById('wizard-custom-hint');
    if (customHint) customHint.textContent = location === 'custom' ? '最终目录：自定义父目录 + 目录名' : '';
    const preview = document.getElementById('wizard-path-preview');
    if (preview) {
        preview.textContent = location === 'custom'
            ? '最终目录：' + (customDir ? (slug ? joinPreviewPath(customDir, slug) : customDir) : '(未填写)')
            : '最终目录：' + (parent ? (slug ? joinPreviewPath(parent, slug) : parent) : '(未知)');
    }
    const customGroup = document.getElementById('wizard-custom-group');
    if (customGroup) customGroup.style.display = location === 'custom' ? '' : 'none';
}

// ─── 新建工作区向导 ─────────────────────────────────────────────────────────

const wizardState = {
    step: 1,
    options: null,
    category: '',
    preview: null,
    creating: false,
    result: null,
};

function handleNewWorkspace() {
    document.getElementById('new-workspace-modal').style.display = 'flex';
    wizardState.step = 1;
    wizardState.category = '';
    wizardState.preview = null;
    wizardState.result = null;
    wizardState.creating = false;
    wizardState.options = null;
    document.getElementById('wizard-status').textContent = '';
    document.getElementById('wizard-result').innerHTML = '';
    document.getElementById('wizard-preview').innerHTML = '<div class="textbook-empty">点击下一步生成预览。</div>';
    document.getElementById('wizard-name').value = '';
    document.getElementById('wizard-slug').value = '';
    document.getElementById('wizard-custom-directory').value = '';
    document.getElementById('wizard-allow-existing').checked = false;
    document.getElementById('wizard-git-enabled').checked = true;
    document.getElementById('wizard-memory-enabled').checked = true;
    renderWizardCategories();
    loadWizardOptions();
    showWizardStep(1);
}

async function loadWizardOptions() {
    try {
        const response = await fetch('/api/workspace-wizard/options');
        if (!response.ok) throw new Error((await response.json()).detail || '加载向导选项失败');
        wizardState.options = await response.json();
        renderWizardOptions();
    } catch (error) {
        setWizardStatus('✗ ' + error.message, 'error');
    }
}

function applyWizardCategoryInputs() {
    const isStudy = wizardState.category === 'study';
    const nameInput = document.getElementById('wizard-name');
    const slugInput = document.getElementById('wizard-slug');
    if (nameInput) {
        nameInput.disabled = isStudy;
        nameInput.placeholder = isStudy ? '学习项目自动按「年级-科目」命名' : '输入项目名称（选填）';
    }
    if (slugInput) {
        slugInput.disabled = isStudy;
        slugInput.placeholder = isStudy ? '自动按科目生成' : '默认按名称生成，选填';
    }
}

function renderWizardCategories() {
    applyWizardCategoryInputs();
    const categories = wizardState.options?.categories || [
        { id: 'study', name: '学习项目', description: '按科目拆分工作区。' },
        { id: 'office', name: '日常办公', description: '文档、表格、演示与归档。' },
        { id: 'research', name: '科研项目', description: '文献、实验与论文写作。' },
        { id: 'blank', name: '空白项目', description: '只创建基础工作区。' },
    ];
    const grid = document.getElementById('wizard-categories');
    grid.innerHTML = '';
    categories.forEach(category => {
        const card = document.createElement('button');
        card.type = 'button';
        card.className = 'wizard-category-card' + (wizardState.category === category.id ? ' selected' : '');
        card.innerHTML = '<strong>' + escapeHtml(category.name) + '</strong><span>' + escapeHtml(category.description) + '</span>';
        card.addEventListener('click', () => {
            wizardState.category = category.id;
            wizardState.preview = null;
            renderWizardCategories();
            renderWizardConfig();
            showWizardStep(2);
        });
        grid.appendChild(card);
    });
}

function renderWizardOptions() {
    const options = wizardState.options;
    if (!options) return;
    const locationSelect = document.getElementById('wizard-location');
    const old = locationSelect.value;
    locationSelect.innerHTML = '';
    (options.locations || []).forEach(item => {
        const option = document.createElement('option');
        option.value = item.id;
        option.textContent = item.label;
        locationSelect.appendChild(option);
    });
    locationSelect.value = old || options.defaults?.location || 'cili';
    updateWizardLocationHint();

    const profile = collectWizardProfile(false);
    const stageSelect = document.getElementById('wizard-study-stage');
    if (stageSelect) {
        const oldStage = stageSelect.value || profile.stage;
        stageSelect.innerHTML = '<option value="">选择学段...</option>';
        (options.stages || []).forEach(stage => {
            const option = document.createElement('option');
            option.value = stage;
            option.textContent = stage;
            stageSelect.appendChild(option);
        });
        stageSelect.value = oldStage;
    }

    updateStudyGradeOptions();
}

function updateStudyGradeOptions() {
    const gradeSelect = document.getElementById('wizard-study-grade');
    if (!gradeSelect) return;
    const stage = document.getElementById('wizard-study-stage')?.value || '';
    const grades = (wizardState.options?.grades || {})[stage] || [];
    const old = gradeSelect.value;
    gradeSelect.innerHTML = '<option value="">选择年级...</option>';
    grades.forEach(grade => {
        const option = document.createElement('option');
        option.value = grade;
        option.textContent = grade;
        gradeSelect.appendChild(option);
    });
    gradeSelect.value = grades.includes(old) ? old : '';
}

function renderWizardConfig() {
    applyWizardCategoryInputs();
    const container = document.getElementById('wizard-category-config');
    const category = wizardState.category;
    if (!category) {
        container.innerHTML = '<div class="textbook-empty">请先选择项目类型。</div>';
        return;
    }
    if (category === 'study') {
        container.innerHTML = getStudyConfigHtml();
        bindStudyConfigEvents();
        return;
    }
    if (category === 'office') {
        container.innerHTML = getOfficeConfigHtml();
        return;
    }
    if (category === 'research') {
        container.innerHTML = getResearchConfigHtml();
        return;
    }
    container.innerHTML = '<p>空白项目无需额外配置。</p>';
}

function getStudyConfigHtml() {
    return `
        <div class="wizard-grid">
            <div class="form-group"><label>孩子称呼</label><input type="text" id="wizard-study-learner" placeholder="用于对话称呼"></div>
            <div class="form-group"><label>学段</label><select id="wizard-study-stage"></select></div>
            <div class="form-group"><label>年级</label><select id="wizard-study-grade"></select></div>
            <div class="form-group"><label>考区</label><input type="text" id="wizard-study-region" placeholder="例如：广东"></div>
            <div class="form-group"><label>学习重点</label><select id="wizard-study-focus"><option>同步学习</option><option>中考复习</option><option>高考复习</option></select></div>
            <div class="form-group"><label>每天学习分钟数</label><input type="number" id="wizard-study-minutes" min="0" max="600" value="60"></div>
        </div>
        <div class="form-group"><label>家长备注</label><textarea id="wizard-study-note" rows="2" placeholder="孩子学习习惯、薄弱点等"></textarea></div>
        <div class="form-group"><label>科目（勾选后按科目选择教材版本）</label><div id="wizard-study-subjects" class="wizard-subject-list"></div></div>
        <label class="wizard-checkbox-row"><input type="checkbox" id="wizard-download-textbooks" checked> 自动下载匹配教材</label>
    `;
}

function getOfficeConfigHtml() {
    return `
        <div class="wizard-grid">
            <div class="form-group"><label>岗位/角色</label><input type="text" id="wizard-office-role" placeholder="例如：行政 / 项目经理"></div>
            <div class="form-group"><label>文档语言</label><select id="wizard-office-language"><option>中文</option><option>英文</option><option>中英双语</option></select></div>
        </div>
    `;
}

function getResearchConfigHtml() {
    return `
        <div class="wizard-grid">
            <div class="form-group"><label>学科</label><input type="text" id="wizard-research-field" placeholder="例如：计算机"></div>
            <div class="form-group"><label>研究方向</label><input type="text" id="wizard-research-direction"></div>
            <div class="form-group"><label>引用格式</label><select id="wizard-research-citation"><option>GB/T 7714</option><option>APA</option><option>MLA</option><option>Chicago</option><option>IEEE</option></select></div>
        </div>
    `;
}

function editionCoversGrade(item, grade) {
    if (!grade) return true;
    const grades = item.grades || [];
    if (!grades.length) return true;
    return grades.includes(grade);
}

function renderStudySubjectRows() {
    const stage = document.getElementById('wizard-study-stage')?.value || '';
    const grade = document.getElementById('wizard-study-grade')?.value || '';
    const subjects = wizardState.options?.study_subjects?.[stage] || [];
    const container = document.getElementById('wizard-study-subjects');
    container.innerHTML = '';
    subjects.forEach(subject => {
        const row = document.createElement('div');
        row.className = 'wizard-subject-row';
        const label = document.createElement('label');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = subject;
        checkbox.checked = ['语文', '数学', '英语'].includes(subject);
        checkbox.addEventListener('change', updateWizardSubjectHint);
        label.appendChild(checkbox);
        label.append(subject);
        row.appendChild(label);
        const select = document.createElement('select');
        select.className = 'wizard-subject-edition';
        select.dataset.subject = subject;
        select.title = `${subject} 教材版本`;
        const allEditions = wizardState.options?.textbook_editions?.[stage]?.[subject] || [];
        const editions = allEditions.filter(item => editionCoversGrade(item, grade));
        const usable = editions.length ? editions : allEditions;
        if (usable.length) {
            usable.forEach(edition => {
                const option = document.createElement('option');
                option.value = edition.name;
                option.textContent = edition.publisher ? `${edition.name}（${edition.publisher}）` : edition.name;
                select.appendChild(option);
            });
            const fallback = usable.find(edition => edition.default) || usable[0];
            select.value = fallback.name;
        } else {
            select.disabled = true;
            const option = document.createElement('option');
            option.value = '';
            option.textContent = '暂无教材';
            select.appendChild(option);
        }
        row.appendChild(select);
        container.appendChild(row);
    });
    updateWizardSubjectHint();
}

function bindStudyConfigEvents() {
    document.getElementById('wizard-study-stage')?.addEventListener('change', () => {
        renderStudySubjectRows();
        updateStudyGradeOptions();
    });
    document.getElementById('wizard-study-grade')?.addEventListener('change', renderStudySubjectRows);
    renderWizardOptions();
}

function updateWizardSubjectHint() {
    const checked = Array.from(document.querySelectorAll('#wizard-study-subjects input:checked')).map(x => x.value);
    setWizardStatus(checked.length ? `已选 ${checked.length} 个科目，将创建 ${checked.length} 个工作区。` : '请至少选择一个科目。');
}

function collectWizardProfile(strict = false) {
    const category = wizardState.category;
    if (category === 'study') {
        const subject_overrides = {};
        document.querySelectorAll('#wizard-study-subjects select.wizard-subject-edition').forEach(sel => {
            if (sel.dataset.subject && sel.value) {
                subject_overrides[sel.dataset.subject] = { textbook_edition: sel.value };
            }
        });
        const profile = {
            learner_name: document.getElementById('wizard-study-learner')?.value.trim() || '',
            stage: document.getElementById('wizard-study-stage')?.value || '',
            grade: document.getElementById('wizard-study-grade')?.value || '',
            exam_region: document.getElementById('wizard-study-region')?.value.trim() || '',
            focus: document.getElementById('wizard-study-focus')?.value || '同步学习',
            daily_minutes: Number(document.getElementById('wizard-study-minutes')?.value || 0),
            parent_note: document.getElementById('wizard-study-note')?.value.trim() || '',
            subject_overrides,
        };
        if (strict && !profile.learner_name) throw new Error('请填写孩子称呼');
        if (strict && !profile.stage) throw new Error('请选择学段');
        if (strict && !profile.grade) throw new Error('请选择年级');
        return profile;
    }
    if (category === 'office') {
        return {
            role: document.getElementById('wizard-office-role')?.value.trim() || '',
            doc_language: document.getElementById('wizard-office-language')?.value || '中文',
        };
    }
    if (category === 'research') {
        return {
            field: document.getElementById('wizard-research-field')?.value.trim() || '',
            direction: document.getElementById('wizard-research-direction')?.value.trim() || '',
            citation_style: document.getElementById('wizard-research-citation')?.value || 'GB/T 7714',
        };
    }
    return {};
}

function collectWizardSubjects() {
    return Array.from(document.querySelectorAll('#wizard-study-subjects input:checked')).map(x => x.value);
}

function setWizardStatus(message, type = '') {
    const el = document.getElementById('wizard-status');
    el.textContent = message;
    el.className = 'settings-status' + (type ? ' ' + type : '');
}

function showWizardStep(step) {
    wizardState.step = step;
    document.querySelectorAll('.wizard-step').forEach(item => {
        item.classList.toggle('active', item.id === `wizard-step-${step}`);
    });
    document.querySelectorAll('.wizard-step-indicator').forEach(item => {
        item.classList.toggle('active', Number(item.dataset.step) <= step);
        item.classList.toggle('current', Number(item.dataset.step) === step);
    });
    const back = document.getElementById('wizard-back-btn');
    const next = document.getElementById('wizard-next-btn');
    back.style.display = step === 1 ? 'none' : '';
    next.textContent = step === 4 ? '创建' : (step === 5 ? '完成' : '下一步');
    next.disabled = false;
}

function wizardGoBack() {
    if (wizardState.step > 1) showWizardStep(wizardState.step - 1);
}

async function workspaceNameExists(name) {
    try {
        const response = await fetch('/api/workspaces');
        if (!response.ok) return false;
        const data = await response.json();
        return (data.workspaces || []).some(ws => ws.name === name);
    } catch {
        return false; // 网络失败时放行，由后端预检兜底
    }
}

async function wizardGoNext() {
    try {
        if (wizardState.step === 1) {
            if (!wizardState.category) throw new Error('请选择项目类型');
            renderWizardConfig();
            showWizardStep(2);
            return;
        }
        if (wizardState.step === 2) {
            const name = document.getElementById('wizard-name').value.trim();
            if (name && wizardState.category !== 'study' && await workspaceNameExists(name)) {
                throw new Error(`已存在同名工作区「${name}」，请修改项目名称`);
            }
            renderWizardConfig();
            showWizardStep(3);
            return;
        }
        if (wizardState.step === 3) {
            collectWizardProfile(true);
            await previewWizard();
            showWizardStep(4);
            return;
        }
        if (wizardState.step === 4) {
            await createWizard();
            return;
        }
        if (wizardState.step === 5) {
            document.getElementById('new-workspace-modal').style.display = 'none';
            return;
        }
    } catch (error) {
        setWizardStatus('✗ ' + error.message, 'error');
    }
}

function wizardRequest() {
    return {
        category: wizardState.category,
        name: document.getElementById('wizard-name').value.trim(),
        location: document.getElementById('wizard-location').value,
        slug: document.getElementById('wizard-slug').value.trim(),
        custom_directory: document.getElementById('wizard-custom-directory').value.trim(),
        profile: collectWizardProfile(true),
        subjects: collectWizardSubjects(),
        allow_existing: document.getElementById('wizard-allow-existing').checked,
    };
}

function renderWizardPreview(data) {
    const container = document.getElementById('wizard-preview');
    container.innerHTML = '';
    const warnings = data.warnings || [];
    if (warnings.length) {
        const warning = document.createElement('div');
        warning.className = 'wizard-warning';
        warning.textContent = warnings.join('；');
        container.appendChild(warning);
    }
    data.workspaces.forEach(item => {
        const card = document.createElement('div');
        card.className = 'wizard-preview-card';
        const title = document.createElement('strong');
        title.textContent = item.name;
        const path = document.createElement('div');
        path.textContent = item.directory + (item.exists ? '（目录已存在）' : '');
        const pre = document.createElement('pre');
        pre.textContent = item.agents_md;
        card.append(title, path, pre);
        container.appendChild(card);
    });
}

async function previewWizard() {
    const next = document.getElementById('wizard-next-btn');
    next.disabled = true;
    setWizardStatus('生成预览...');
    try {
        const response = await fetch('/api/workspace-wizard/preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(wizardRequest()),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '生成预览失败');
        wizardState.preview = data;
        renderWizardPreview(data);
        setWizardStatus('预览已生成，确认后点击创建。');
    } finally {
        next.disabled = false;
    }
}

async function createWizard() {
    if (wizardState.creating) return;
    wizardState.creating = true;
    const next = document.getElementById('wizard-next-btn');
    next.disabled = true;
    setWizardStatus('正在创建...');
    try {
        const body = {
            ...wizardRequest(),
            overwrite_agents: false,
            git_enabled: document.getElementById('wizard-git-enabled').checked,
            memory_enabled: document.getElementById('wizard-memory-enabled').checked,
            download_textbooks: document.getElementById('wizard-download-textbooks')?.checked || false,
        };
        const response = await fetch('/api/workspace-wizard/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '创建失败');
        wizardState.result = data;
        showWizardStep(5);
        renderWizardResult(data);
        setWizardStatus('✓ 创建完成', 'success');
        await loadWorkspaces();
    } catch (error) {
        setWizardStatus('✗ ' + error.message, 'error');
    } finally {
        wizardState.creating = false;
        next.disabled = false;
    }
}

function renderWizardResult(data) {
    const container = document.getElementById('wizard-result');
    container.innerHTML = '';
    (data.created || []).forEach(item => {
        const card = document.createElement('div');
        card.className = 'wizard-result-card success';
        card.innerHTML = '<strong>' + escapeHtml(item.name) + '</strong><div>' + escapeHtml(item.directory) + '</div>';
        if (item.subject) {
            const subject = document.createElement('div');
            subject.textContent = '科目：' + item.subject;
            card.appendChild(subject);
        }
        container.appendChild(card);
    });
    (data.failed || []).forEach(item => {
        const card = document.createElement('div');
        card.className = 'wizard-result-card error';
        card.innerHTML = '<strong>' + escapeHtml(item.name) + '</strong><div>' + escapeHtml(item.error) + '</div>';
        container.appendChild(card);
    });
    (data.textbook_tasks || []).forEach(task => {
        const card = document.createElement('div');
        card.className = 'wizard-result-card info';
        card.textContent = `已启动教材下载：${task.subject}，共 ${task.total_books} 本。`;
        container.appendChild(card);
    });
    (data.warnings || []).forEach(warning => {
        const card = document.createElement('div');
        card.className = 'wizard-warning';
        card.textContent = warning;
        container.appendChild(card);
    });
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

        // 展示名由后端统一给出（core/session.py 的 session_display_name），
        // 保证 Web 与 QQ 的 /session 列表口径一致；缺失时退回本地兜底。
        const displayName = session.display_name || session.name || session.preview || '未命名';
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
        ${showDelete ? '<div class="session-dropdown-item" data-action="delete">删除会话</div>' : ''}
        ${showShare ? '<div class="session-dropdown-item" data-action="share">分享会话</div>' : ''}
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
                            <tr><td class="info-key">预填充速度</td><td class="info-value">${(usage.prefill_speed || 0).toLocaleString()} tokens/s</td></tr>
                            <tr><td class="info-key">生成速度</td><td class="info-value">${(usage.generation_speed || 0).toLocaleString()} tokens/s</td></tr>
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
        // 初始加载最近 50 条消息
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}?limit=50&offset=0`);
        const session = await response.json();
        currentSession = session;
        // 记录已加载的消息偏移量（从末尾算起）
        currentSessionLoadedOffset = session.messages.length;
        currentSessionHasMore = session.has_more;
        savePosition({ session_id: sessionId });

        console.log('Session switched to:', currentSession.session_id);

        // 清理所有正在进行的工具输出轮询与 worker 卡片状态（修复切会话泄漏）
        clearAllToolStreaming();

        // 重连全局事件流到当前会话（worker 消息 + 工具输出实时推送）
        connectEventSource();

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
            // 恢复该会话运行中的 worker 卡片（历史消息里只有已完成的 agent_ref）
            recoverRunningCards();
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

        // 完整重置发送/停止状态（可能在旧会话运行中创建新会话）
        clearAllToolStreaming();
        // 重连事件流到新会话
        connectEventSource();
        chatInput.disabled = false;
        sendBtn.disabled = false;
        sendBtn.textContent = '发送';
        sendBtn.classList.remove('btn-danger');
        sendBtn.classList.add('btn-primary');
        isSending = false;
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
    statusEl.textContent = currentWorkspace.uuid;

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
        statusEl.textContent = `${currentWorkspace.uuid}（系统工作区，不可修改）`;
    }

    // 切换到基本设置 tab 并加载项目提示词
    switchWorkspaceSettingsTab('ws-basic');
    loadInstructions();
    loadTemplateList();
    loadGitSettings();
}


async function saveWorkspaceSettings() {
    if (!currentWorkspace) return;

    const statusEl = document.getElementById('workspace-settings-status');
    statusEl.textContent = '保存中...';
    statusEl.className = 'settings-status';

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
        statusEl.className = 'settings-status success';

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
            statusEl.textContent = currentWorkspace.uuid;
            statusEl.className = 'settings-status';
        }, 1000);
    } catch (error) {
        statusEl.textContent = '✗ 保存失败: ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

async function deleteWorkspaceConfig() {
    if (!currentWorkspace) return;

    const wsName = currentWorkspace.name || currentWorkspace.uuid;
    const confirmed = confirm(
        `确定要删除工作区「${wsName}」的配置吗？\n\n` +
        `⚠ 将从工作区列表中移除该配置\n\n` +
        `✓ 工作区文件和会话数据不会被删除\n\n` +
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
            let detail = `服务器错误 (${response.status})`;
            try {
                const err = await response.json();
                detail = err.detail || detail;
            } catch {
                // 响应不是 JSON，使用默认错误信息
            }
            throw new Error(detail);
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

// ─── 工作区设置 Tab 切换 ─────────────────────────────────────────

function switchWorkspaceSettingsTab(tabName) {
    // 切换 tab 按钮状态
    document.querySelectorAll('#workspace-settings-modal .settings-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabName);
    });
    // 切换 tab 内容
    document.querySelectorAll('#workspace-settings-modal .tab-content').forEach(content => {
        content.classList.toggle('active', content.id === `tab-${tabName}`);
    });
    // 切换 footer 按钮
    const footerSaveBtn = document.getElementById('workspace-settings-save-btn');
    const deleteBtn = document.getElementById('workspace-delete-btn');

    if (tabName === 'ws-instructions') {
        // 项目提示词 tab：显示保存提示词按钮（靠右），隐藏删除按钮
        footerSaveBtn.textContent = '保存提示词';
        footerSaveBtn.style.display = '';
        footerSaveBtn.style.marginLeft = 'auto';
        footerSaveBtn.onclick = saveInstructions;
        deleteBtn.style.display = 'none';
    } else if (tabName === 'ws-git') {
        // 版本管理 tab：显示保存远程按钮，隐藏删除按钮
        footerSaveBtn.textContent = '保存远程设置';
        footerSaveBtn.style.display = '';
        footerSaveBtn.style.marginLeft = 'auto';
        footerSaveBtn.onclick = saveGitRemote;
        deleteBtn.style.display = 'none';
    } else if (tabName === 'ws-textbooks') {
        // 教材下载 tab：使用面板内按钮，隐藏底部保存/删除
        footerSaveBtn.style.display = 'none';
        deleteBtn.style.display = 'none';
        initTextbookPanel();
    } else {
        // 基本设置 tab：显示 footer 按钮
        footerSaveBtn.textContent = '保存';
        footerSaveBtn.style.display = '';
        footerSaveBtn.style.marginLeft = '';
        footerSaveBtn.onclick = saveWorkspaceSettings;
        const isSystem = currentWorkspace?.system || currentWorkspace?.uuid === 'system';
        deleteBtn.style.display = isSystem ? 'none' : '';
    }
}

// ─── 项目提示词相关 ─────────────────────────────────────────────

async function loadInstructions() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('instructions-status');
    const editor = document.getElementById('instructions-editor');
    const filenameEl = document.getElementById('instructions-filename');

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/instructions`);
        const data = await response.json();

        editor.value = data.content || '';
        filenameEl.textContent = data.filename || 'AGENTS.md';
        if (!data.found) {
            statusEl.textContent = '未找到项目指令文件，将创建 AGENTS.md';
            statusEl.className = 'settings-status info';
        } else {
            statusEl.textContent = '';
        }
    } catch (error) {
        statusEl.textContent = '✗ 加载失败: ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

async function saveInstructions() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('instructions-status');
    const editor = document.getElementById('instructions-editor');
    const filenameEl = document.getElementById('instructions-filename');

    statusEl.textContent = '保存中...';
    statusEl.className = 'settings-status info';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/instructions`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filename: filenameEl.textContent,
                content: editor.value
            })
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Failed to save');
        }

        statusEl.textContent = '✓ 保存成功';
        statusEl.className = 'settings-status success';
    } catch (error) {
        statusEl.textContent = '✗ 保存失败: ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

async function generateInstructions() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('instructions-status');
    const generateBtn = document.getElementById('instructions-generate-btn');

    const confirmed = confirm(
        '将启动 AI 分析项目代码并生成 AGENTS.md。\n\n' +
        '此过程可能需要几分钟，期间会扫描项目结构和代码。\n\n' +
        '是否继续？'
    );
    if (!confirmed) return;

    generateBtn.disabled = true;
    generateBtn.textContent = '生成中...';
    statusEl.textContent = '正在启动 AI 生成...';
    statusEl.className = 'settings-status info';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/instructions/generate`, {
            method: 'POST'
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Failed to start');
        }

        const data = await response.json();
        const taskId = data.task_id;

        // 轮询状态
        pollGenerateStatus(taskId);
    } catch (error) {
        statusEl.textContent = '✗ 启动失败: ' + error.message;
        statusEl.className = 'settings-status error';
        generateBtn.disabled = false;
        generateBtn.textContent = 'AI 生成';
    }
}

async function pollGenerateStatus(taskId) {
    const statusEl = document.getElementById('instructions-status');
    const generateBtn = document.getElementById('instructions-generate-btn');
    const editor = document.getElementById('instructions-editor');
    const filenameEl = document.getElementById('instructions-filename');

    const poll = async () => {
        try {
            const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/instructions/generate/${taskId}`);
            const data = await response.json();

            if (data.status === 'running') {
                statusEl.textContent = 'AI 正在分析代码...';
                setTimeout(poll, 3000);
            } else if (data.status === 'completed') {
                statusEl.textContent = '✓ 生成完成';
                statusEl.className = 'settings-status success';
                generateBtn.disabled = false;
                generateBtn.textContent = 'AI 生成';
                // 重新加载内容
                filenameEl.textContent = 'AGENTS.md';
                await loadInstructions();
            } else {
                statusEl.textContent = '✗ 生成失败: ' + (data.summary || 'Unknown error');
                statusEl.className = 'settings-status error';
                generateBtn.disabled = false;
                generateBtn.textContent = 'AI 生成';
            }
        } catch (error) {
            statusEl.textContent = '✗ 查询状态失败: ' + error.message;
            statusEl.className = 'settings-status error';
            generateBtn.disabled = false;
            generateBtn.textContent = 'AI 生成';
        }
    };

    poll();
}

async function loadTemplateList() {
    if (!currentWorkspace) return;
    const select = document.getElementById('instructions-template-select');

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/instructions/templates`);
        const data = await response.json();

        select.innerHTML = '<option value="">加载模板...</option>';
        for (const tpl of data.templates) {
            const option = document.createElement('option');
            option.value = tpl.name;
            option.textContent = tpl.name;
            select.appendChild(option);
        }
    } catch (error) {
        console.error('Failed to load templates:', error);
    }
}

async function loadTemplate() {
    if (!currentWorkspace) return;
    const select = document.getElementById('instructions-template-select');
    const templateName = select.value;
    if (!templateName) return;

    const statusEl = document.getElementById('instructions-status');
    const editor = document.getElementById('instructions-editor');

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/instructions/templates/${templateName}`);
        const data = await response.json();

        if (editor.value.trim()) {
            const confirmed = confirm('加载模板将覆盖当前内容，是否继续？');
            if (!confirmed) {
                select.value = '';
                return;
            }
        }

        editor.value = data.content;
        statusEl.textContent = `已加载模板: ${templateName}`;
        statusEl.className = 'settings-status info';
        select.value = '';
    } catch (error) {
        statusEl.textContent = '✗ 加载模板失败: ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

// ─── 工作区 Git 版本管理 ─────────────────────────────────────────

async function loadGitSettings() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('git-settings-status');
    const toggle = document.getElementById('git-enabled-toggle');
    const statusSection = document.getElementById('git-status-section');
    const commitsList = document.getElementById('git-commits-list');
    const remoteSection = document.getElementById('git-remote-section');

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/git/status`);
        const data = await response.json();

        toggle.checked = data.enabled;
        // 显示远程仓库区域
        if (remoteSection) {
            remoteSection.style.display = data.enabled ? '' : 'none';
        }
        if (data.initialized) {
            statusSection.style.display = '';
            renderGitCommits(data.commits);
        } else {
            statusSection.style.display = 'none';
            commitsList.innerHTML = '';
        }
        // 加载远程仓库信息
        const remoteInfo = data.remote_info;
        if (remoteInfo) {
            const urlInput = document.getElementById('git-remote-url');
            const usernameInput = document.getElementById('git-username');
            const tokenInput = document.getElementById('git-token');
            const infoEl = document.getElementById('git-remote-info');
            if (urlInput) urlInput.value = remoteInfo.remote_url || '';
            if (usernameInput) usernameInput.value = remoteInfo.username || '';
            // Token 不回显明文，只在有值时显示提示
            if (tokenInput) tokenInput.value = '';
            if (tokenInput) tokenInput.placeholder = remoteInfo.has_token ? `已配置 (${remoteInfo.token_preview})` : 'Personal Access Token';
            if (infoEl) {
                infoEl.textContent = remoteInfo.remote_url ? '已配置远程仓库，设置定时同步后每 2 小时自动拉取/推送' : '';
            }
        }
        statusEl.textContent = '';
        statusEl.className = 'settings-status';
    } catch (error) {
        statusEl.textContent = '✗ 加载 Git 设置失败: ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

async function toggleGitEnabled() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('git-settings-status');
    const toggle = document.getElementById('git-enabled-toggle');
    const enabled = toggle.checked;

    statusEl.textContent = enabled ? '正在启用...' : '正在关闭...';
    statusEl.className = 'settings-status info';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/git/settings`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ git_enabled: enabled })
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || '操作失败');
        }

        const data = await response.json();
        statusEl.textContent = enabled ? '✓ Git 版本管理已启用' : '✓ Git 版本管理已关闭';
        statusEl.className = 'settings-status success';

        // 刷新状态
        await loadGitSettings();
    } catch (error) {
        toggle.checked = !enabled;  // 回滚
        statusEl.textContent = '✗ ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

async function saveGitRemote() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('git-settings-status');
    const urlInput = document.getElementById('git-remote-url');
    const usernameInput = document.getElementById('git-username');
    const tokenInput = document.getElementById('git-token');

    statusEl.textContent = '保存中...';
    statusEl.className = 'settings-status info';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/git/settings`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                git_enabled: document.getElementById('git-enabled-toggle').checked,
                git_remote_url: urlInput.value.trim(),
                git_username: usernameInput.value.trim(),
                git_token: tokenInput.value.trim()
            })
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || '保存失败');
        }

        statusEl.textContent = '✓ 远程仓库设置已保存';
        statusEl.className = 'settings-status success';
        // 清空 token 输入框
        tokenInput.value = '';
        await loadGitSettings();
    } catch (error) {
        statusEl.textContent = '✗ ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

async function gitPull() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('git-settings-status');
    const pullBtn = document.getElementById('git-pull-btn');

    pullBtn.disabled = true;
    pullBtn.textContent = '拉取中...';
    statusEl.textContent = '正在拉取...';
    statusEl.className = 'settings-status info';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/git/pull`, { method: 'POST' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '拉取失败');
        statusEl.textContent = '✓ ' + data.message;
        statusEl.className = 'settings-status success';
        await loadGitSettings();
    } catch (error) {
        statusEl.textContent = '✗ ' + error.message;
        statusEl.className = 'settings-status error';
    } finally {
        pullBtn.disabled = false;
        pullBtn.textContent = '拉取';
    }
}

async function gitPush() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('git-settings-status');
    const pushBtn = document.getElementById('git-push-btn');

    pushBtn.disabled = true;
    pushBtn.textContent = '推送中...';
    statusEl.textContent = '正在推送...';
    statusEl.className = 'settings-status info';

    try {
        // 先静默同步远程地址到 git config
        const urlInput = document.getElementById('git-remote-url');
        const usernameInput = document.getElementById('git-username');
        const tokenInput = document.getElementById('git-token');
        await fetch(`/api/workspaces/${currentWorkspace.uuid}/git/settings`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                git_enabled: document.getElementById('git-enabled-toggle').checked,
                git_remote_url: urlInput.value.trim(),
                git_username: usernameInput.value.trim(),
                git_token: tokenInput.value.trim()
            })
        });
        // 推送
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/git/push`, { method: 'POST' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '推送失败');
        statusEl.textContent = '✓ ' + data.message;
        statusEl.className = 'settings-status success';
        tokenInput.value = '';
    } catch (error) {
        statusEl.textContent = '✗ ' + error.message;
        statusEl.className = 'settings-status error';
    } finally {
        pushBtn.disabled = false;
        pushBtn.textContent = '推送';
    }
}

function renderGitCommits(commits) {
    const list = document.getElementById('git-commits-list');
    if (!commits || commits.length === 0) {
        list.innerHTML = '<div class="git-commit-empty">暂无提交记录</div>';
        return;
    }
    list.innerHTML = commits.map(c => `
        <div class="git-commit-item">
            <span class="git-commit-hash">${c.hash}</span>
            <span class="git-commit-date">${c.date}</span>
            <span class="git-commit-subject">${escapeHtml(c.subject)}</span>
        </div>
    `).join('');
}

// 绑定事件
document.getElementById('git-enabled-toggle')?.addEventListener('change', toggleGitEnabled);
document.getElementById('git-refresh-btn')?.addEventListener('click', loadGitSettings);
document.getElementById('git-pull-btn')?.addEventListener('click', gitPull);
document.getElementById('git-push-btn')?.addEventListener('click', gitPush);

// ─── 全局文件拖放上传 ─────────────────────────────────────────────
// 拖放文件到任意窗口，自动上传到当前工作区；若文件管理器已打开且拖放落在其内，则上传到当前文件夹
// 复用 file-manager.js 的 uploadFiles / fmCurrentPath / loadFileManagerFiles

const dropOverlay = document.createElement('div');
dropOverlay.className = 'drop-overlay';
const dropOverlayInner = document.createElement('div');
dropOverlayInner.className = 'drop-overlay-inner';
dropOverlay.appendChild(dropOverlayInner);
document.body.appendChild(dropOverlay);
let dragDepth = 0;

function isFileDrag(e) {
    return e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
}

// 拖放目标是否落在已打开的文件管理器内
function dropInFileManager(e) {
    const fmModal = document.getElementById('file-manager-modal');
    return fmModal && fmModal.style.display !== 'none' && e.target.closest('#file-manager-modal');
}

function showDropOverlay(e) {
    dropOverlayInner.textContent = dropInFileManager(e)
        ? '松开以上传到当前文件夹'
        : '松开以上传到工作区';
    dropOverlay.classList.add('show');
}

function hideDropOverlay() {
    dragDepth = 0;
    dropOverlay.classList.remove('show');
}

document.addEventListener('dragenter', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    dragDepth++;
    showDropOverlay(e);
});

document.addEventListener('dragover', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
});

document.addEventListener('dragleave', (e) => {
    if (!isFileDrag(e)) return;
    // 指针离开窗口时直接重置，避免覆盖层卡住
    if (!document.body.contains(e.relatedTarget)) {
        hideDropOverlay();
        return;
    }
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) hideDropOverlay();
});

document.addEventListener('drop', async (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    hideDropOverlay();

    const files = Array.from(e.dataTransfer.files || []);
    if (files.length === 0) return;

    // 目录拖放（webkitGetAsEntry 仅 Chromium 支持），提示打包后上传
    const hasDir = Array.from(e.dataTransfer.items || []).some((it) => {
        if (it.kind !== 'file' || !it.webkitGetAsEntry) return false;
        const entry = it.webkitGetAsEntry();
        return !!entry && entry.isDirectory;
    });
    if (hasDir) {
        showToast('不支持拖放文件夹，请打包为 zip 后上传');
        return;
    }

    if (!currentWorkspace) {
        showToast('请先选择工作区');
        return;
    }

    const inFileManager = dropInFileManager(e);
    // 主界面拖放统一落到 uploads/（与 QQ 入站附件同目录，便于两边一致）；
    // 文件管理器面板内的拖放仍落在当前浏览目录。
    const path = inFileManager ? fmCurrentPath : 'uploads';
    try {
        const result = await uploadFiles(path, files);
        const uploaded = result.uploaded || [];
        const errList = result.errors || [];
        if (uploaded.length > 0) {
            // 上传成功后，将带路径的文件名插入对话输入框
            const lines = uploaded
                .map((f) => (path ? `${path}/${f.name}` : f.name))
                .join('\n');
            const input = document.getElementById('chat-input');
            if (input) {
                if (input.value && !input.value.endsWith('\n')) input.value += '\n';
                input.value += lines;
                input.focus();
            }
            showToast(`✓ 已上传 ${uploaded.length} 个文件，文件名已插入输入框`);
        }
        if (errList.length > 0) {
            const reasons = errList.map((err) => `${err.name}: ${err.error}`).join('；');
            showToast(`✗ ${errList.length} 个文件上传失败：${reasons}`, 4000);
        }
        if (inFileManager && uploaded.length > 0) {
            await loadFileManagerFiles(fmCurrentPath);
        }
    } catch (error) {
        showToast('✗ 上传失败: ' + (error.message || error), 3000);
    }
});

// ─── 教材下载 ───────────────────────────────────────────────────────────────

const textbookState = {
    initialized: false,
    loading: false,
    workspaceUuid: null,
    books: [],
    filtered: [],
    taskId: null,
    pollTimer: null,
};

function textbookFormatSize(bytes) {
    const value = Number(bytes || 0);
    if (value >= 1024 * 1024 * 1024) return (value / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    if (value >= 1024 * 1024) return (value / 1024 / 1024).toFixed(1) + ' MB';
    if (value >= 1024) return (value / 1024).toFixed(1) + ' KB';
    return value + ' B';
}

function textbookSetStatus(message, type = '') {
    const el = document.getElementById('textbook-task-status');
    if (!el) return;
    el.textContent = message;
    el.className = 'settings-status' + (type ? ' ' + type : '');
}

function textbookSetButtons(taskRunning = false, hasFailures = false) {
    document.getElementById('textbook-download-btn').disabled = textbookState.loading || taskRunning;
    document.getElementById('textbook-refresh-btn').disabled = textbookState.loading || taskRunning;
    document.getElementById('textbook-cancel-btn').style.display = taskRunning ? '' : 'none';
    document.getElementById('textbook-retry-btn').style.display = (!taskRunning && hasFailures) ? '' : 'none';
}

function initTextbookPanel() {
    if (!currentWorkspace) return;
    const isSystem = currentWorkspace.system || currentWorkspace.uuid === 'system';
    if (isSystem) {
        stopTextbookPolling(null);
        textbookState.taskId = null;
        textbookState.books = [];
        textbookState.filtered = [];
        textbookSetStatus('系统工作区不支持教材下载', 'error');
        document.getElementById('textbook-list').innerHTML =
            '<div class="textbook-empty">系统工作区不支持教材下载。</div>';
        return;
    }

    // 切换工作区后清空上一个工作区的任务与选择，避免跨工作区误操作。
    if (textbookState.workspaceUuid !== currentWorkspace.uuid) {
        stopTextbookPolling(null);
        textbookState.workspaceUuid = currentWorkspace.uuid;
        textbookState.taskId = null;
        textbookState.books = [];
        textbookState.filtered = [];
        textbookState.loading = false;
        document.getElementById('textbook-progress').style.display = 'none';
        document.getElementById('textbook-progress').value = 0;
    }

    if (!textbookState.initialized) {
        const stage = document.getElementById('textbook-stage');
        const system = document.getElementById('textbook-system');
        const subject = document.getElementById('textbook-subject');
        const edition = document.getElementById('textbook-edition');
        const grade = document.getElementById('textbook-grade');
        const semester = document.getElementById('textbook-semester');
        const highScope = document.getElementById('textbook-high-scope');
        stage.addEventListener('change', () => {
            syncTextbookStageControls();
            loadTextbookSubjects();
        });
        system.addEventListener('change', loadTextbookSubjects);
        subject.addEventListener('change', () => {
            subject.dataset.value = subject.value;
            loadTextbookBooks();
        });
        edition.addEventListener('change', loadTextbookBooks);
        grade.addEventListener('change', loadTextbookBooks);
        semester.addEventListener('change', renderTextbookBooks);
        highScope.addEventListener('change', loadTextbookBooks);
        document.getElementById('textbook-refresh-btn').addEventListener('click', loadTextbookBooks);
        document.getElementById('textbook-select-all-btn').addEventListener('click', () => {
            document.querySelectorAll('#textbook-list input[type="checkbox"]').forEach(box => {
                box.checked = true;
            });
            updateTextbookSummary();
        });
        document.getElementById('textbook-clear-btn').addEventListener('click', () => {
            document.querySelectorAll('#textbook-list input[type="checkbox"]').forEach(box => {
                box.checked = false;
            });
            updateTextbookSummary();
        });
        document.getElementById('textbook-download-btn').addEventListener('click', downloadSelectedTextbooks);
        document.getElementById('textbook-cancel-btn').addEventListener('click', cancelTextbookTask);
        document.getElementById('textbook-retry-btn').addEventListener('click', retryFailedTextbooks);
        textbookState.initialized = true;
    }

    syncTextbookStageControls();
    loadTextbookSubjects();
}

function syncTextbookStageControls() {
    const stage = document.getElementById('textbook-stage').value;
    const system = document.getElementById('textbook-system');
    const highScopeGroup = document.getElementById('textbook-high-scope-group');
    const isHighSchool = stage === '高中';
    system.disabled = isHighSchool;
    if (isHighSchool) system.value = '六三制';
    highScopeGroup.style.display = isHighSchool ? '' : 'none';
}

async function loadTextbookSubjects() {
    if (!currentWorkspace || textbookState.loading) return;
    const subjectSelect = document.getElementById('textbook-subject');
    const stage = document.getElementById('textbook-stage').value;
    const system = document.getElementById('textbook-system').value;
    textbookState.loading = true;
    textbookSetButtons();
    syncTextbookStageControls();
    subjectSelect.innerHTML = '<option value="">加载中...</option>';
    textbookState.books = [];
    textbookState.filtered = [];
    document.getElementById('textbook-list').innerHTML =
        '<div class="textbook-empty">选择科目后自动列出可下载课本。</div>';
    updateTextbookSummary();
    textbookSetStatus('加载科目...');
    try {
        const params = new URLSearchParams({ stage, system, book_types: 'textbook' });
        const response = await fetch('/api/textbooks/subjects?' + params.toString());
        if (!response.ok) throw new Error((await response.json()).detail || '加载科目失败');
        const data = await response.json();
        const oldSubject = subjectSelect.dataset.value || '';
        subjectSelect.innerHTML = '<option value="">选择科目...</option>';
        (data.subjects || []).forEach(item => {
            const option = document.createElement('option');
            option.value = item.subject;
            option.textContent = item.subject + '（' + item.count + ' 本，' + textbookFormatSize(item.total_size) + '）';
            subjectSelect.appendChild(option);
        });
        if (oldSubject && (data.subjects || []).some(item => item.subject === oldSubject)) {
            subjectSelect.value = oldSubject;
            subjectSelect.dataset.value = oldSubject;
        } else {
            subjectSelect.dataset.value = '';
        }
        if (!(data.subjects || []).length) {
            subjectSelect.innerHTML = '<option value="">暂无科目</option>';
        }
        if (subjectSelect.value) {
            await loadTextbookBooks();
            return;
        }
        textbookSetStatus('请选择科目。');
    } catch (error) {
        subjectSelect.innerHTML = '<option value="">加载失败</option>';
        textbookSetStatus('✗ ' + error.message, 'error');
    } finally {
        textbookState.loading = false;
        textbookSetButtons();
    }
}

async function loadTextbookBooks() {
    if (!currentWorkspace) return;
    // 由 loadTextbookSubjects 恢复上一次科目时会嵌套调用；避免在这里被外层 loading 拦截。
    const stage = document.getElementById('textbook-stage').value;
    const system = document.getElementById('textbook-system').value;
    const subject = document.getElementById('textbook-subject').value;
    const edition = document.getElementById('textbook-edition').value;
    const highScope = document.getElementById('textbook-high-scope').value;
    const listEl = document.getElementById('textbook-list');

    if (!subject) {
        textbookState.books = [];
        textbookState.filtered = [];
        listEl.innerHTML = '<div class="textbook-empty">选择科目后自动列出可下载课本。</div>';
        updateTextbookSummary();
        return;
    }

    textbookState.loading = true;
    textbookSetButtons();
    listEl.innerHTML = '<div class="textbook-empty">加载教材...</div>';
    textbookSetStatus('加载教材清单...');
    try {
        const params = new URLSearchParams({
            stage: stage,
            system: system,
            subject: subject,
            edition: edition,
            edition_grade: document.getElementById('textbook-grade').value,
            high_scope: highScope,
            book_types: 'textbook',
        });
        const response = await fetch('/api/textbooks/options?' + params.toString());
        if (!response.ok) throw new Error((await response.json()).detail || '加载教材失败');
        const data = await response.json();
        textbookState.books = data.books || [];

        const editionSelect = document.getElementById('textbook-edition');
        const oldEdition = editionSelect.value;
        editionSelect.innerHTML = '<option value="">全部版本</option>';
        (data.editions || []).forEach(item => {
            const option = document.createElement('option');
            option.value = item.edition_raw;
            option.textContent = item.edition_raw + '（' + item.count + ' 本，' + textbookFormatSize(item.total_size) + '）';
            editionSelect.appendChild(option);
        });
        editionSelect.value = (data.editions || []).some(item => item.edition_raw === oldEdition)
            ? oldEdition : '';

        const grades = [...new Set(textbookState.books.map(item => item.grade).filter(Boolean))].sort();
        const semesters = [...new Set(textbookState.books.map(item => item.semester).filter(Boolean))].sort();
        populateTextbookSelect('textbook-grade', grades, '全部年级');
        populateTextbookSelect('textbook-semester', semesters, '全部学期');
        renderTextbookBooks();
        const warning = (data.warnings || []).join('；');
        textbookSetStatus(warning ? warning : '已加载 ' + textbookState.books.length + ' 本教材，默认全选。');
    } catch (error) {
        textbookState.books = [];
        textbookState.filtered = [];
        listEl.innerHTML = '<div class="textbook-empty">加载失败</div>';
        updateTextbookSummary();
        textbookSetStatus('✗ ' + error.message, 'error');
    } finally {
        textbookState.loading = false;
        textbookSetButtons();
    }
}

function populateTextbookSelect(id, values, emptyLabel) {
    const select = document.getElementById(id);
    const old = select.value;
    select.innerHTML = '<option value="">' + emptyLabel + '</option>';
    values.forEach(value => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = value;
        select.appendChild(option);
    });
    select.value = values.includes(old) ? old : '';
}

function renderTextbookBooks() {
    const grade = document.getElementById('textbook-grade').value;
    const semester = document.getElementById('textbook-semester').value;
    textbookState.filtered = textbookState.books.filter(book =>
        (!grade || book.grade === grade) && (!semester || book.semester === semester)
    );
    const listEl = document.getElementById('textbook-list');
    if (!textbookState.filtered.length) {
        listEl.innerHTML = '<div class="textbook-empty">没有匹配的教材。</div>';
        updateTextbookSummary();
        return;
    }
    listEl.innerHTML = '';
    textbookState.filtered.forEach(book => {
        const label = document.createElement('label');
        label.className = 'textbook-item';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = book.book_key;
        checkbox.checked = true;
        checkbox.addEventListener('change', updateTextbookSummary);
        const text = document.createElement('span');
        text.className = 'textbook-item-text';
        text.textContent = [
            book.title,
            book.edition_raw,
            book.grade || '',
            book.semester || book.high_type || '',
            book.split ? '分片 ' + book.chunk_count : '',
            textbookFormatSize(book.total_size),
        ].filter(Boolean).join(' · ');
        label.append(checkbox, text);
        listEl.appendChild(label);
    });
    updateTextbookSummary();
}

function updateTextbookSummary() {
    const selected = selectedTextbookBooks();
    const totalSize = selected.reduce((sum, book) => sum + Number(book.total_size || 0), 0);
    const splitCount = selected.filter(book => book.split).length;
    document.getElementById('textbook-summary').textContent =
        '已选 ' + selected.length + ' 本，共 ' + textbookFormatSize(totalSize) +
        '；其中 ' + splitCount + ' 本需分片合并。';
    textbookSetButtons(Boolean(textbookState.taskId && isTextbookTaskRunning()));
}

function selectedTextbookBooks() {
    const selected = new Set(Array.from(
        document.querySelectorAll('#textbook-list input[type="checkbox"]:checked')
    ).map(box => box.value));
    return textbookState.filtered.filter(book => selected.has(book.book_key));
}

function isTextbookTaskRunning(status) {
    return status === 'running';
}

async function downloadSelectedTextbooks() {
    if (!currentWorkspace) return;
    const books = selectedTextbookBooks();
    if (!books.length) {
        textbookSetStatus('请先选择要下载的教材。', 'error');
        return;
    }
    const downloadBtn = document.getElementById('textbook-download-btn');
    downloadBtn.disabled = true;
    textbookSetStatus('创建下载任务...');
    try {
        const response = await fetch('/api/textbooks/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                workspace_uuid: currentWorkspace.uuid,
                book_keys: books.map(book => book.book_key),
                overwrite: false,
            }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '创建下载任务失败');
        startTextbookPolling(data.task_id);
    } catch (error) {
        textbookSetStatus('✗ ' + error.message, 'error');
        downloadBtn.disabled = false;
    }
}

function startTextbookPolling(taskId) {
    textbookState.taskId = taskId;
    textbookSetButtons(true);
    const progress = document.getElementById('textbook-progress');
    progress.style.display = '';
    progress.value = 0;
    if (textbookState.pollTimer) clearInterval(textbookState.pollTimer);
    textbookState.pollTimer = setInterval(pollTextbookTask, 1000);
    pollTextbookTask();
}

async function pollTextbookTask() {
    if (!textbookState.taskId) return;
    try {
        const response = await fetch('/api/textbooks/download/' + textbookState.taskId);
        if (response.status === 404) {
            stopTextbookPolling('任务不存在（服务可能已重启）', 'error');
            return;
        }
        if (!response.ok) throw new Error((await response.json()).detail || '查询任务失败');
        const task = await response.json();
        const percent = task.total_bytes > 0
            ? Math.min(100, Math.round(task.downloaded_bytes / task.total_bytes * 100))
            : 100;
        document.getElementById('textbook-progress').value = percent;
        const failed = (task.books || []).filter(book => book.status === 'failed');
        const skipped = (task.books || []).filter(book => book.status === 'skipped');
        const done = (task.books || []).filter(book => book.status === 'done');
        textbookSetStatus(
            task.status + '：完成 ' + done.length + '，跳过 ' + skipped.length +
            '，失败 ' + failed.length + '；' + textbookFormatSize(task.downloaded_bytes) +
            ' / ' + textbookFormatSize(task.total_bytes) + '（' + percent + '%）' +
            (failed.length ? '；首个错误：' + failed[0].error : '')
        );
        if (!isTextbookTaskRunning(task.status)) {
            stopTextbookPolling(null, task.status === 'done' ? 'success' : 'error');
            textbookSetButtons(false, failed.length > 0);
        } else {
            textbookSetButtons(true);
        }
    } catch (error) {
        textbookSetStatus('查询下载进度失败: ' + error.message, 'error');
    }
}

function stopTextbookPolling(message, type = '') {
    if (textbookState.pollTimer) {
        clearInterval(textbookState.pollTimer);
        textbookState.pollTimer = null;
    }
    if (message !== null) textbookSetStatus(message, type);
}

async function retryFailedTextbooks() {
    if (!textbookState.taskId) return;
    try {
        const response = await fetch('/api/textbooks/download/' + textbookState.taskId + '/retry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '重试失败');
        startTextbookPolling(data.task_id);
    } catch (error) {
        textbookSetStatus('✗ ' + error.message, 'error');
    }
}

async function cancelTextbookTask() {
    if (!textbookState.taskId) return;
    try {
        const response = await fetch('/api/textbooks/download/' + textbookState.taskId + '/cancel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '取消失败');
        textbookState.taskId = data.task_id;
        pollTextbookTask();
    } catch (error) {
        textbookSetStatus('✗ ' + error.message, 'error');
    }
}
