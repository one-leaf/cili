// ── utils.js ── 工具函数
// 通用工具，无业务依赖，其他模块均可引用

// ── 图片压缩 ──
const MAX_IMAGE_DIMENSION = 1920;  // 最大宽高
const MAX_IMAGE_SIZE_BYTES = 2 * 1024 * 1024;  // 2MB

async function compressImage(file, quality = 0.85) {
    return new Promise((resolve) => {
        const img = new Image();
        const url = URL.createObjectURL(file);
        img.onload = () => {
            URL.revokeObjectURL(url);
            let { width, height } = img;
            // 缩放
            if (width > MAX_IMAGE_DIMENSION || height > MAX_IMAGE_DIMENSION) {
                const scale = Math.min(MAX_IMAGE_DIMENSION / width, MAX_IMAGE_DIMENSION / height);
                width = Math.round(width * scale);
                height = Math.round(height * scale);
            }
            const canvas = document.createElement('canvas');
            canvas.width = width;
            canvas.height = height;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, width, height);
            canvas.toBlob((blob) => {
                if (!blob) { resolve({ data: null, media_type: 'image/jpeg' }); return; }
                const reader = new FileReader();
                reader.onload = (ev) => {
                    const dataUrl = ev.target.result;
                    const commaIdx = dataUrl.indexOf(',');
                    const data = dataUrl.substring(commaIdx + 1);
                    resolve({ data, media_type: 'image/jpeg', preview_url: dataUrl, size: blob.size });
                };
                reader.readAsDataURL(blob);
            }, 'image/jpeg', quality);
        };
        img.onerror = () => {
            URL.revokeObjectURL(url);
            resolve({ data: null, media_type: 'image/jpeg' });
        };
        img.src = url;
    });
}

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
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
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

    // 保护数学公式：提取 $$...$$ 和 $...$ 为占位符，避免 marked 破坏 LaTeX 语法
    const mathBlocks = [];
    // 先处理 display math $$...$$
    text = text.replace(/\$\$([\s\S]*?)\$\$/g, (match, formula) => {
        const placeholder = 'MATHBLOCK{' + mathBlocks.length + '}';
        mathBlocks.push(match);
        return placeholder;
    });
    // 再处理 inline math $...$（排除转义的 \$）
    text = text.replace(/(?<!\$)\$(?!\$)(.*?)(?<!\$)\$(?!\$)/g, (match, formula) => {
        const placeholder = 'MATHBLOCK{' + mathBlocks.length + '}';
        mathBlocks.push(match);
        return placeholder;
    });

    // 解析 markdown
    let html = marked.parse(text);

    // 将 mermaid 代码块转换为 <div class="mermaid">，由 mermaid.js 渲染为 SVG
    html = html.replace(/<pre><code class="language-mermaid">([\s\S]*?)<\/code><\/pre>/g, (match, content) => {
        // 还原 marked 的 HTML 转义，mermaid 需要原始文本
        const decoded = content
            .replace(/&lt;/g, '<')
            .replace(/&gt;/g, '>')
            .replace(/&quot;/g, '"')
            .replace(/&#39;/g, "'")
            .replace(/&amp;/g, '&');
        return '<div class="mermaid">' + decoded + '</div>';
    });

    // 将 PDF <img> 转换为 iframe 内嵌预览（兜底：URL 中已有 workspace_uuid 的 PDF 不会走降级逻辑）
    html = html.replace(
        /<img([^>]*)\ssrc="([^"]*\.pdf)(\?[^"]*)?"/g,
        (match, before, src, query) => {
            const url = src + (query || '');
            const altMatch = before.match(/alt="([^"]*)"/);
            const alt = altMatch ? altMatch[1] : 'PDF 文件';
            return `<div class="pdf-preview"><iframe src="${url}" title="${alt}"></iframe><a href="${url}" target="_blank" class="pdf-link">📄 ${alt || '查看 PDF'}</a></div>`;
        }
    );

    // 恢复数学公式
    mathBlocks.forEach((block, idx) => {
        html = html.replace('MATHBLOCK{' + idx + '}', block);
    });

    // XSS 防护：sanitize HTML（允许 MathJax 所需标签）
    if (typeof DOMPurify !== 'undefined') {
        html = DOMPurify.sanitize(html, {
            ADD_TAGS: ['mjx-container', 'annotation', 'semantics', 'math', 'iframe'],
            ADD_ATTR: ['encoding', 'display', 'xmlns', 'target', 'allowfullscreen'],
        });
    } else {
        // DOMPurify 加载失败时 fail-closed：转义所有标签，阻止原始 HTML 注入。
        // 转义后的文本节点内 `<`/`>` 经浏览器解码还原，MathJax 仍可正常渲染公式。
        html = html.replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
    return html;
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

// ── 格式化文件大小 ──
function formatFileSize(bytes) {
    if (!bytes) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    return (bytes / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
}

// ── 访问令牌 (access_token) ──
// 服务端配置 access_token 后（多用于非 localhost 绑定），前端需携带令牌访问。
// URL ?token=xxx 支持首次页面加载与静态资源（浏览器无法给静态资源加请求头），
// 之后所有 fetch 请求自动注入 X-Access-Token 头（含 SSE 流式请求）。
const TOKEN_KEY = 'cili_access_token';

function getAccessToken() {
    try {
        const urlToken = new URLSearchParams(window.location.search).get('token');
        if (urlToken) {
            localStorage.setItem(TOKEN_KEY, urlToken);
            return urlToken;
        }
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
