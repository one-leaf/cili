// 会话来源通道的展示名（core/channels.py 的 id → 中文标签）
const SESSION_CHANNEL_LABELS = {
    web: '网页', qq: 'QQ', wechat: '微信', cli: '终端', email: '邮件', cron: '定时',
};

// 当前会话是否是「未绑定的新建工作流」会话（metadata.workflow_panel.workflow_id 为空）
function onBlankWorkflowSession() {
    const panel = currentSession && currentSession.metadata && currentSession.metadata.workflow_panel;
    return !!(panel && !panel.workflow_id);
}

// 当前会话是否属于工作流（面板会话 / 运行结果会话）
function onWorkflowRelatedSession() {
    const metadata = (currentSession && currentSession.metadata) || {};
    return !!(metadata.workflow_panel || metadata.run_results);
}

// 切换类型后让右侧聊天跟过来：
// - 工作流模式且没打开工作流 → 打开/复用「新建工作流」会话，展示工作流示例
// - 会话模式且停在会话工作流会话上 → 回到最近的普通会话
async function syncChatToMode() {
    if (!currentWorkspace) return;
    if (sidebarFilter === 'workflow') {
        if (!window.WorkflowPane || typeof WorkflowPane.getCurrentWorkflowId !== 'function') return;
        if (WorkflowPane.getCurrentWorkflowId()) return;
        // 青少年模式禁止工作流时不自动创建面板会话，避免切页就报错
        const teenOn = window.teenStatus && window.teenStatus.enabled;
        const teenUnlocked = window.teenStatus && window.teenStatus.unlocked;
        if (teenOn && !teenUnlocked
                && ((window.teenStatus.policy || {}).workflows || 'deny') === 'deny') {
            return;
        }
        // 回到上次打开的工作流（还在的话），否则打开/复用「新建工作流」会话
        const entries = typeof WorkflowPane.getSidebarEntries === 'function'
            ? WorkflowPane.getSidebarEntries() : [];
        if (_lastWorkflowModeId && entries.some(item => item.id === _lastWorkflowModeId)) {
            await WorkflowPane.openWorkflowById(_lastWorkflowModeId);
            return;
        }
        if (onBlankWorkflowSession()) {
            if (chatMessages.querySelector('.welcome-message')) {
                chatMessages.innerHTML = newSessionWelcomeHtml();
            }
            return;
        }
        await WorkflowPane.newWorkflow();
        return;
    }
    if (window.WorkflowPane && typeof WorkflowPane.getCurrentWorkflowId === 'function') {
        const workflowId = WorkflowPane.getCurrentWorkflowId();
        if (workflowId) _lastWorkflowModeId = workflowId;
    }
    if (currentSession && onWorkflowRelatedSession() && _lastSessionModeSessionId
            && _lastSessionModeSessionId !== currentSession.session_id) {
        await loadSession(_lastSessionModeSessionId);
    }
}

// ── 会话分区折叠（VS Code 风格） ──
const SESSIONS_COLLAPSED_KEY = 'cili_sessions_collapsed';

function applySessionsCollapsed(collapsed) {
    const section = document.querySelector('.sessions-section');
    const toggle = document.getElementById('sessions-toggle-btn');
    if (!section) return;
    section.classList.toggle('collapsed', collapsed);
    section.style.flex = collapsed ? '0 0 auto' : '';
    if (toggle) toggle.setAttribute('aria-expanded', String(!collapsed));
    if (window.FileTree && typeof window.FileTree.relayout === 'function') {
        window.FileTree.relayout();
    }
}

function toggleSessionsSection() {
    const section = document.querySelector('.sessions-section');
    if (!section) return;
    const collapsed = !section.classList.contains('collapsed');
    try {
        localStorage.setItem(SESSIONS_COLLAPSED_KEY, collapsed ? '1' : '0');
    } catch (e) { /* ignore */ }
    applySessionsCollapsed(collapsed);
}

function initSessionsSection() {
    const toggle = document.getElementById('sessions-toggle-btn');
    if (!toggle) return;
    toggle.addEventListener('click', toggleSessionsSection);
    let collapsed = false;
    try {
        collapsed = localStorage.getItem(SESSIONS_COLLAPSED_KEY) === '1';
    } catch (e) { /* ignore */ }
    applySessionsCollapsed(collapsed);
}

// 侧栏视图切换（会话 / 工作流 / …）：视图名即分区标题，点击弹出菜单
const SIDEBAR_FILTER_KEY = 'cili_sidebar_filter';

const SIDEBAR_VIEWS = [
    {
        id: 'session',
        label: '会话',
        icon: '<svg class="view-menu-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    },
    {
        id: 'workflow',
        label: '工作流',
        icon: '<svg class="view-menu-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>',
    },
    // 小程序上线后在此追加一行，列表/菜单逻辑无需改动
    // { id: 'miniapp', label: '小程序', icon: '...' },
];

const CHECK_ICON = '<svg class="view-menu-check" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';

let sidebarViewMenu = null;

function currentSidebarView() {
    return SIDEBAR_VIEWS.find(view => view.id === sidebarFilter) || SIDEBAR_VIEWS[0];
}

function renderSidebarViewTrigger() {
    const view = currentSidebarView();
    const label = document.getElementById('sidebar-view-label');
    if (label) label.textContent = view.label;
    if (newSessionBtn) {
        const newLabel = '新建' + view.label;
        newSessionBtn.title = newLabel;
        newSessionBtn.setAttribute('aria-label', newLabel);
    }
}

function sidebarViewCount(viewId) {
    const matchesHidden = item => !!item.hidden === showHiddenSessions;
    if (viewId === 'session') return sessions.filter(matchesHidden).length;
    if (viewId === 'workflow' && window.WorkflowPane
            && typeof WorkflowPane.getSidebarEntries === 'function') {
        return WorkflowPane.getSidebarEntries().filter(matchesHidden).length;
    }
    return 0;
}

async function setSidebarView(viewId) {
    if (viewId === sidebarFilter || !SIDEBAR_VIEWS.some(view => view.id === viewId)) return;
    sidebarFilter = viewId;
    // 切换后不可见条目的选中状态要丢掉，避免批量操作误伤
    selectedSessions.clear();
    selectedWorkflows.clear();
    try { localStorage.setItem(SIDEBAR_FILTER_KEY, viewId); } catch (e) { /* ignore */ }
    renderSidebarViewTrigger();
    updateFooterToolbar();
    renderSidebarList();
    // 当前显示的是空状态欢迎块时，示例先跟随模式切换
    if (chatMessages.querySelector('.welcome-message')) {
        chatMessages.innerHTML = newSessionWelcomeHtml();
    }
    await syncChatToMode();
}

function closeSidebarViewMenu(restoreFocus = false) {
    if (!sidebarViewMenu) return;
    sidebarViewMenu.remove();
    sidebarViewMenu = null;
    const trigger = document.getElementById('sidebar-view-trigger');
    if (trigger) {
        trigger.setAttribute('aria-expanded', 'false');
        if (restoreFocus) trigger.focus();
    }
}

function openSidebarViewMenu() {
    const trigger = document.getElementById('sidebar-view-trigger');
    if (!trigger || sidebarViewMenu) return;
    const menu = document.createElement('div');
    menu.className = 'sidebar-view-menu';
    menu.setAttribute('role', 'menu');
    SIDEBAR_VIEWS.forEach(view => {
        const active = view.id === sidebarFilter;
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'sidebar-view-menu-item' + (active ? ' active' : '');
        item.setAttribute('role', 'menuitemradio');
        item.setAttribute('aria-checked', String(active));
        item.innerHTML = `${view.icon}<span>${view.label}</span>`
            + `<span class="view-menu-count">${sidebarViewCount(view.id)}</span>`
            + (active ? CHECK_ICON : '');
        item.addEventListener('click', () => {
            closeSidebarViewMenu();
            setSidebarView(view.id);
        });
        menu.appendChild(item);
    });
    menu.addEventListener('keydown', (e) => {
        if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
        e.preventDefault();
        const items = Array.from(menu.querySelectorAll('.sidebar-view-menu-item'));
        const current = items.indexOf(document.activeElement);
        const step = e.key === 'ArrowDown' ? 1 : -1;
        items[(current + step + items.length) % items.length].focus();
    });
    openAnchoredMenu(menu, trigger);
    sidebarViewMenu = menu;
    trigger.setAttribute('aria-expanded', 'true');
    const activeItem = menu.querySelector('.sidebar-view-menu-item.active');
    if (activeItem) activeItem.focus();
}

function initSidebarViewMenu() {
    const trigger = document.getElementById('sidebar-view-trigger');
    if (!trigger) return;
    try {
        const saved = localStorage.getItem(SIDEBAR_FILTER_KEY);
        if (saved === 'session' || saved === 'workflow') sidebarFilter = saved;
    } catch (e) { /* ignore */ }
    renderSidebarViewTrigger();

    trigger.addEventListener('click', (e) => {
        e.stopPropagation();
        if (sidebarViewMenu) closeSidebarViewMenu();
        else openSidebarViewMenu();
    });
    trigger.addEventListener('keydown', (e) => {
        if (e.key !== 'ArrowDown' || sidebarViewMenu) return;
        e.preventDefault();
        openSidebarViewMenu();
    });

    document.addEventListener('click', (e) => {
        if (!sidebarViewMenu) return;
        if (sidebarViewMenu.contains(e.target) || trigger.contains(e.target)) return;
        closeSidebarViewMenu();
    });

    document.addEventListener('keydown', (e) => {
        if (sidebarViewMenu && e.key === 'Escape') {
            e.preventDefault();
            closeSidebarViewMenu(true);
            return;
        }
        // 视图快捷键 Alt+1/2/3（浏览器保留 Ctrl+1~8 切标签页，网页拦不住）
        if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
        const view = SIDEBAR_VIEWS[Number(e.key) - 1];
        if (!view) return;
        e.preventDefault();
        closeSidebarViewMenu();
        setSidebarView(view.id);
    });

    // 滚动或窗口尺寸变化时关闭菜单，避免浮层错位
    window.addEventListener('scroll', () => closeSidebarViewMenu(), { capture: true, passive: true });
    window.addEventListener('resize', () => closeSidebarViewMenu());

    const importInput = document.getElementById('session-import-input');
    if (importInput) {
        importInput.addEventListener('change', async (event) => {
            const file = event.target.files && event.target.files[0];
            event.target.value = '';
            if (file) await handleImportFile(file);
        });
    }
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
                <button class="theme-toggle" id="settings-btn" title="系统设置" onclick="openSettings()">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                        <line x1="4" y1="6" x2="20" y2="6"></line>
                        <circle cx="9" cy="6" r="2"></circle>
                        <line x1="4" y1="12" x2="20" y2="12"></line>
                        <circle cx="15" cy="12" r="2"></circle>
                        <line x1="4" y1="18" x2="20" y2="18"></line>
                        <circle cx="7" cy="18" r="2"></circle>
                    </svg>
                </button>
                <button class="theme-toggle" id="theme-toggle-btn" title="切换亮色/暗色主题">
                    ${getThemeIcon()}
                </button>
                <span class="footer-text" title="${data.app_name} by ${data.author} · ${data.version}">
                    <span class="footer-dev">${data.app_name}</span>
                    <span class="footer-sep">·</span>
                    <span class="footer-version">${data.version}</span>
                </span>
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

// 持久化主题偏好：cookie 跨端口保持（服务端口顺延后 localStorage 会因换源丢失），localStorage 兜底
function persistTheme(theme) {
    try { localStorage.setItem('cili-theme', theme); } catch (e) { /* 隐私模式等场景忽略 */ }
    document.cookie = `cili-theme=${theme};path=/;max-age=31536000;samesite=lax`;
}

// 切换主题
function toggleTheme() {
    const html = document.documentElement;
    const isDark = html.getAttribute('data-theme') === 'dark';
    if (isDark) {
        html.removeAttribute('data-theme');
        persistTheme('light');
    } else {
        html.setAttribute('data-theme', 'dark');
        persistTheme('dark');
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

function setupEventListeners() {
    workspaceSelect.addEventListener('change', handleWorkspaceChange);
    workspaceSettingsBtn.addEventListener('click', openWorkspaceSettings);
    newWorkspaceBtn.addEventListener('click', handleNewWorkspace);
    newSessionBtn.addEventListener('click', handleNewButton);
    sessionMenuBtn.addEventListener('click', toggleSessionPanelMenu);
    document.getElementById('sidebar-toggle-btn').addEventListener('click', toggleSidebar);
    document.getElementById('sidebar-open-btn').addEventListener('click', toggleSidebar);
    // 右下角单按钮：红=停 master，黄=停后台，发送=发送/排队
    composerBtn.addEventListener('click', () => {
        const state = composerBtn.dataset.state;
        if (state === 'stop') {
            stopAgent();
        } else if (state === 'stop-bg') {
            stopBackground();
        } else {
            sendMessage();
        }
    });
    modelSelect.addEventListener('change', onModelSelectChange);
    if (suggestBtn) suggestBtn.addEventListener('click', handleSuggest);
    // 新会话空状态的示例：点击写入输入框（不发送）
    chatMessages.addEventListener('click', (e) => {
        const btn = e.target.closest('.welcome-example');
        if (!btn) return;
        chatInput.value = btn.textContent;
        chatInput.focus();
        updateComposerButtons();
    });
    updateComposerButtons();
    if (queueResumeBtn) queueResumeBtn.addEventListener('click', () => resumeQueue());
    if (queueClearBtn) queueClearBtn.addEventListener('click', () => clearQueue());
    chatInput.addEventListener('input', () => {
        // 开始输入即收起推荐并作废在途请求（设计 §2.3）
        hideSuggestBar({ invalidate: true });
        updateComposerButtons();
    });
    chatInput.addEventListener('keydown', (e) => {
        // isComposing：中文输入法组合期间的 Enter 不提交（避免截断误发）
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            sendMessage();
        }
    });
    // Ctrl+V：只上传到 uploads；后续在文件树中预览选择（与预览流程完全一致）
    chatInput.addEventListener('paste', async (e) => {
        const cd = e.clipboardData;
        if (!cd) return;
        const files = [];
        const seen = new Set();
        const addFile = (f) => {
            if (!f) return;
            const key = `${f.name}|${f.size}|${f.type}`;
            if (seen.has(key)) return;
            seen.add(key);
            files.push(f);
        };
        if (cd.files) {
            for (const f of cd.files) addFile(f);
        }
        if (cd.items) {
            for (const item of cd.items) {
                if (item.kind === 'file') addFile(item.getAsFile());
            }
        }
        if (files.length === 0) return;
        e.preventDefault();
        if (!currentWorkspace) {
            showToast('请先选择工作区');
            return;
        }
        const EXT_BY_MIME = {
            'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp',
            'image/bmp': '.bmp', 'image/svg+xml': '.svg',
            'application/pdf': '.pdf',
            'application/msword': '.doc',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
            'application/vnd.ms-excel': '.xls',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
            'application/vnd.ms-powerpoint': '.ppt',
            'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
            'text/plain': '.txt', 'text/markdown': '.md', 'text/html': '.html',
            'text/csv': '.csv', 'application/json': '.json', 'application/zip': '.zip',
        };
        // 截图粘贴等无名文件补默认名，再交给共用上传流程
        const stamp = Date.now();
        const uploadables = files.map((file, i) => {
            if (file.name && file.name.trim()) return file;
            const ext = EXT_BY_MIME[file.type] || '';
            return new File([file], `paste-${stamp}-${i}${ext}`, {
                type: file.type || 'application/octet-stream',
            });
        });
        try {
            const { errors } = await uploadFilesAsAttachments(uploadables);
            if (errors.length) {
                showToast('上传失败：' + (errors[0].error || '未知错误'));
            }
        } catch (err) {
            showToast('上传失败：' + (err.message || '未知错误'));
        }
    });

    // Global settings 按钮由 loadFooter() 注入（内联 onclick）
    document.getElementById('settings-save-btn').addEventListener('click', saveSettings);

    // Workspace settings
    document.getElementById('workspace-settings-save-btn').addEventListener('click', saveWorkspaceSettings);
    document.getElementById('workspace-delete-btn').addEventListener('click', deleteWorkspaceConfig);

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

    // 全局设置页签（限定 #settings-modal；工作区弹窗的同名类由自身 wiring 处理）
    document.querySelectorAll('#settings-modal .settings-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('#settings-modal .settings-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('#settings-modal .tab-content').forEach(c => c.classList.remove('active'));
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

        // 先渲染普通工作区；按名称排序（同一孩子/同名前缀的挨在一起）
        sortWorkspacesByName(data.workspaces.filter(ws => !ws.system)).forEach(ws => {
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
        updateComposerButtons();
        refreshModelSelect();
        if (window.ContextGauge) ContextGauge.refresh();  // 无会话：隐藏指示器
        closeEventSource();  // 无工作区：断开事件流
        clearPosition();
        workspaceSettingsBtn.disabled = true;
        document.getElementById('memory-btn').disabled = true;
        sessionsList.innerHTML = '<div class="empty-state">选择一个工作区</div>';
        chatMessages.innerHTML = '<div class="welcome-message"><h2>欢迎使用草履虫</h2><p>请先选择工作区，再新建会话开始</p></div>';
        updateWorkspacePath();
        if (window.FileTree) FileTree.setWorkspace(null);
        if (window.WorkflowPane) WorkflowPane.reset();
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
    if (window.AgentCardStore) {
        AgentCardStore.clear();
    }
    _renderedSessionKey = null;
    isMultiSelectMode = false;
    showHiddenSessions = false;
    selectedSessions.clear();
    selectedWorkflows.clear();
    _lastSessionModeSessionId = '';
    _lastWorkflowModeId = '';
    newSessionBtn.style.display = '';
    if (_footerOriginalHTML !== null) {
        document.getElementById('footer-content').innerHTML = _footerOriginalHTML;
        _footerOriginalHTML = null;
    }
    setFooterAboveFiles(false);
    savePosition({ workspace_uuid: selectedUuid });

    if (window.WorkflowPane) {
        try { WorkflowPane.init(selectedUuid); } catch (e) { console.warn('WorkflowPane init failed', e); }
    }

    if (!currentWorkspace) {
        updateWorkspacePath();
        if (window.FileTree) FileTree.setWorkspace(null);
        if (window.WorkflowPane) WorkflowPane.reset();
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
        // 没有会话时禁用输入框（有会话时运行中也保持可输入）
        sessionRunning = false;
        sessionBackground = { agents: 0, shells: 0, active: 0, items: [] };
        queuedItems = [];
        queuePaused = false;
        queueReason = '';
        updateComposerButtons();
        renderQueueBar();
        chatMessages.innerHTML = '<div class="welcome-message"><h2>欢迎使用草履虫</h2><p>点击 + 新建会话开始</p></div>';
    }

    // 工作区已就绪：让右侧聊天跟住当前类型（工作流模式直接展示工作流示例）
    await syncChatToMode();
}

// ─── 全局文件拖放上传 ─────────────────────────────────────────────
// 拖放文件到任意窗口，上传到当前工作区 uploads/，并加入输入区附件 chip
// （与「添加文件」/Ctrl+V 同一套 pendingAttachments 流程，随消息发送）

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

function showDropOverlay(e) {
    dropOverlayInner.textContent = '松开以上传并添加到输入区';
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

    // 主界面拖放统一落到 uploads/，与「添加文件」/Ctrl+V 同一流程：
    // 上传后进入输入区附件 chip，随消息发送（不再插入输入框文本）
    try {
        const { errors } = await uploadFilesAsAttachments(files);
        if (errors.length > 0) {
            const reasons = errors.map((err) => `${err.name}: ${err.error}`).join('；');
            showToast(`✗ ${errors.length} 个文件上传失败：${reasons}`, 4000);
        }
    } catch (error) {
        showToast('✗ 上传失败: ' + (error.message || error), 3000);
    }
});
