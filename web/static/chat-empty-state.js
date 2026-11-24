// ── 聊天空状态：新会话/工作流示例与欢迎区 HTML ──
// 依赖 app.js 的会话状态与 sidebarFilter；供 chat.js / workflow_pane.js 调用。

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
