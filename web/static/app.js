// State
let currentWorkspace = null;  // {uuid, name, directory}
let currentSession = null;
let _renderedSessionKey = null;
let sessionLoadEpoch = 0;  // 会话加载代次：旧响应到达时丢弃
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
// 服务端会话状态与排队队列（与 isSending 解耦：isSending 只表示本页持有前台流）
let sessionRunning = false;      // 服务端该会话正在运行（/status、turn_complete）
let queuedItems = [];            // 服务端队列快照（queue_update / /status）
let queuePaused = false;         // 队列已暂停（停止/出错后需显式继续或清空）
let queueReason = '';            // 暂停原因：stopped / error
let isStopping = false;          // 已发 /stop，等待回合真正收尾
let isMultiSelectMode = false;
let showHiddenSessions = false;
let selectedSessions = new Set();
// 消息分页加载状态
let currentSessionLoadedOffset = 0;  // 已加载的消息数（从末尾算起）
let currentSessionHasMore = false;   // 是否还有更多历史消息
let isLoadingMore = false;           // 是否正在加载更多消息
let pendingAttachments = [];         // [{ path: "uploads/x", name, media_type }]
let modelSelectBusy = false;         // 模型列表加载/切换请求进行中
// 推荐输入（suggest）：当前气泡内容 / 生成中 / 请求序号（丢弃过期响应）
let suggestItems = [];
let suggestLoading = false;
let suggestRequestSeq = 0;


// DOM elements
const workspaceSelect = document.getElementById('workspace-select');
const workspaceSettingsBtn = document.getElementById('workspace-settings-btn');
const newWorkspaceBtn = document.getElementById('new-workspace-btn');
const sessionsList = document.getElementById('sessions-list');
const chatMessages = document.getElementById('chat-messages');
const chatInput = document.getElementById('chat-input');
const composerBtn = document.getElementById('composer-action-btn');
const suggestBtn = document.getElementById('suggest-btn');
const suggestBar = document.getElementById('suggest-bar');
const modelSelect = document.getElementById('model-select');
const queuedMessagesEl = document.getElementById('queued-messages');
const queuedListEl = document.getElementById('queued-list');
const queuedLabelEl = document.getElementById('queued-label');
const queueResumeBtn = document.getElementById('queue-resume-btn');
const queueClearBtn = document.getElementById('queue-clear-btn');
const newSessionBtn = document.getElementById('new-session-btn');
const sessionMenuBtn = document.getElementById('session-menu-btn');

// 解析聊天消息里的工作区文件链接，返回相对路径；非文件或跨工作区返回 null。
// 覆盖 renderMarkdown 产出的两种形态：
//   /api/workspaces/{uuid}/files/{rel}
//   /api/files/{rel}?workspace_uuid={uuid}
function _workspaceFileLinkPath(a) {
    const uuid = currentWorkspace?.uuid;
    if (!uuid) return null;
    let url;
    try {
        url = new URL(a.href, window.location.href);
    } catch {
        return null;
    }
    if (url.origin !== window.location.origin) return null;

    let raw = null;
    const wsMatch = url.pathname.match(/^\/api\/workspaces\/([^/]+)\/files\/(.*)$/);
    if (wsMatch) {
        if (wsMatch[1] !== uuid) return null;  // 跨工作区：不在此工作区预览面板里打开
        raw = wsMatch[2];
    } else {
        const filesMatch = url.pathname.match(/^\/api\/files\/(.*)$/);
        if (filesMatch && url.searchParams.get('workspace_uuid') === uuid) {
            raw = filesMatch[1];
        }
    }
    if (!raw) return null;
    try {
        return decodeURIComponent(raw);  // normalizePath 期望已解码的相对路径
    } catch {
        return null;
    }
}

// 聊天区域的链接：工作区文件链接点击后开右侧预览面板，其余在新标签页打开
// （防止离开聊天界面丢失状态）。带修饰键/非左键点击时尊重"新标签打开"的意图。
chatMessages.addEventListener('click', (e) => {
    const a = e.target.closest('a');
    if (!a) return;

    const plainClick = e.button === 0 && !e.ctrlKey && !e.metaKey && !e.shiftKey;
    if (plainClick && window.FileTree) {
        const filePath = _workspaceFileLinkPath(a);
        if (filePath) {
            e.preventDefault();
            window.FileTree.openPreview(filePath);
            return;
        }
    }

    e.preventDefault();
    window.open(a.href, '_blank', 'noopener,noreferrer');
});

// 输入面板拖拽调整大小
const resizeHandle = document.querySelector('.input-resize-handle');
const chatInputArea = document.querySelector('.chat-input-area');
const INPUT_MIN_HEIGHT = 80;
const INPUT_MAX_HEIGHT = 500;
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
    initSessionsSection();
    if (window.ContextGauge) ContextGauge.init();
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

// ── 输入区按钮状态（发送 / 排队发送 / 停止）──
// 单点决策：所有分支结束后都调它，不再在 fetch 分支里直接改按钮文案/配色。
function hasDraft() {
    return chatInput.value.trim().length > 0
        || pendingAttachments.length > 0
        || (typeof getActiveEditorContext === 'function' && getActiveEditorContext() !== null);
}

// 右下角单按钮图标（Claude Code 插件式）：发送=纸飞机，停止=方块
const COMPOSER_SEND_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"></path><path d="M22 2l-7 20-4-9-9-4 20-7z"></path></svg>';
const COMPOSER_STOP_ICON = '<svg width="10" height="10" viewBox="0 0 10 10" fill="currentColor"><rect x="1" y="1" width="8" height="8" rx="1.5"></rect></svg>';

function updateComposerButtons() {
    const hasSession = !!currentSession;
    const draft = hasDraft();
    // 有草稿=发送（运行中即排队发送）；运行中空草稿=停止；否则禁用
    let state = 'disabled';
    if (hasSession) {
        if (draft) {
            state = 'send';
        } else if (sessionRunning) {
            state = 'stop';
        }
    }
    composerBtn.dataset.state = state;
    composerBtn.innerHTML = state === 'stop' ? COMPOSER_STOP_ICON : COMPOSER_SEND_ICON;
    const title = state === 'stop' ? '停止当前回合'
        : (state === 'send' && sessionRunning ? '排队发送' : '发送');
    composerBtn.title = title;
    composerBtn.setAttribute('aria-label', title);
    composerBtn.disabled = state === 'disabled' || (state === 'stop' && isStopping);
    // 有会话时输入框常开（运行中也能输入）；无会话才禁用
    chatInput.disabled = !hasSession;
    // 模型下拉：无会话或运行中（含前台请求/后台回合）不可切换
    if (!hasSession && modelSelect.dataset.currentRef) {
        modelSelect.dataset.currentRef = '';
        modelSelect.innerHTML = '<option>模型</option>';
    }
    modelSelect.disabled = !hasSession || sessionRunning || isSending || modelSelectBusy;

    // 推荐输入：回合结束（含发送中）、无待答 ask_user/审批卡、未在生成时才可用
    if (suggestBtn) {
        const pendingAsk = typeof hasPendingAskUser === 'function' && hasPendingAskUser();
        suggestBtn.disabled = !hasSession || sessionRunning || isSending
            || suggestLoading || pendingAsk;
        if (!hasSession) {
            suggestBtn.title = '推荐输入';
        } else if (pendingAsk) {
            suggestBtn.title = '请先回答上方的问题';
        } else if (sessionRunning || isSending) {
            suggestBtn.title = '本轮完成后可用';
        } else {
            suggestBtn.title = '推荐输入';
        }
    }
}

// ── 推荐输入（suggest）────────────────────────────────────────────────
// 设计：workspace/default/cili-input-suggest-plan-2026-11-13.md
// 仅在回合结束、无待答 ask_user、未生成中可用；点击气泡只填充不发送。

function hideSuggestBar({ invalidate = false } = {}) {
    if (invalidate) suggestRequestSeq++;  // 丢弃在途/过期响应
    suggestItems = [];
    suggestLoading = false;
    if (suggestBtn) suggestBtn.classList.remove('suggest-loading');
    if (suggestBar) {
        suggestBar.hidden = true;
        suggestBar.innerHTML = '';
    }
    updateComposerButtons();
}

async function handleSuggest() {
    if (suggestLoading) return;
    if (suggestBar && !suggestBar.hidden) {
        hideSuggestBar();  // 已弹出则收起
        return;
    }
    await requestSuggestions(false);
}

async function requestSuggestions(refresh) {
    if (!currentWorkspace || !currentSession) return;
    const workspaceUuid = currentWorkspace.uuid;
    const sessionId = currentSession.session_id;
    const seq = ++suggestRequestSeq;
    suggestLoading = true;
    if (suggestBtn) suggestBtn.classList.add('suggest-loading');
    updateComposerButtons();

    try {
        const resp = await fetch(
            `/api/workspaces/${workspaceUuid}/sessions/${sessionId}/suggest`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    refresh: !!refresh,
                    exclude: refresh ? suggestItems.slice() : [],
                }),
            }
        );
        // 过期判定：序号变化、会话切换/新建、已开始新回合
        if (seq !== suggestRequestSeq) return;
        if (!currentSession || currentSession.session_id !== sessionId) return;

        if (!resp.ok) {
            let detail = '';
            try {
                detail = (await resp.json()).detail || '';
            } catch (e) { /* 非 JSON 错误体，用兜底文案 */ }
            showToast(detail || (resp.status === 503
                ? '暂时拿不到建议，请稍后再试'
                : '获取建议失败，请稍后再试'));
            return;
        }
        const data = await resp.json();
        const items = Array.isArray(data.suggestions) ? data.suggestions : [];
        if (!items.length) {
            showToast('暂时拿不到建议，请稍后再试');
            return;
        }
        renderSuggestBar(items);
    } catch (error) {
        if (seq === suggestRequestSeq) {
            console.error('获取推荐输入失败:', error);
            showToast('获取建议失败，请稍后再试');
        }
    } finally {
        if (seq === suggestRequestSeq) {
            suggestLoading = false;
            if (suggestBtn) suggestBtn.classList.remove('suggest-loading');
            updateComposerButtons();
        }
    }
}

function renderSuggestBar(items) {
    if (!suggestBar) return;
    suggestItems = items.slice();
    suggestBar.innerHTML = '';
    items.forEach((text) => {
        const pill = document.createElement('button');
        pill.type = 'button';
        pill.className = 'suggest-pill';
        pill.textContent = text;  // textContent：防 XSS
        pill.title = text;
        pill.addEventListener('click', () => fillSuggestion(text));
        suggestBar.appendChild(pill);
    });
    const refreshChip = document.createElement('button');
    refreshChip.type = 'button';
    refreshChip.className = 'suggest-pill suggest-refresh';
    refreshChip.textContent = '换一批';
    refreshChip.addEventListener('click', (e) => {
        e.stopPropagation();
        if (!suggestLoading) requestSuggestions(true);
    });
    suggestBar.appendChild(refreshChip);
    suggestBar.hidden = false;
}

function fillSuggestion(text) {
    const existing = chatInput.value.trim();
    chatInput.value = existing ? existing + ' ' + text : text;
    chatInput.focus();
    hideSuggestBar();
}

// 输入区模型下拉：默认显示当前会话模型，允许切换（会话级覆盖）
async function refreshModelSelect() {
    if (!currentWorkspace || !currentSession) {
        modelSelect.innerHTML = '<option>模型</option>';
        modelSelect.dataset.currentRef = '';
        modelSelect.disabled = true;
        return;
    }
    const workspaceUuid = currentWorkspace.uuid;
    const sessionId = currentSession.session_id;
    modelSelectBusy = true;
    updateComposerButtons();
    try {
        const resp = await fetch(`/api/workspaces/${workspaceUuid}/sessions/${sessionId}/model`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        if (!currentSession || currentSession.session_id !== sessionId) return;
        const opts = (data.available || []).map(m => {
            const ref = m.ref || '';
            const name = m.name || ref;
            return `<option value="${escapeHtml(ref)}" title="${escapeHtml(ref)}">${escapeHtml(name)}</option>`;
        }).join('');
        modelSelect.innerHTML = opts || '<option>（无可用模型）</option>';
        modelSelect.dataset.currentRef = data.current_ref || '';
        modelSelect.value = data.current_ref || '';
        modelSelect.title = data.current_ref || '切换模型';
    } catch (e) {
        if (currentSession && currentSession.session_id === sessionId) {
            console.warn('加载模型列表失败:', e);
            modelSelect.innerHTML = '<option>模型</option>';
            modelSelect.dataset.currentRef = '';
            modelSelect.title = '模型列表加载失败';
        }
    } finally {
        modelSelectBusy = false;
        updateComposerButtons();
    }
}

async function onModelSelectChange() {
    if (!currentWorkspace || !currentSession) return;
    const workspaceUuid = currentWorkspace.uuid;
    const sessionId = currentSession.session_id;
    const ref = modelSelect.value;
    if (!ref || ref === modelSelect.dataset.currentRef) return;
    const previousRef = modelSelect.dataset.currentRef || '';
    modelSelectBusy = true;
    updateComposerButtons();
    try {
        const resp = await fetch(`/api/workspaces/${workspaceUuid}/sessions/${sessionId}/model`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_ref: ref }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
        if (currentSession && currentSession.session_id === sessionId) {
            modelSelect.dataset.currentRef = data.current_ref || ref;
            modelSelect.value = data.current_ref || ref;
            modelSelect.title = data.current_ref || ref;
        }
        showToast(`已切换模型：${data.current_name || ref}`);
        // 窗口上限随模型变化，立即刷新上下文指示器
        if (window.ContextGauge) ContextGauge.refresh();
    } catch (e) {
        console.error('切换模型失败:', e);
        modelSelect.value = previousRef;
        showToast('切换模型失败：' + (e.message || e));
    } finally {
        modelSelectBusy = false;
        updateComposerButtons();
    }
}

// ─── 上传到 uploads/ 并加入输入区附件 chip ────────────────────────────
// Ctrl+V、拖放共用：与文件浏览器「添加文件」同一套 pendingAttachments 流程，
// 文件随消息作为附件发送，不再把路径文本插入对话输入框。
async function uploadFilesAsAttachments(files) {
    let uploadedCount = 0;
    const errors = [];
    for (const file of files) {
        const result = await uploadFiles('uploads', [file]);
        const up = (result.uploaded || [])[0];
        if (up) {
            const rel = `uploads/${up.name}`;
            if (!pendingAttachments.some((a) => a.path === rel)) {
                pendingAttachments.push({
                    path: rel,
                    name: up.name,
                    media_type: file.type || 'application/octet-stream',
                });
            }
            uploadedCount += 1;
        }
        errors.push(...(result.errors || []));
    }
    if (uploadedCount > 0) {
        if (window.FileTree && typeof window.FileTree.refresh === 'function') {
            window.FileTree.refresh();
        }
        showToast(`已上传 ${uploadedCount} 个文件到 uploads`);
        renderComposerAttachments();
    }
    updateComposerButtons();
    return { uploadedCount, errors };
}

// Setup event listeners
function setupEventListeners() {
    workspaceSelect.addEventListener('change', handleWorkspaceChange);
    workspaceSettingsBtn.addEventListener('click', openWorkspaceSettings);
    newWorkspaceBtn.addEventListener('click', handleNewWorkspace);
    newSessionBtn.addEventListener('click', createNewSession);
    sessionMenuBtn.addEventListener('click', toggleSessionPanelMenu);
    document.getElementById('sidebar-toggle-btn').addEventListener('click', toggleSidebar);
    document.getElementById('sidebar-open-btn').addEventListener('click', toggleSidebar);
    // 右下角单按钮：停止态点停止，发送态点发送/排队
    composerBtn.addEventListener('click', () => {
        if (composerBtn.dataset.state === 'stop') {
            stopAgent();
        } else {
            sendMessage();
        }
    });
    modelSelect.addEventListener('change', onModelSelectChange);
    if (suggestBtn) suggestBtn.addEventListener('click', handleSuggest);
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
    if (window.AgentCardStore) {
        AgentCardStore.clear();
    }
    _renderedSessionKey = null;
    isMultiSelectMode = false;
    showHiddenSessions = false;
    selectedSessions.clear();
    newSessionBtn.style.display = '';
    if (_footerOriginalHTML !== null) {
        document.getElementById('footer-content').innerHTML = _footerOriginalHTML;
        _footerOriginalHTML = null;
    }
    setFooterAboveFiles(false);
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
        // 没有会话时禁用输入框（有会话时运行中也保持可输入）
        sessionRunning = false;
        queuedItems = [];
        queuePaused = false;
        queueReason = '';
        updateComposerButtons();
        renderQueueBar();
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
        nameInput.placeholder = isStudy ? '学习项目自动按「孩子称呼-科目」命名' : '输入项目名称（选填）';
    }
    if (slugInput) {
        slugInput.disabled = isStudy;
        slugInput.placeholder = isStudy ? '自动按「孩子称呼-科目」生成' : '默认按名称生成，选填';
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
        <div class="session-dropdown-item" data-action="fork">分支会话</div>
        ${showDelete ? '<div class="session-dropdown-item" data-action="delete">删除会话</div>' : ''}
        ${showShare ? '<div class="session-dropdown-item" data-action="share">分享会话</div>' : ''}
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

    openAnchoredMenu(dropdown, sessionMenuBtn, false);

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
        case 'fork':
            await forkSession(session);
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
                chatMessages.innerHTML = '<div class="empty-state">请选择或创建会话</div>';
                sessionRunning = false;
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

    hideSuggestBar({ invalidate: true });  // 切换会话：收起推荐、作废在途请求
    if (window.ContextGauge) ContextGauge.close();  // 上下文面板随会话切换关闭
    const epoch = ++sessionLoadEpoch;
    const sessionKey = `${currentWorkspace.uuid}:${sessionId}`;
    const sameSession = _renderedSessionKey === sessionKey;
    try {
        // 初始加载最近 50 条消息
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}?limit=50&offset=0`);
        const session = await response.json();
        if (epoch !== sessionLoadEpoch) return;
        currentSession = session;
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
        queuedItems = status.queue_items || [];
        queuePaused = !!status.queue_paused;
        queueReason = status.queue_reason || '';
        updateComposerButtons();
        refreshModelSelect();
        if (window.ContextGauge) ContextGauge.refresh();
        renderQueueBar();
        renderSessions();
        renderMessages(session.messages || []);

        if (window.AgentCardStore) {
            AgentCardStore.reconcile();
            await AgentCardStore.mountRunning();
        }
        _renderedSessionKey = sessionKey;

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

    hideSuggestBar({ invalidate: true });  // 新建会话：收起推荐、作废在途请求
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
        if (window.AgentCardStore) {
            AgentCardStore.clear();
        }
        _renderedSessionKey = null;
        // 重连事件流到新会话
        connectEventSource();
        chatInput.disabled = false;
        isSending = false;
        sessionRunning = false;
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

    // System 工作区不适用项目提示词 / 版本管理 / 教材下载
    const systemHiddenTabs = ['ws-instructions', 'ws-git', 'ws-textbooks'];
    document.querySelectorAll('#workspace-settings-modal .settings-tab').forEach(tabBtn => {
        if (systemHiddenTabs.includes(tabBtn.dataset.tab)) {
            tabBtn.style.display = isSystem ? 'none' : '';
        }
    });

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
    } else if (tabName === 'ws-cron') {
        // 定时任务 tab：面板内按钮，隐藏底部保存/删除
        footerSaveBtn.style.display = 'none';
        deleteBtn.style.display = 'none';
        if (window.CronSettings) {
            window.CronSettings.init(currentWorkspace?.uuid || '');
        }
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

let generatePollTimer = null;

function stopGeneratePolling() {
    if (generatePollTimer) {
        clearTimeout(generatePollTimer);
        generatePollTimer = null;
    }
}

async function pollGenerateStatus(taskId) {
    const statusEl = document.getElementById('instructions-status');
    const generateBtn = document.getElementById('instructions-generate-btn');
    const editor = document.getElementById('instructions-editor');
    const filenameEl = document.getElementById('instructions-filename');
    // 轮询期间切换工作区后不能拿旧 taskId 去查新工作区
    const workspaceUuid = currentWorkspace?.uuid;

    const poll = async () => {
        generatePollTimer = null;
        if (!currentWorkspace || currentWorkspace.uuid !== workspaceUuid) return;
        try {
            const response = await fetch(`/api/workspaces/${workspaceUuid}/instructions/generate/${taskId}`);
            const data = await response.json();

            if (data.status === 'running') {
                statusEl.textContent = 'AI 正在分析代码...';
                generatePollTimer = setTimeout(poll, 3000);
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

    stopGeneratePolling();
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

// ─── 教材下载 ───────────────────────────────────────────────────────────────

const textbookState = {
    initialized: false,
    loading: false,
    workspaceUuid: null,
    options: null,        // /api/workspace-wizard/options：学段/年级/科目/教材版本
    books: [],            // 匹配到的教材
    taskId: null,
    taskRunning: false,   // 任务是否仍在 queued/running（与 taskId 分离，便于失败后重试）
    pollTimer: null,
};

function textbookFormatSize(bytes) {
    const value = Number(bytes || 0);
    if (value >= 1024 * 1024 * 1024) return (value / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    if (value >= 1024 * 1024) return (value / 1024 / 1024).toFixed(1) + ' MB';
    if (value >= 1024) return (value / 1024).toFixed(0) + ' KB';
    return value + ' B';
}

function textbookSetStatus(message, type = '') {
    const el = document.getElementById('textbook-task-status');
    if (!el) return;
    el.textContent = message || '';
    el.className = 'settings-status' + (type ? ' ' + type : '');
}

function textbookSelectedBooks() {
    return textbookState.books.filter(book =>
        document.querySelector(`#textbook-list input[data-book-key="${book.book_key}"]`)?.checked
    );
}

function textbookSetButtons(taskRunning = false, hasFailures = false) {
    const downloadBtn = document.getElementById('textbook-download-btn');
    if (downloadBtn) downloadBtn.disabled = textbookState.loading || taskRunning || !textbookState.books.length;
    const refreshBtn = document.getElementById('textbook-refresh-btn');
    if (refreshBtn) refreshBtn.disabled = textbookState.loading || taskRunning;
    document.getElementById('textbook-cancel-btn').style.display = taskRunning ? '' : 'none';
    document.getElementById('textbook-retry-btn').style.display = (!taskRunning && hasFailures) ? '' : 'none';
}

async function initTextbookPanel() {
    const ws = currentWorkspace;
    if (!ws || ws.system || ws.uuid === 'system') {
        textbookSetStatus('系统工作区不支持教材下载', 'error');
        document.getElementById('textbook-list').innerHTML =
            '<div class="textbook-empty">系统工作区不支持教材下载。</div>';
        return;
    }
    if (textbookState.workspaceUuid !== ws.uuid) {
        textbookState.workspaceUuid = ws.uuid;
        textbookState.books = [];
        textbookState.taskId = null;
        textbookState.taskRunning = false;
        if (textbookState.pollTimer) {
            clearInterval(textbookState.pollTimer);
            textbookState.pollTimer = null;
        }
        document.getElementById('textbook-progress').style.display = 'none';
        document.getElementById('textbook-progress').value = 0;
    }
    if (!textbookState.initialized) {
        document.getElementById('textbook-stage').addEventListener('change', () => {
            renderTextbookGrades();
            renderTextbookSubjects();
        });
        document.getElementById('textbook-grade').addEventListener('change', renderTextbookSubjects);
        document.getElementById('textbook-refresh-btn').addEventListener('click', refreshTextbookPanel);
        document.getElementById('textbook-select-all-btn').addEventListener('click', () => {
            document.querySelectorAll('#textbook-list input[type="checkbox"]').forEach(cb => { cb.checked = true; });
            updateTextbookSummary();
        });
        document.getElementById('textbook-clear-btn').addEventListener('click', () => {
            document.querySelectorAll('#textbook-list input[type="checkbox"]').forEach(cb => { cb.checked = false; });
            updateTextbookSummary();
        });
        document.getElementById('textbook-download-btn').addEventListener('click', downloadSelectedTextbooks);
        document.getElementById('textbook-cancel-btn').addEventListener('click', cancelTextbookTask);
        document.getElementById('textbook-retry-btn').addEventListener('click', retryFailedTextbooks);
        textbookState.initialized = true;
        refreshTextbookPanel();
    } else if (!textbookState.options) {
        refreshTextbookPanel();
    }
}

async function refreshTextbookPanel() {
    textbookSetStatus('加载教材目录...');
    try {
        const response = await fetch('/api/workspace-wizard/options');
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '加载失败');
        textbookState.options = data;
        const stageSelect = document.getElementById('textbook-stage');
        const stages = data.stages || [];
        stageSelect.innerHTML = '';
        stages.forEach(stage => {
            const option = document.createElement('option');
            option.value = stage;
            option.textContent = stage;
            stageSelect.appendChild(option);
        });
        if (stages.includes('小学')) stageSelect.value = '小学';
        textbookSetStatus('');
        renderTextbookGrades();
        renderTextbookSubjects();
    } catch (error) {
        textbookSetStatus('✗ 加载教材目录失败: ' + (error.message || error), 'error');
    }
}

function renderTextbookGrades() {
    const stage = document.getElementById('textbook-stage').value;
    const gradeSelect = document.getElementById('textbook-grade');
    const gradeGroup = document.getElementById('textbook-grade-group');
    // 高中教材统一标「高中年级」（按必修/选择性必修分册，不按年级），
    // 年级筛选无区分度，隐藏以免"选了年级列表不变"的困惑
    if (stage.includes('高中')) {
        gradeGroup.style.display = 'none';
        gradeSelect.value = '';
        return;
    }
    gradeGroup.style.display = '';
    const grades = textbookState.options?.grades?.[stage] || [];
    const previous = gradeSelect.value;
    gradeSelect.innerHTML = '';
    grades.forEach(grade => {
        const option = document.createElement('option');
        option.value = grade;
        option.textContent = grade;
        gradeSelect.appendChild(option);
    });
    if (grades.includes(previous)) gradeSelect.value = previous;
}

function renderTextbookSubjects() {
    const stage = document.getElementById('textbook-stage').value;
    const grade = document.getElementById('textbook-grade').value;
    const container = document.getElementById('textbook-subjects');
    const subjects = textbookState.options?.study_subjects?.[stage] || [];
    // 保留已有勾选状态（切换学段/年级/刷新时不丢）
    const previous = {};
    container.querySelectorAll('.wizard-subject-row').forEach(row => {
        const cb = row.querySelector('input[type="checkbox"]');
        const sel = row.querySelector('select');
        if (cb && sel && sel.dataset.subject) {
            previous[sel.dataset.subject] = { checked: cb.checked, edition: sel.value };
        }
    });
    container.innerHTML = '';
    subjects.forEach(subject => {
        const row = document.createElement('div');
        row.className = 'wizard-subject-row';
        const label = document.createElement('label');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = subject;
        checkbox.checked = previous[subject]
            ? previous[subject].checked
            : ['语文', '数学', '英语'].includes(subject);
        checkbox.addEventListener('change', loadTextbookBooks);
        label.appendChild(checkbox);
        label.append(subject);
        row.appendChild(label);

        const select = document.createElement('select');
        select.className = 'wizard-subject-edition';
        select.dataset.subject = subject;
        select.title = `${subject} 教材版本`;
        const allEditions = textbookState.options?.textbook_editions?.[stage]?.[subject] || [];
        const editions = allEditions.filter(item => editionCoversGrade(item, grade));
        const usable = editions.length ? editions : allEditions;
        if (usable.length) {
            usable.forEach(edition => {
                const option = document.createElement('option');
                option.value = edition.name;
                option.textContent = edition.publisher
                    ? `${edition.name}（${edition.publisher}）` : edition.name;
                select.appendChild(option);
            });
            const preserved = previous[subject]?.edition;
            if (preserved && usable.some(item => item.name === preserved)) {
                select.value = preserved;
            } else {
                const fallback = usable.find(edition => edition.default) || usable[0];
                select.value = fallback.name;
            }
        } else {
            select.disabled = true;
            const option = document.createElement('option');
            option.value = '';
            option.textContent = '暂无教材';
            select.appendChild(option);
        }
        select.addEventListener('change', loadTextbookBooks);
        row.appendChild(select);
        container.appendChild(row);
    });
    loadTextbookBooks();
}

function textbookSelections() {
    const selections = [];
    document.querySelectorAll('#textbook-subjects .wizard-subject-row').forEach(row => {
        const cb = row.querySelector('input[type="checkbox"]');
        const sel = row.querySelector('select');
        if (cb && cb.checked && sel && sel.value) {
            selections.push({ subject: sel.dataset.subject, edition: sel.value });
        }
    });
    return selections;
}

async function loadTextbookBooks() {
    const stage = document.getElementById('textbook-stage').value;
    const grade = document.getElementById('textbook-grade').value;
    const selections = textbookSelections();
    if (!selections.length) {
        textbookState.books = [];
        renderTextbookBooks();
        return;
    }
    textbookState.loading = true;
    textbookSetButtons();
    try {
        const responses = await Promise.all(selections.map(({ subject, edition }) => {
            const params = new URLSearchParams({ stage, subject, edition, grade });
            return fetch('/api/textbooks/options?' + params.toString()).then(r => r.json());
        }));
        const seen = new Set();
        const books = [];
        responses.forEach(data => {
            (data.books || []).forEach(book => {
                if (seen.has(book.book_key)) return;
                seen.add(book.book_key);
                books.push(book);
            });
        });
        books.sort((a, b) =>
            (a.subject || '').localeCompare(b.subject || '', 'zh') ||
            (a.grade || '').localeCompare(b.grade || '', 'zh') ||
            (a.volume || '').localeCompare(b.volume || '', 'zh'));
        textbookState.books = books;
        renderTextbookBooks();
    } catch (error) {
        textbookSetStatus('✗ 加载教材列表失败: ' + (error.message || error), 'error');
    } finally {
        textbookState.loading = false;
        textbookSetButtons();
    }
}

function renderTextbookBooks() {
    const listEl = document.getElementById('textbook-list');
    if (!textbookState.books.length) {
        listEl.innerHTML = '<div class="textbook-empty">没有匹配的教材。可换学段/年级或调整科目版本。</div>';
        updateTextbookSummary();
        return;
    }
    listEl.innerHTML = '';
    textbookState.books.forEach(book => {
        const label = document.createElement('label');
        label.className = 'textbook-item';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = true;
        checkbox.dataset.bookKey = book.book_key;
        checkbox.addEventListener('change', updateTextbookSummary);
        const text = document.createElement('span');
        text.className = 'textbook-item-text';
        const sizeText = Number(book.total_size) > 0 ? ' · ' + textbookFormatSize(book.total_size) : '';
        text.textContent = `${book.title} · ${book.grade || ''}${book.semester || ''}${sizeText}`;
        label.appendChild(checkbox);
        label.appendChild(text);
        listEl.appendChild(label);
    });
    updateTextbookSummary();
}

function updateTextbookSummary() {
    const selected = textbookSelectedBooks();
    const totalSize = selected.reduce((sum, book) => sum + Number(book.total_size || 0), 0);
    const el = document.getElementById('textbook-summary');
    if (el) {
        const sizeText = totalSize > 0 ? '，共 ' + textbookFormatSize(totalSize) : '';
        el.textContent = textbookState.books.length
            ? '已选 ' + selected.length + ' 本' + sizeText + '（共匹配 ' + textbookState.books.length + ' 本）'
            : '';
    }
    textbookSetButtons(textbookState.taskRunning);
}

function isTextbookTaskRunning(status) {
    return status === 'queued' || status === 'running';
}

const TEXTBOOK_STATUS_TEXT = {
    running: '下载中',
    done: '已完成',
    partial_failed: '部分失败',
    failed: '失败',
    cancelled: '已取消',
};

async function downloadSelectedTextbooks() {
    const selected = textbookSelectedBooks();
    if (!selected.length) {
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
                book_keys: selected.map(book => book.book_key),
            }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '创建任务失败');
        startTextbookPolling(data.task_id);
    } catch (error) {
        downloadBtn.disabled = false;
        textbookSetStatus('✗ ' + error.message, 'error');
    }
}

function startTextbookPolling(taskId) {
    textbookState.taskId = taskId;
    textbookState.taskRunning = true;
    textbookSetButtons(true);
    const progress = document.getElementById('textbook-progress');
    progress.style.display = '';
    progress.value = 0;
    if (textbookState.pollTimer) clearInterval(textbookState.pollTimer);
    textbookState.pollTimer = setInterval(pollTextbookTask, 1000);
}

async function pollTextbookTask() {
    if (!textbookState.taskId) return;
    try {
        const response = await fetch('/api/textbooks/download/' + textbookState.taskId);
        const task = await response.json();
        if (!response.ok) throw new Error(task.detail || '查询失败');
        const percent = task.total_bytes
            ? Math.min(100, Math.round(task.downloaded_bytes / task.total_bytes * 100))
            : 0;
        document.getElementById('textbook-progress').value = percent;
        const failed = (task.books || []).filter(book => book.status === 'failed');
        const skipped = (task.books || []).filter(book => book.status === 'skipped');
        const done = (task.books || []).filter(book => book.status === 'done');
        textbookSetStatus(
            '下载状态：' + (TEXTBOOK_STATUS_TEXT[task.status] || task.status) +
            '，完成 ' + done.length + '，跳过 ' + skipped.length +
            '，失败 ' + failed.length + '；' + textbookFormatSize(task.downloaded_bytes) +
            ' / ' + textbookFormatSize(task.total_bytes) + '（' + percent + '%）' +
            (failed.length ? '；首个错误：' + failed[0].error : '')
        );
        if (!isTextbookTaskRunning(task.status)) {
            textbookState.taskRunning = false;
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
