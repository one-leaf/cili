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
// 自动为链接注入 workspace_uuid，保护数学公式不被 marked 破坏
function renderMarkdown(text) {
    if (!text) return '';

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

    // 恢复数学公式
    mathBlocks.forEach((block, idx) => {
        html = html.replace('MATHBLOCK{' + idx + '}', block);
    });

    // XSS 防护：sanitize HTML（允许 MathJax 所需标签）
    if (typeof DOMPurify !== 'undefined') {
        html = DOMPurify.sanitize(html, {
            ADD_TAGS: ['mjx-container', 'annotation', 'semantics', 'math'],
            ADD_ATTR: ['encoding', 'display', 'xmlns'],
        });
    } else {
        // DOMPurify 加载失败时 fail-closed：转义所有标签，阻止原始 HTML 注入。
        // 转义后的文本节点内 `<`/`>` 经浏览器解码还原，MathJax 仍可正常渲染公式。
        html = html.replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
    return html;
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
