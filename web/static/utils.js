// ── utils.js ── 工具函数
// 通用工具，无业务依赖，其他模块均可引用

// ── localStorage 位置持久化 ──
// 存储格式: { workspace_uuid: "当前工作区", ws_sessions: { "ws-uuid": "session_id" } }
// ws_sessions 按工作区记忆上次访问的会话，刷新或切换回来可恢复
const POSITION_KEY = 'cili_last_position';

function savePosition({ workspace_uuid, session_id } = {}) {
    try {
        const current = JSON.parse(localStorage.getItem(POSITION_KEY) || '{}');
        if (workspace_uuid !== undefined) current.workspace_uuid = workspace_uuid;
        // 当传了 session_id 时，关联到当前工作区（优先用传入的 workspace_uuid，否则用已保存的）
        const ws = workspace_uuid || current.workspace_uuid;
        if (ws && session_id !== undefined) {
            current.ws_sessions = current.ws_sessions || {};
            current.ws_sessions[ws] = session_id;
        }
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

// ── 辅助函数：HTML转义 ──
function escapeHtml(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ── 工作区列表排序 ──
// 中文按拼音排序（区域设置影响不大，数字按数值比较），同名/同前缀（如同一孩子）自然相邻
const _workspaceNameCollator = new Intl.Collator('zh-Hans-CN', { numeric: true, sensitivity: 'base' });

function sortWorkspacesByName(workspaces) {
    return (workspaces || []).slice().sort(
        (a, b) => _workspaceNameCollator.compare(a?.name || '', b?.name || '')
    );
}

// ── 显示提示信息 ──
function showToast(message, duration = 2000) {
    let toast = document.getElementById('toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'toast';
        toast.className = 'toast';
        document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => toast.classList.remove('show'), duration);
}

// ── Markdown 渲染 ──

// 判断 URL 是否指向图片文件（根据扩展名，忽略查询参数）
function _isImageUrl(url) {
    const path = url.split('?')[0];
    return /\.(png|jpe?g|gif|webp|svg|bmp|ico)(\s|$)/i.test(path);
}

// 将非图片媒体的 ![alt](url) 降级为 [alt](url)（如 .pdf / .docx），避免 marked 渲染为 <img>
function _convertNonImageMedia(text) {
    return text.replace(
        /!\[([^\]]*)\]\(([^)]+)\)/g,
        (m, alt, url) => {
            if (url.startsWith('data:')) return m;  // data URI 由 media_type 决定
            if (_isImageUrl(url)) return m;           // 图片扩展名保留
            return `[${alt}](${url})`;
        }
    );
}

// 自动为链接注入 workspace_uuid，保护数学公式不被 marked 破坏
function renderMarkdown(text) {
    if (!text) return '';

    // 将非图片媒体的 ![alt](url) 降级为普通链接，避免 marked 渲染为 <img>
    text = _convertNonImageMedia(text);

    // 获取 workspace_uuid：优先 currentWorkspace，其次 currentSession
    const workspaceUuid = currentWorkspace?.uuid || currentSession?.workspace_uuid;

    // 匹配 ![alt](/api/files/xxx) 并追加 workspace_uuid
    if (workspaceUuid) {
        text = text.replace(
            /!\[([^\]]*)\]\((\/api\/files\/[^)]+)\)/g,
            (match, alt, url) => {
                // 如果已经有 workspace_uuid 参数，跳过
                if (url.includes('workspace_uuid=')) {
                    return match;
                }
                const sep = url.includes('?') ? '&' : '?';
                return `![${alt}](${url}${sep}workspace_uuid=${workspaceUuid})`;
            }
        );

    // 匹配相对路径图片并转换为 /api/workspaces/{uuid}/files/xxx
        text = text.replace(
            /!\[([^\]]*)\]\((?!http|\/api|data:)([^)]+)\)/g,
            (match, alt, url) => {
                const cleanUrl = url.startsWith('/') ? url.slice(1) : url;
                return `![${alt}](/api/workspaces/${workspaceUuid}/files/${cleanUrl})`;
            }
        );

        // 匹配普通文件链接 [text](/api/files/xxx) 并追加 workspace_uuid
        text = text.replace(
            /\[([^\]]*)\]\((\/api\/files\/[^)]+)\)/g,
            (match, linkText, url) => {
                if (url.includes('workspace_uuid=')) {
                    return match;
                }
                const sep = url.includes('?') ? '&' : '?';
                return `[${linkText}](${url}${sep}workspace_uuid=${workspaceUuid})`;
            }
        );

        // 匹配相对路径文件链接 [text](file.md) 并转换为 /api/workspaces/{uuid}/files/xxx
        text = text.replace(
            /\[([^\]]*)\]\((?!http|\/api|data:|#)([^)]+)\)/g,
            (match, linkText, url) => {
                const cleanUrl = url.startsWith('/') ? url.slice(1) : url;
                return `[${linkText}](/api/workspaces/${workspaceUuid}/files/${cleanUrl})`;
            }
        );

        // 匹配原生 <audio>/<video>/<source> 标签的相对 src 路径并转换为文件服务 URL
        // 限定标签范围；跳过外部 URL、已转换的 /api 路径与 data: URI
        text = text.replace(
            /<(audio|video|source)\b([^>]*?)(\s+)src=(?:"([^"]*)"|'([^']*)')([^>]*?>)/gi,
            (match, tag, before, ws, dq, sq, after) => {
                const url = dq ?? sq ?? '';
                if (!url || /^(?:https?:|\/api|data:)/i.test(url)) return match;
                const cleanUrl = url.startsWith('/') ? url.slice(1) : url;
                return `<${tag}${before}${ws}src="/api/workspaces/${workspaceUuid}/files/${cleanUrl}"${after}`;
            }
        );
    }

    return renderMarkdownCore(text);
}

// ── Mermaid 图表渲染 ──
// 初始化 mermaid 并通过 MutationObserver 自动渲染插入 DOM 的 .mermaid 元素
if (typeof mermaid !== 'undefined') {
    const mermaidTheme = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'default';
    mermaid.initialize({
        startOnLoad: false,
        theme: mermaidTheme,
        securityLevel: 'loose',
    });

    // 存储每个元素对应的原始源码，用于主题切换时重新渲染
    const mermaidSourceMap = new WeakMap();

    async function renderMermaidIn(container) {
        if (typeof mermaid === 'undefined') return;
        const elements = container.querySelectorAll
            ? container.querySelectorAll('.mermaid:not([data-mermaid-error])')
            : [];
        for (const el of elements) {
            // 保存原始源码（首次渲染前）
            if (!mermaidSourceMap.has(el)) {
                mermaidSourceMap.set(el, el.textContent.trim());
            }
            try {
                const source = mermaidSourceMap.get(el);
                if (!source) continue;
                const id = 'mmd-' + Math.random().toString(36).slice(2, 9);
                const { svg } = await mermaid.render(id, source);
                el.innerHTML = svg;
                el.removeAttribute('data-mermaid-error');
            } catch (e) {
                el.setAttribute('data-mermaid-error', 'true');
                const orig = mermaidSourceMap.get(el) || '';
                el.innerHTML = '<pre class="mermaid-error">' + escapeHtml(e.message || 'Mermaid 渲染失败') + '\n\n' + escapeHtml(orig) + '</pre>';
            }
        }
    }
    // 暴露为全局函数，供主题切换调用
    window.renderMermaidIn = renderMermaidIn;

    // 全局 MutationObserver：监听 DOM 插入的 .mermaid 元素并自动渲染
    const mermaidObserver = new MutationObserver((mutations) => {
        for (const m of mutations) {
            for (const node of m.addedNodes) {
                if (!(node instanceof HTMLElement)) continue;
                if (node.classList?.contains('mermaid')) {
                    renderMermaidIn(node.parentElement || document.body);
                } else if (node.querySelector?.('.mermaid')) {
                    renderMermaidIn(node);
                }
            }
        }
    });
    mermaidObserver.observe(document.body, { childList: true, subtree: true });
}

// ── 获取文件图标 ──
function getFileIcon(filename) {
    const ext = filename.split('.').pop().toLowerCase();
    const icons = {
        'js': '📜', 'ts': '📜', 'py': '🐍', 'java': '☕', 'go': '🔷',
        'html': '🌐', 'css': '🎨', 'scss': '🎨',
        'json': '📋', 'yaml': '📋', 'yml': '📋', 'xml': '📋', 'toml': '📋',
        'md': '📝', 'txt': '📄', 'log': '📃',
        'png': '🖼️', 'jpg': '🖼️', 'jpeg': '🖼️', 'gif': '🖼️', 'svg': '🖼️', 'webp': '🖼️',
        'pdf': '📕', 'doc': '📘', 'docx': '📘',
        'zip': '📦', 'tar': '📦', 'gz': '📦', 'rar': '📦',
        'sh': '⚙️', 'bat': '⚙️', 'cmd': '⚙️', 'ps1': '⚙️'
    };
    return icons[ext] || '📄';
}

// ── 聊天区贴底滚动（SSE 与子代理卡片共用）──
function scrollChatIfNearBottom() {
    const el = document.getElementById('chat-messages');
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 100) {
        el.scrollTop = el.scrollHeight;
    }
}

// ── 访问令牌 (access_token) ──
// 服务端配置 access_token 后（多用于非 localhost 绑定），前端需携带令牌访问。
// URL ?token=xxx 仅作首次引导：读取后立即存入 localStorage 并从地址栏移除
// （避免令牌滞留在历史记录/Referer 中，P1-32）；服务端在首次 ?token= 鉴权时
// 同步下发 HttpOnly cookie，供静态资源与页面刷新使用。之后所有 fetch 请求
// 自动注入 X-Access-Token 头（含 SSE 流式请求）。
const TOKEN_KEY = 'cili_access_token';

// 启动时消费一次 URL 令牌：入 localStorage、抹掉地址栏 ?token=
function consumeUrlToken() {
    try {
        const params = new URLSearchParams(window.location.search);
        const urlToken = params.get('token');
        if (!urlToken) return;
        localStorage.setItem(TOKEN_KEY, urlToken);
        params.delete('token');
        const qs = params.toString();
        history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : ''));
    } catch (e) { /* localStorage 不可用等：保持 URL 原样 */ }
}
consumeUrlToken();

function getAccessToken() {
    try {
        return localStorage.getItem(TOKEN_KEY) || '';
    } catch (e) { return ''; }
}

function patchFetchWithToken() {
    const token = getAccessToken();
    if (!token) return;
    const originalFetch = window.fetch;
    window.fetch = function (input, init) {
        init = init || {};
        const headers = new Headers(init.headers || {});
        headers.set('X-Access-Token', token);
        init.headers = headers;
        return originalFetch.call(this, input, init);
    };
}
patchFetchWithToken();
