// 会话来源通道的展示名（core/channels.py 的 id → 中文标签）
const SESSION_CHANNEL_LABELS = {
    web: '网页', qq: 'QQ', wechat: '微信', cli: '终端', email: '邮件', cron: '定时',
};

// 新会话空状态的示例输入（app.js 与 chat.js 共用；点击写入输入框）
const NEW_SESSION_EXAMPLES = [
    '今日有哪些新闻',
    '给电脑做一次体检',
    '小学一年级语文第一课是什么',
    '画一张剖切的中式凉亭透视结构图，掀开屋顶能看到房梁的榫卯结构',
];

// 工作流模式的示例输入：侧栏切到工作流且没打开任何工作流时展示
const NEW_WORKFLOW_EXAMPLES = [
    '创建一个系统安全检查的工作流',
    '创建一个代码质量巡检工作流：扫描问题、归类、给修复建议',
    '创建一个把长文档变成摘要和要点卡片的学习工作流',
    '创建一个旅行计划工作流：查资料、排行程、出行程单',
];

function workflowWelcomeActive() {
    if (sidebarFilter !== 'workflow') return false;
    return !(window.WorkflowPane
        && typeof WorkflowPane.getCurrentWorkflowId === 'function'
        && WorkflowPane.getCurrentWorkflowId());
}

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

// 步骤名在示例里只取主标题（「认书：核实书目…」→「认书」），避免示例过长
function shortStepName(name) {
    const base = String(name || '').split(/[：:]/)[0].trim();
    return base.length > 12 ? base.slice(0, 12) : base;
}

// 工作流会话（编辑/运行结果）的示例：围绕当前工作流可执行的操作
function workflowSessionExamples(metadata) {
    const results = metadata.run_results;
    const panel = metadata.workflow_panel;
    const pane = window.WorkflowPane;
    const workflow = pane && pane.getCurrentWorkflow();
    const state = (pane && pane.getCurrentState()) || {};
    const boundId = (results && results.workflow_id) || (panel && panel.workflow_id) || '';
    const sameWorkflow = workflow && (!boundId || workflow.id === boundId);
    const steps = sameWorkflow && Array.isArray(workflow.steps) ? workflow.steps : [];

    if (results) {
        // 失败步骤优先作为「只重跑第 N 步」示例
        let target = -1;
        steps.forEach((step, index) => {
            const item = state.items && state.items[step.id];
            if (target < 0 && item && item.status === 'failed') target = index;
        });
        if (target < 0) target = 0;
        const targetName = steps[target] ? shortStepName(steps[target].name) : '';
        return [
            '执行当前工作流',
            targetName ? `只重跑第 ${target + 1} 步：${targetName}` : '只重跑某一步',
            '把执行结果推送给我',
            '总结这次运行的问题',
        ];
    }
    const firstName = steps[0] ? shortStepName(steps[0].name) : '';
    return [
        '运行当前工作流',
        firstName ? `修改第 1 步：${firstName}` : '修改某一步的要求',
        '给工作流增加一个汇总步骤',
        '修改运行输入项',
    ];
}

function examplesHtml(examples) {
    return examples
        .map(t => `<button type="button" class="welcome-example">${t}</button>`)
        .join('');
}

// 空状态提示：工作流会话用运行/修改示例，工作流模式（未打开工作流）用创建示例，其余用会话示例
function newSessionWelcomeHtml() {
    const metadata = (currentSession && currentSession.metadata) || {};
    if (metadata.run_results || (metadata.workflow_panel && metadata.workflow_panel.workflow_id)) {
        const isResults = !!metadata.run_results;
        return `<div class="welcome-message"><h2>${isResults ? '这次运行的结果' : '编辑工作流'}</h2>`
            + `<p>可以直接提问，或点击示例：</p>`
            + `<div class="welcome-examples">${examplesHtml(workflowSessionExamples(metadata))}</div></div>`;
    }
    if (metadata.workflow_panel || workflowWelcomeActive()) {
        return `<div class="welcome-message"><h2>描述你的工作目标</h2>`
            + `<p>描述目标后 AI 会生成步骤，或点击示例：</p>`
            + `<div class="welcome-examples">${examplesHtml(NEW_WORKFLOW_EXAMPLES)}</div></div>`;
    }
    return `<div class="welcome-message"><h2>开始新对话</h2>`
        + `<p>输入消息开始使用，或点击示例：</p>`
        + `<div class="welcome-examples">${examplesHtml(NEW_SESSION_EXAMPLES)}</div></div>`;
}

// State
let currentWorkspace = null;  // {uuid, name, directory}
let currentSession = null;
let _renderedSessionKey = null;
let sessionLoadEpoch = 0;  // 会话加载代次：旧响应到达时丢弃
// 最近一次普通会话（非工作流面板/结果会话）：切回「会话」模式时恢复它
let _lastSessionModeSessionId = '';
// 最近打开的工作流：切回「工作流」模式时恢复它
let _lastWorkflowModeId = '';
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
// 后台任务状态（/status.background、session_start/session_complete 后刷新）。
// 只用于 Composer 黄态/徽标，绝不参与 sendMessage 的排队判断。
let sessionBackground = { agents: 0, shells: 0, active: 0, items: [] };
let queuedItems = [];            // 服务端队列快照（queue_update / /status）
let queuePaused = false;         // 队列已暂停（停止/出错后需显式继续或清空）
let queueReason = '';            // 暂停原因：stopped / error
let isStopping = false;          // 已发 /stop，等待回合真正收尾
let isMultiSelectMode = false;
let showHiddenSessions = false;
let selectedSessions = new Set();
// 侧栏列表类型（session / workflow），持久化到 localStorage
let sidebarFilter = 'session';
let selectedWorkflows = new Set();
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
// 最近一批建议的文本：草稿命中说明是误触（如重复点击），不再请求
let suggestHistory = new Set();
// 最近一批是否是「改写草稿」模式：点击建议时替换而不是追加
let suggestDraftMode = false;


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
// 覆盖 renderMarkdownWithLinks 产出的两种形态：
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
    initSidebarFilter();
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

// 侧栏列表类型切换（会话 / 工作流）；新建与导入跟随当前类型
const SIDEBAR_FILTER_KEY = 'cili_sidebar_filter';

function initSidebarFilter() {
    const buttons = Array.from(document.querySelectorAll('.sidebar-filter-btn'));
    if (!buttons.length) return;
    try {
        const saved = localStorage.getItem(SIDEBAR_FILTER_KEY);
        if (saved === 'session' || saved === 'workflow') sidebarFilter = saved;
    } catch (e) { /* ignore */ }
    const applyActive = () => {
        buttons.forEach(btn => {
            const active = btn.dataset.filter === sidebarFilter;
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-selected', String(active));
        });
        if (newSessionBtn) {
            newSessionBtn.title = sidebarFilter === 'workflow' ? '新建工作流' : '新建会话';
        }
    };
    buttons.forEach(btn => btn.addEventListener('click', async () => {
        if (!btn.dataset.filter || btn.dataset.filter === sidebarFilter) return;
        sidebarFilter = btn.dataset.filter;
        // 切换后不可见条目的选中状态要丢掉，避免批量操作误伤
        selectedSessions.clear();
        selectedWorkflows.clear();
        try { localStorage.setItem(SIDEBAR_FILTER_KEY, sidebarFilter); } catch (e) { /* ignore */ }
        applyActive();
        updateFooterToolbar();
        renderSidebarList();
        // 当前显示的是空状态欢迎块时，示例先跟随模式切换
        if (chatMessages.querySelector('.welcome-message')) {
            chatMessages.innerHTML = newSessionWelcomeHtml();
        }
        await syncChatToMode();
    }));
    applyActive();

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
    const bgActive = !!(sessionBackground && sessionBackground.active > 0);
    // 红只表示 master；黄只表示后台；有草稿永远是发送。
    let state = 'disabled';
    if (hasSession) {
        if (draft) {
            state = 'send';
        } else if (sessionRunning) {
            state = 'stop';           // 红：停 master
        } else if (bgActive) {
            state = 'stop-bg';        // 黄：停全部后台
        }
    }
    composerBtn.dataset.state = state;
    composerBtn.innerHTML = (state === 'stop' || state === 'stop-bg')
        ? COMPOSER_STOP_ICON : COMPOSER_SEND_ICON;

    let title = '发送';
    if (state === 'stop') {
        title = '停止当前回合';
    } else if (state === 'stop-bg') {
        title = '停止后台任务';
    } else if (state === 'send' && sessionRunning) {
        title = '排队发送';
    }
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
    if (invalidate) {
        suggestRequestSeq++;  // 丢弃在途/过期响应
        suggestHistory.clear();
        suggestDraftMode = false;
    }
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
    // 输入框有草稿：带上它生成「改写/美化」建议；草稿已在最近一批里则视为误触，不执行
    const draft = (chatInput && chatInput.value ? chatInput.value : '').trim();
    if (draft && suggestHistory.has(draft)) return;
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
                    input: draft,
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
        renderSuggestBar(items, { fromDraft: !!draft });
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

function renderSuggestBar(items, { fromDraft = false } = {}) {
    if (!suggestBar) return;
    suggestItems = items.slice();
    // 记住整批（收起气泡后仍生效）：再次以同一条草稿请求视为误触
    suggestHistory = new Set(items);
    suggestDraftMode = !!fromDraft;
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
    if (suggestDraftMode) {
        // 改写草稿：直接替换输入框内容
        chatInput.value = text;
    } else {
        const existing = chatInput.value.trim();
        chatInput.value = existing ? existing + ' ' + text : text;
    }
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
