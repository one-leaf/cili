// ── settings.js ── 设置相关逻辑
// 依赖 app.js 全局变量: currentWorkspace, chatInput, sendBtn
// 依赖 utils.js 函数: showToast

// ── 全局设置 ──

// 打开设置说明窗口
async function openSettingsHelp() {
    const newWindow = window.open('', '_blank');
    if (!newWindow) {
        alert('无法打开新窗口，请检查浏览器是否阻止了弹出窗口');
        return;
    }

    // Show loading state
    newWindow.document.write('<html><head><title>加载中...</title></head><body><p>正在加载设置说明...</p></body></html>');

    try {
        const response = await fetch('/docs/settings-guide.md');
        if (!response.ok) throw new Error('无法加载文档');
        const markdown = await response.text();

        const html = `
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>设置说明</title>
    <script src="${window.location.origin}/static/libs/marked.min.js"><\/script>
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
    <script id="MathJax-script" async src="${window.location.origin}/static/libs/mathjax/es5/tex-mml-chtml.js"><\/script>
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
        const markdown = ${JSON.stringify(markdown)};
        // 保护数学公式不被 marked 破坏
        function renderMarkdownWithMath(text) {
            if (!text) return '';
            const mathBlocks = [];
            text = text.replace(/\$\$([\s\S]*?)\$\$/g, (match) => {
                const placeholder = 'MATHBLOCK{' + mathBlocks.length + '}';
                mathBlocks.push(match);
                return placeholder;
            });
            text = text.replace(/(?<!\$)\$(?!\$)(.*?)(?<!\$)\$(?!\$)/g, (match) => {
                const placeholder = 'MATHBLOCK{' + mathBlocks.length + '}';
                mathBlocks.push(match);
                return placeholder;
            });
            let html = marked.parse(text);
            mathBlocks.forEach((block, idx) => {
                html = html.replace('MATHBLOCK{' + idx + '}', block);
            });
            return html;
        }
        document.querySelector('.md-content').innerHTML = renderMarkdownWithMath(markdown);
        if (window.MathJax && window.MathJax.typesetPromise) {
            MathJax.typesetPromise();
        }
    <\/script>
</body>
</html>
        `;

        newWindow.document.open();
        newWindow.document.write(html);
        newWindow.document.close();
        newWindow.document.title = '设置说明';
    } catch (error) {
        newWindow.document.open();
        newWindow.document.write(`<html><head><title>加载失败</title></head><body><p>无法加载设置说明：${error.message}</p></body></html>`);
        newWindow.document.close();
    }
}

// 加载某个角色（worker/lite）模型配置到表单
// 收集某个角色（worker/lite）模型配置；name 留空 → 信号后端移除、回退继承 Master
// ── MCP 服务器管理 ──
// 前端内存态：加载的服务器（保留原 headers，保存时 _preserve_headers）+
// 表单新填的服务器（带完整 headers），一次性随 saveSettings 提交。
let mcpServers = {};  // name -> server config

function mcpParseKv(text) {
    const result = {};
    (text || '').split(',').forEach(pair => {
        const idx = pair.indexOf('=');
        if (idx > 0) result[pair.slice(0, idx).trim()] = pair.slice(idx + 1).trim();
    });
    return result;
}

async function loadMcpServers() {
    try {
        const response = await fetch('/api/mcp/servers');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();
        mcpServers = {};
        for (const [name, s] of Object.entries(data.servers || {})) {
            // _loaded 标记：已保存服务器，保存时保留原 headers（密钥不回传前端）
            mcpServers[name] = { ...s, _loaded: true };
        }
    } catch (e) {
        mcpServers = {};
    }
    renderMcpServers();
}

function renderMcpServers() {
    const container = document.getElementById('mcp-server-list');
    const statusEl = document.getElementById('mcp-status');
    const names = Object.keys(mcpServers);
    if (names.length === 0) {
        container.innerHTML = '<div class="form-hint" style="color: var(--text-secondary);">尚未配置任何 MCP 服务器。</div>';
        statusEl.textContent = '';
        return;
    }
    const statusText = (s) => {
        if (s.status === 'connected') return `已连接（${s.tool_count} 工具）`;
        if (s.status === 'connecting') return '连接中...';
        if (s.status === 'failed') return '连接失败';
        return '离线';
    };
    const statusColor = (s) => {
        if (s.status === 'connected') return '#4caf50';
        if (s.status === 'failed') return '#e74c3c';
        return 'var(--text-secondary)';
    };
    container.innerHTML = names.map(name => {
        const s = mcpServers[name];
        const target = (s.url || '').trim();
        const headersInfo = Object.keys(s.headers_masked || {}).map(k => `${k}=${s.headers_masked[k]}`).join(', ');
        return `
        <div class="mcp-server-card" style="border:1px solid var(--border-color);border-radius:6px;padding:8px 10px;margin-bottom:8px;">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
                <strong>${escapeHtml(name)}</strong>
                <span style="display:flex;align-items:center;gap:8px;flex-shrink:0;">
                    <span style="font-size:12px;color:${statusColor(s)};">${statusText(s)}</span>
                    <button class="btn btn-small mcp-expand-btn" data-name="${escapeHtml(name)}">工具清单</button>
                </span>
            </div>
            <div style="font-size:12px;color:var(--text-secondary);margin-top:4px;">
                ${escapeHtml(target || '未配置 URL')}
                ${headersInfo ? '<br>认证: ' + escapeHtml(headersInfo) : ''}
            </div>
            <div style="margin-top:6px;display:flex;align-items:center;gap:8px;">
                <button class="btn btn-small mcp-test-btn" data-name="${escapeHtml(name)}">测试连接</button>
                <button class="btn btn-small mcp-remove-btn" data-name="${escapeHtml(name)}">删除</button>
                <span class="mcp-test-result" style="font-size:12px;"></span>
            </div>
            <div class="mcp-tools-list" style="display:none;margin-top:8px;border-top:1px dashed var(--border-color);padding-top:8px;"></div>
        </div>`;
    }).join('');
    statusEl.textContent = `共 ${names.length} 个服务器`;
    container.querySelectorAll('.mcp-remove-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            delete mcpServers[btn.dataset.name];
            renderMcpServers();
        });
    });
    container.querySelectorAll('.mcp-test-btn').forEach(btn => {
        btn.addEventListener('click', () => testMcpServer(btn.dataset.name, btn));
    });
    container.querySelectorAll('.mcp-expand-btn').forEach(btn => {
        btn.addEventListener('click', () => toggleMcpTools(btn));
    });
}

// 从内存态 server 对象构造待测试配置（保留新填的 headers）
function buildMcpTestConfig(s) {
    return {
        url: s.url || '',
        headers: s.headers || {},
        tool_timeout: s.tool_timeout != null ? s.tool_timeout : 30,
        enabled_tools: s.enabled_tools || ['*']
    };
}

// 测试已保存服务器：优先按 name 用后端保存的配置（含真实 headers）
async function testMcpServer(name, btn) {
    const row = btn.closest('.mcp-server-card');
    const resultEl = row.querySelector('.mcp-test-result');
    resultEl.textContent = '测试中...';
    resultEl.style.color = 'var(--text-secondary)';
    btn.disabled = true;
    const post = (body) => fetch('/api/mcp/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });
    try {
        let data = null;
        let response = await post({ name });
        data = await response.json();
        // 未保存的服务器 → 回退用内存态配置测试
        if (!data || data.status === 'failed' && /未找到已保存/.test(data.error || '')) {
            response = await post({ name, config: buildMcpTestConfig(mcpServers[name] || {}) });
            data = await response.json();
        }
        if (!response.ok) throw new Error('HTTP ' + response.status);
        if (data.status === 'connected') {
            resultEl.textContent = `✓ 连接成功（${data.tool_count} 工具）`;
            resultEl.style.color = '#4caf50';
        } else {
            resultEl.textContent = '✗ 连接失败: ' + (data.error || '未知错误');
            resultEl.style.color = '#e74c3c';
        }
    } catch (e) {
        resultEl.textContent = '✗ 请求失败: ' + e.message;
        resultEl.style.color = '#e74c3c';
    } finally {
        btn.disabled = false;
    }
}

// 展开/收起工具清单（懒渲染，数据来自 /api/mcp/servers 的 tools 字段）
function toggleMcpTools(btn) {
    const row = btn.closest('.mcp-server-card');
    const listEl = row.querySelector('.mcp-tools-list');
    if (listEl.style.display !== 'none') {
        listEl.style.display = 'none';
        btn.textContent = '工具清单';
        return;
    }
    const tools = (mcpServers[btn.dataset.name] || {}).tools || [];
    if (tools.length === 0) {
        listEl.innerHTML = '<div class="form-hint" style="color:var(--text-secondary);">未连接或无工具（连接后可见，或点击"重新连接"刷新）。</div>';
    } else {
        listEl.innerHTML = tools.map(t => `
            <div style="margin-bottom:8px;">
                <code style="font-size:12px;color:var(--primary-color);">${escapeHtml(t.name)}</code>
                ${t.description ? `<div style="font-size:12px;color:var(--text-secondary);margin-top:2px;white-space:pre-wrap;word-break:break-word;">${escapeHtml(t.description)}</div>` : ''}
            </div>
        `).join('');
    }
    listEl.style.display = '';
    btn.textContent = '收起';
}

// 收集 MCP 配置：已加载的服务器保留原 headers（_preserve_headers），
// 表单新增的服务器带完整配置。返回 null 表示名称冲突（阻止保存）。
function collectMcpServers() {
    const result = {};
    for (const [name, s] of Object.entries(mcpServers)) {
        const server = {
            url: s.url || '',
            tool_timeout: s.tool_timeout != null ? s.tool_timeout : 30,
            enabled_tools: s.enabled_tools || ['*']
        };
        if (s._loaded) {
            // 已保存服务器：后端继承原 headers（不覆盖密钥）
            server._preserve_headers = true;
        } else {
            // 本次会话新增的服务器：带上表单里填写的真实 headers
            server.headers = s.headers || {};
        }
        result[name] = server;
    }
    const name = document.getElementById('mcp-name').value.trim();
    if (name) {
        if (result[name]) {
            showToast('服务器名称已存在：' + name);
            return null;
        }
        const headersStr = document.getElementById('mcp-headers').value.trim();
        result[name] = {
            url: document.getElementById('mcp-url').value.trim(),
            headers: mcpParseKv(headersStr),
            tool_timeout: parseInt(document.getElementById('mcp-tool-timeout').value) || 30,
            enabled_tools: (document.getElementById('mcp-enabled-tools').value.trim() || '*').split(',').map(s => s.trim()).filter(Boolean)
        };
    }
    return result;
}

async function reloadMcp() {
    const statusEl = document.getElementById('mcp-status');
    statusEl.textContent = '重连中...';
    try {
        const response = await fetch('/api/mcp/reload', { method: 'POST' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();
        const counts = Object.entries(data.servers || {}).map(([n, s]) =>
            `${n}:${s.status === 'connected' ? '已连接' : s.status}`
        ).join(', ');
        statusEl.textContent = counts || '无服务器';
        showToast('MCP 已重新连接');
        loadMcpServers();  // 刷新工具清单
    } catch (e) {
        statusEl.textContent = '重连失败: ' + e.message;
    }
}

// 测试添加表单中填写的服务器（不保存，使用表单里的真实 headers）
async function testNewMcp() {
    const name = document.getElementById('mcp-name').value.trim();
    const resultEl = document.getElementById('mcp-test-new-result');
    if (!name) { resultEl.textContent = '请先填写服务器名称'; return; }
    const config = {
        url: document.getElementById('mcp-url').value.trim(),
        headers: mcpParseKv(document.getElementById('mcp-headers').value.trim()),
        tool_timeout: parseInt(document.getElementById('mcp-tool-timeout').value) || 30,
        enabled_tools: (document.getElementById('mcp-enabled-tools').value.trim() || '*').split(',').map(s => s.trim()).filter(Boolean)
    };
    resultEl.textContent = '测试中...';
    resultEl.style.color = 'var(--text-secondary)';
    try {
        const response = await fetch('/api/mcp/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, config })
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();
        if (data.status === 'connected') {
            resultEl.textContent = `✓ 连接成功（${data.tool_count} 工具）`;
            resultEl.style.color = '#4caf50';
        } else {
            resultEl.textContent = '✗ 连接失败: ' + (data.error || '未知错误');
            resultEl.style.color = '#e74c3c';
        }
    } catch (e) {
        resultEl.textContent = '✗ 请求失败: ' + e.message;
        resultEl.style.color = '#e74c3c';
    }
}

// 初始化 MCP 表单事件（settings.js 在 body 末尾加载，DOM 已就绪）
(function initMcpSettings() {
    const addBtn = document.getElementById('mcp-add-btn');
    const reloadBtn = document.getElementById('mcp-reload-btn');
    const testNewBtn = document.getElementById('mcp-test-new-btn');
    if (testNewBtn) testNewBtn.addEventListener('click', testNewMcp);
    if (addBtn) addBtn.addEventListener('click', () => {
        const name = document.getElementById('mcp-name').value.trim();
        if (!name) { showToast('请先填写服务器名称'); return; }
        if (mcpServers[name]) { showToast('服务器名称已存在：' + name); return; }
        const mcp = collectMcpServers();
        if (!mcp || !mcp[name]) return;
        // 把表单新增项并入内存态并重渲染（不立即保存）
        mcpServers[name] = {
            url: mcp[name].url, tool_timeout: mcp[name].tool_timeout,
            enabled_tools: mcp[name].enabled_tools, status: 'pending', tool_count: 0,
            headers: mcp[name].headers,  // 保留原值，供卡片"测试连接"回退用
            headers_masked: Object.fromEntries(Object.entries(mcp[name].headers || {}).map(([k, v]) =>
                [k, v.length > 8 ? v.slice(0, 4) + '...' + v.slice(-4) : '***']))
        };
        renderMcpServers();
        ['mcp-name', 'mcp-url', 'mcp-headers'].forEach(id => {
            document.getElementById(id).value = '';
        });
        showToast('已添加（保存后生效）');
    });
    if (reloadBtn) reloadBtn.addEventListener('click', reloadMcp);
})();

// ─── 接入端账号卡片（QQ / 微信共用） ─────────────────────────────────
// 多账号（设计 §8）：每个接入端是一个账号列表；每个账号可绑定默认工作空间
// 与锁定开关。工作区下拉在 openSettings 时取一次、所有卡片复用。

const ACCOUNT_CARD_STYLE =
    'border:1px solid var(--border-color,#ddd);border-radius:6px;padding:10px 14px;margin:10px 0;';

let settingsWorkspaceOptions = [];

async function loadSettingsWorkspaceOptions() {
    try {
        const resp = await fetch('/api/workspaces');
        const data = await resp.json();
        settingsWorkspaceOptions = (data.workspaces || [])
            .filter(ws => !ws.system)
            .map(ws => ({ uuid: ws.uuid, name: ws.name }));
    } catch (e) {
        settingsWorkspaceOptions = [];
    }
}

function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function newAccountId() {
    return Date.now().toString(16).slice(-6) + Math.random().toString(16).slice(2, 6);
}

function workspaceOptionsHtml(selected) {
    const opts = ['<option value="">（跟随默认）</option>'];
    settingsWorkspaceOptions.forEach(ws => {
        const sel = ws.uuid === selected ? ' selected' : '';
        opts.push(`<option value="${escapeHtml(ws.uuid)}"${sel}>${escapeHtml(ws.name)}</option>`);
    });
    return opts.join('');
}

function accountHeaderHtml(acc) {
    return `
        <div class="form-group" style="display:flex;gap:12px;align-items:center;margin-bottom:8px;">
            <input class="acc-label" placeholder="账号备注（仅用于区分）"
                   value="${escapeHtml(acc.label || '')}" style="flex:1;">
            <label style="white-space:nowrap;margin:0;">
                <input type="checkbox" class="acc-enabled" ${acc.enabled ? 'checked' : ''}> 启用
            </label>
            <button type="button" class="acc-delete btn btn-small">删除</button>
        </div>`;
}

function workspaceFieldsHtml(acc) {
    return `
        <div class="form-group">
            <label>默认工作空间:</label>
            <select class="acc-workspace">${workspaceOptionsHtml(acc.default_workspace_uuid || '')}</select>
            <small class="form-hint">该账号的对话默认落在这个工作区；不同账号配不同工作区可隔离记忆</small>
        </div>
        <div class="form-group">
            <label>锁定工作空间:</label>
            <input type="checkbox" class="acc-locked" ${acc.workspace_locked ? 'checked' : ''}>
            <small class="form-hint">锁定后该账号的 /w 不可用，需在此解锁；需先选择默认工作空间</small>
        </div>`;
}

function bindAccountCommon(card, deleteConfirmText) {
    const wsSel = card.querySelector('.acc-workspace');
    const lockCb = card.querySelector('.acc-locked');
    const syncLock = () => { lockCb.disabled = !wsSel.value; };
    wsSel.addEventListener('change', syncLock);
    syncLock();
    card.querySelector('.acc-delete').addEventListener('click', () => {
        if (confirm(deleteConfirmText ||
            '删除该账号？保存后其配置与会话绑定将被移除（登录凭证文件保留，重新添加后可重新扫码）。')) {
            card.remove();
        }
    });
}

// 扫码登录成功或测试连接成功后，自动勾选「启用」，省去用户手动开关
function enableAccountCard(card) {
    const cb = card && card.querySelector('.acc-enabled');
    if (cb) cb.checked = true;
}

// ─── QQ 接入端设置 ───────────────────────────────────────────────────

function buildQQCard(acc) {
    const card = document.createElement('div');
    card.className = 'account-card';
    card.dataset.accountId = acc.id || '';
    card.style.cssText = ACCOUNT_CARD_STYLE;
    const bound = !!(acc.app_id && acc.secret_masked);
    card.innerHTML = `
        ${accountHeaderHtml(acc)}
        <div class="form-group">
            <label>连接状态:</label>
            <span class="acc-login-status form-hint">${bound ? '已连接' : '未连接'}</span>
            <button type="button" class="acc-onboard-btn btn btn-small btn-primary">扫码连接</button>
            <button type="button" class="acc-disconnect-btn btn btn-small">断开连接</button>
        </div>
        ${workspaceFieldsHtml(acc)}`;
    bindAccountCommon(card, '删除该账号？保存后其配置与会话绑定将被移除。');
    card.querySelector('.acc-onboard-btn').addEventListener('click', () => qqStartOnboard(card));
    card.querySelector('.acc-disconnect-btn').addEventListener('click', () => qqDisconnect(card));
    return card;
}

function renderQQAccounts(accounts) {
    const list = document.getElementById('qq-accounts-list');
    list.innerHTML = '';
    (Array.isArray(accounts) ? accounts : []).forEach(acc => list.appendChild(buildQQCard(acc)));
}

function collectQQAccounts() {
    // 凭据（app_id/secret）与消息格式不进表单：凭据由扫码绑定写入、断开连接
    // 清除，消息格式固定默认 markdown；留空即继承已保存值
    return Array.from(document.querySelectorAll('#qq-accounts-list .account-card')).map(card => ({
        id: card.dataset.accountId || '',
        label: card.querySelector('.acc-label').value.trim(),
        enabled: card.querySelector('.acc-enabled').checked,
        default_workspace_uuid: card.querySelector('.acc-workspace').value,
        workspace_locked: card.querySelector('.acc-locked').checked && !card.querySelector('.acc-locked').disabled,
    }));
}

// ─── QQ 扫码连接（凭据由服务端落库，前端只展示状态） ──────────────────

let qqOnboardTimer = null;
let qqOnboardSessionId = '';
let qqOnboardAccountId = '';
let qqOnboardCard = null;

function stopQqOnboardPolling() {
    if (qqOnboardTimer) {
        clearInterval(qqOnboardTimer);
        qqOnboardTimer = null;
    }
}

function hideQqOnboardQr() {
    document.getElementById('qq-onboard-qr-wrap').style.display = 'none';
    document.getElementById('qq-onboard-qr-img').style.display = 'none';
    document.getElementById('qq-onboard-qr-fallback').style.display = 'none';
    document.getElementById('qq-onboard-msg').textContent = '';
}

function renderQqOnboardQr(data) {
    document.getElementById('qq-onboard-qr-wrap').style.display = '';
    document.getElementById('qq-onboard-msg').textContent = data.message || '';

    const img = document.getElementById('qq-onboard-qr-img');
    const fallback = document.getElementById('qq-onboard-qr-fallback');
    if (data.qr_png_data_url) {
        img.src = data.qr_png_data_url;
        img.style.display = '';
        fallback.style.display = 'none';
    } else {
        // 服务端没装 qrcode → 退化成展示原始内容串
        img.style.display = 'none';
        fallback.style.display = '';
        fallback.textContent = data.qr_content || '';
    }
}

function closeQqOnboardModal(cancel = true) {
    stopQqOnboardPolling();
    const sid = qqOnboardSessionId;
    const accountId = qqOnboardAccountId;
    qqOnboardSessionId = '';
    qqOnboardAccountId = '';
    qqOnboardCard = null;
    hideQqOnboardQr();
    document.getElementById('qq-onboard-modal').style.display = 'none';
    if (cancel && sid && accountId) {
        // 尽早丢弃服务端的扫码会话（服务端另有 TTL 兜底）
        fetch(`/api/qq/accounts/${encodeURIComponent(accountId)}/onboard/cancel?session_id=${encodeURIComponent(sid)}`,
              { method: 'POST' }).catch(() => {});
    }
}

async function qqStartOnboard(card) {
    const accountId = card.dataset.accountId || '';
    if (!accountId) {
        showToast('该账号缺少 ID，请先保存后重试');
        return;
    }
    stopQqOnboardPolling();
    qqOnboardSessionId = '';
    qqOnboardAccountId = accountId;
    qqOnboardCard = card;
    hideQqOnboardQr();
    document.getElementById('qq-onboard-modal').style.display = 'flex';
    document.getElementById('qq-onboard-msg').textContent = '正在获取二维码…';
    try {
        const resp = await fetch(
            `/api/qq/accounts/${encodeURIComponent(accountId)}/onboard/start`,
            { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || '取二维码失败');
        qqOnboardSessionId = data.session_id;
        renderQqOnboardQr(data);
        qqOnboardTimer = setInterval(pollQqOnboard, data.interval_ms || 2000);
    } catch (e) {
        // 弹框保持打开，失败原因留在上面，用户看完手动关闭
        document.getElementById('qq-onboard-msg').textContent = '✗ ' + e.message;
    }
}

async function pollQqOnboard() {
    if (!qqOnboardSessionId || !qqOnboardAccountId) return;
    try {
        const resp = await fetch(
            `/api/qq/accounts/${encodeURIComponent(qqOnboardAccountId)}/onboard/status` +
            `?session_id=${encodeURIComponent(qqOnboardSessionId)}`);
        const data = await resp.json();
        if (data.status === 'pending') return;
        stopQqOnboardPolling();
        qqOnboardSessionId = '';
        const card = qqOnboardCard;
        qqOnboardCard = null;
        if (data.status === 'succeeded') {
            closeQqOnboardModal(false);
            enableAccountCard(card);
            await refreshQQStatus();
            showToast(data.message || '机器人已连接');
        } else {
            // 失败/过期：弹框留着展示原因，用户手动关闭
            document.getElementById('qq-onboard-msg').textContent =
                '✗ ' + (data.message || '绑定失败');
        }
    } catch (e) {
        // 网络抖动：不清会话，下一轮继续轮询
    }
}

async function refreshQQStatus() {
    try {
        const resp = await fetch('/api/qq/status');
        const data = await resp.json();
        const byId = {};
        (data.accounts || []).forEach(a => { byId[a.id] = a; });
        document.querySelectorAll('#qq-accounts-list .account-card').forEach(card => {
            const acc = byId[card.dataset.accountId];
            if (!acc) return;
            const parts = [acc.bound ? '已连接' : '未连接'];
            if (acc.bound && acc.app_id) parts.push(`机器人 ${acc.app_id}`);
            if (acc.running) parts.push('运行中');
            card.querySelector('.acc-login-status').textContent = parts.join(' · ');
        });
    } catch (e) {
        // 状态刷新失败不打扰用户
    }
}

async function qqDisconnect(card) {
    const accountId = card.dataset.accountId || '';
    try {
        const resp = await fetch(
            `/api/qq/accounts/${encodeURIComponent(accountId)}/disconnect`, { method: 'POST' });
        const data = await resp.json();
        showToast(data.message || '已断开连接');
        await refreshQQStatus();
    } catch (e) {
        showToast('断开连接失败：' + e.message);
    }
}

// ─── 微信接入端设置 ──────────────────────────────────────────────────

let wechatPollTimer = null;
let wechatSessionId = '';
let wechatLoginAccountId = '';

function buildWechatCard(acc) {
    const card = document.createElement('div');
    card.className = 'account-card';
    card.dataset.accountId = acc.id || '';
    card.style.cssText = ACCOUNT_CARD_STYLE;
    const parts = [acc.logged_in ? '已连接' : '未连接'];
    if (acc.logged_in && acc.ilink_user_id) parts.push(`账号 ${acc.ilink_user_id}`);
    if (acc.running) parts.push('运行中');
    card.innerHTML = `
        ${accountHeaderHtml(acc)}
        <div class="form-group">
            <label>连接状态:</label>
            <span class="acc-login-status form-hint">${escapeHtml(parts.join(' · '))}</span>
            <button type="button" class="acc-login-btn btn btn-small btn-primary">扫码连接</button>
            <button type="button" class="acc-logout-btn btn btn-small">断开连接</button>
        </div>
        ${workspaceFieldsHtml(acc)}`;
    bindAccountCommon(card);
    card.querySelector('.acc-login-btn').addEventListener('click', () => wechatStartLogin(card));
    card.querySelector('.acc-logout-btn').addEventListener('click', () => wechatLogout(card));
    return card;
}

function renderWechatAccounts(accounts) {
    const list = document.getElementById('wechat-accounts-list');
    list.innerHTML = '';
    (Array.isArray(accounts) ? accounts : []).forEach(acc => list.appendChild(buildWechatCard(acc)));
}

function collectWechatAccounts() {
    return Array.from(document.querySelectorAll('#wechat-accounts-list .account-card')).map(card => ({
        id: card.dataset.accountId || '',
        label: card.querySelector('.acc-label').value.trim(),
        enabled: card.querySelector('.acc-enabled').checked,
        default_workspace_uuid: card.querySelector('.acc-workspace').value,
        workspace_locked: card.querySelector('.acc-locked').checked && !card.querySelector('.acc-locked').disabled,
    }));
}

async function refreshWechatStatus() {
    try {
        const resp = await fetch('/api/wechat/status');
        const data = await resp.json();
        const byId = {};
        (data.accounts || []).forEach(a => { byId[a.id] = a; });
        document.querySelectorAll('#wechat-accounts-list .account-card').forEach(card => {
            const acc = byId[card.dataset.accountId];
            if (!acc) return;
            const parts = [acc.logged_in ? '已连接' : '未连接'];
            if (acc.logged_in && acc.ilink_user_id) parts.push(`账号 ${acc.ilink_user_id}`);
            if (acc.running) parts.push('运行中');
            card.querySelector('.acc-login-status').textContent = parts.join(' · ');
        });
    } catch (e) {
        // 状态刷新失败不打扰用户
    }
}

function hideWechatQr() {
    document.getElementById('wechat-qr-wrap').style.display = 'none';
    document.getElementById('wechat-verify-wrap').style.display = 'none';
    document.getElementById('wechat-qr-img').style.display = 'none';
    document.getElementById('wechat-qr-fallback').style.display = 'none';
    document.getElementById('wechat-login-msg').textContent = '';
}

function stopWechatPolling() {
    if (wechatPollTimer) {
        clearInterval(wechatPollTimer);
        wechatPollTimer = null;
    }
}

function renderWechatQr(data) {
    document.getElementById('wechat-qr-wrap').style.display = '';
    document.getElementById('wechat-login-msg').textContent = data.message || '';

    const img = document.getElementById('wechat-qr-img');
    const fallback = document.getElementById('wechat-qr-fallback');
    if (data.qr_png_data_url) {
        img.src = data.qr_png_data_url;
        img.style.display = '';
        fallback.style.display = 'none';
    } else {
        // 服务端没装 qrcode → 退化成展示原始内容串
        img.style.display = 'none';
        fallback.style.display = '';
        fallback.textContent = data.qr_content || '';
    }

    const needCode = data.challenge === 'verify_code';
    document.getElementById('wechat-verify-wrap').style.display = needCode ? '' : 'none';
    if (needCode) document.getElementById('wechat-verify-code').focus();
}

function openWechatLoginModal() {
    document.getElementById('wechat-login-modal').style.display = 'flex';
}

function closeWechatLoginModal(cancel = true) {
    stopWechatPolling();
    const sid = wechatSessionId;
    const accountId = wechatLoginAccountId;
    wechatSessionId = '';
    wechatLoginAccountId = '';
    hideWechatQr();
    document.getElementById('wechat-login-modal').style.display = 'none';
    if (cancel && sid && accountId) {
        // 尽早清理服务端的扫码会话（服务端另有 TTL 兜底）
        fetch(`/api/wechat/accounts/${encodeURIComponent(accountId)}/login/cancel?session_id=${encodeURIComponent(sid)}`,
              { method: 'POST' }).catch(() => {});
    }
}

async function wechatStartLogin(card) {
    const accountId = card.dataset.accountId || '';
    stopWechatPolling();
    wechatSessionId = '';
    wechatLoginAccountId = accountId;
    hideWechatQr();
    openWechatLoginModal();
    document.getElementById('wechat-login-msg').textContent = '正在获取二维码…';
    // 总是 force=true：用户点「扫码登录」就是要扫码；而且若服务端那侧凭证已失效，
    // 走「已登录」短路会让人卡在「显示已登录、实际用不了」的状态。
    try {
        const resp = await fetch(`/api/wechat/accounts/${encodeURIComponent(accountId)}/login/start?force=true`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || '取二维码失败');
        if (data.status === 'succeeded') {
            closeWechatLoginModal(false);
            enableAccountCard(card);
            await refreshWechatStatus();
            showToast(data.message || '微信已登录');
            return;
        }
        wechatSessionId = data.session_id;
        renderWechatQr(data);
        wechatPollTimer = setInterval(pollWechatLogin, data.interval_ms || 2000);
    } catch (e) {
        // 弹框保持打开，失败原因留在上面，用户看完手动关闭
        document.getElementById('wechat-login-msg').textContent = '✗ ' + e.message;
    }
}

async function pollWechatLogin(verifyCode) {
    if (!wechatSessionId || !wechatLoginAccountId) return;
    let url = `/api/wechat/accounts/${encodeURIComponent(wechatLoginAccountId)}/login/status?session_id=${encodeURIComponent(wechatSessionId)}`;
    if (verifyCode) url += `&verify_code=${encodeURIComponent(verifyCode)}`;
    try {
        const resp = await fetch(url);
        const data = await resp.json();
        if (data.status === 'pending') {
            renderWechatQr(data);
            return;
        }
        stopWechatPolling();
        wechatSessionId = '';
        if (data.status === 'succeeded') {
            const accountId = wechatLoginAccountId;  // closeWechatLoginModal 会清空它
            closeWechatLoginModal(false);
            const card = document.querySelector(
                `#wechat-accounts-list .account-card[data-account-id="${CSS.escape(accountId)}"]`);
            enableAccountCard(card);
            await refreshWechatStatus();
            showToast(data.message || '微信已连接');
        } else {
            // 失败/过期：弹框留着展示原因，用户手动关闭
            document.getElementById('wechat-login-msg').textContent =
                '✗ ' + (data.message || '登录失败');
        }
    } catch (e) {
        // 网络抖动：不清会话，下一轮继续轮询
    }
}

async function wechatLogout(card) {
    const accountId = card.dataset.accountId || '';
    try {
        const resp = await fetch(`/api/wechat/accounts/${encodeURIComponent(accountId)}/logout`, { method: 'POST' });
        const data = await resp.json();
        showToast(data.message || '已登出');
        await refreshWechatStatus();
    } catch (e) {
        showToast('登出失败：' + e.message);
    }
}

(function initAccountCards() {
    document.addEventListener('DOMContentLoaded', () => {
        const qqAdd = document.getElementById('qq-add-account-btn');
        if (qqAdd) qqAdd.addEventListener('click', () => {
            document.getElementById('qq-accounts-list').appendChild(buildQQCard({ id: newAccountId() }));
        });
        const wechatAdd = document.getElementById('wechat-add-account-btn');
        if (wechatAdd) wechatAdd.addEventListener('click', () => {
            document.getElementById('wechat-accounts-list').appendChild(buildWechatCard({ id: newAccountId() }));
        });
        const verifyBtn = document.getElementById('wechat-verify-btn');
        if (verifyBtn) {
            verifyBtn.addEventListener('click', () => {
                pollWechatLogin(document.getElementById('wechat-verify-code').value.trim());
            });
        }
        // 扫码登录弹框：×、背景与「取消登录」都走完整关闭（顺带取消服务端会话）。
        // 注意 app.js 的通用 data-modal 关闭也会触发一次隐藏，两者叠加无害。
        const loginModal = document.getElementById('wechat-login-modal');
        if (loginModal) {
            document.getElementById('wechat-login-modal-close')
                .addEventListener('click', () => closeWechatLoginModal(true));
            document.getElementById('wechat-login-cancel-btn')
                .addEventListener('click', () => closeWechatLoginModal(true));
            loginModal.querySelector('.modal-backdrop')
                .addEventListener('click', () => closeWechatLoginModal(true));
        }
        // QQ 扫码绑定弹框：同上
        const onboardModal = document.getElementById('qq-onboard-modal');
        if (onboardModal) {
            document.getElementById('qq-onboard-modal-close')
                .addEventListener('click', () => closeQqOnboardModal(true));
            document.getElementById('qq-onboard-cancel-btn')
                .addEventListener('click', () => closeQqOnboardModal(true));
            onboardModal.querySelector('.modal-backdrop')
                .addEventListener('click', () => closeQqOnboardModal(true));
        }
    });
})();
// 打开全局设置弹窗
async function openSettings() {
    document.getElementById('settings-modal').style.display = 'flex';
    document.getElementById('settings-status').textContent = '加载中...';

    // Reset to models tab
    document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('.settings-tab[data-tab="models"]').classList.add('active');
    document.getElementById('tab-models').classList.add('active');

    try {
        const response = await fetch('/api/config');
        const data = await response.json();
        const cfg = data.config || {};

        // ── Providers（模型列表 tab） ──
        renderProviders(cfg.providers || {});
        restoreAgentSelection(cfg.agents || {});

        // ── System parameters ──
        const systemCfg = cfg.system || {};
        document.getElementById('system-pip-mirror').value = systemCfg.pip_mirror || '';
        document.getElementById('system-browser-path').value = systemCfg.browser_path || '';
        document.getElementById('system-search-engine').value = systemCfg.search_engine || 'bing';
        document.getElementById('system-mineru-api-key').value = '';
        document.getElementById('system-allowed-ips').value = (systemCfg.allowed_ips || []).join(', ');
        document.getElementById('system-max-iterations').value = systemCfg.max_iterations || 200;
        document.getElementById('system-max-concurrent-agents').value = systemCfg.max_concurrent_agents || 2;

        // Show MinerU masked key in placeholder
        const mineruInput = document.getElementById('system-mineru-api-key');
        if (systemCfg.mineru_api_key_masked) {
            mineruInput.placeholder = `当前: ${systemCfg.mineru_api_key_masked}（留空保持不变，清空请填写none）`;
        } else {
            mineruInput.placeholder = '留空则使用免费 Agent API（≤10MB/≤20页）';
        }

        document.getElementById('settings-status').textContent = `配置文件: ${data.config_path}`;

        // ── MCP servers ──
        await loadMcpServers();

        // ── 接入端账号列表（工作区下拉取一次，卡片复用） ──
        await loadSettingsWorkspaceOptions();
        renderQQAccounts(cfg.qq && cfg.qq.accounts);
        refreshQQStatus();

        // ── 微信接入端 ──
        renderWechatAccounts(cfg.wechat && cfg.wechat.accounts);
        refreshWechatStatus();  // 配置里没有登录态，从 /api/wechat/status 补齐

        // ── 关于（版本与升级） ──
        loadAboutStatus();
    } catch (error) {
        document.getElementById('settings-status').textContent = '加载失败: ' + error.message;
    }
}

// 保存全局设置
async function saveSettings() {
    const statusEl = document.getElementById('settings-status');
    statusEl.textContent = '保存中...';

    const payload = {};

    // ── Providers（模型列表 tab） ──
    const providersPayload = collectProviders();
    if (providersPayload === null) return;  // 收集失败（JSON 语法错等），状态已提示
    payload.providers = providersPayload;

    // ── Agents（角色 → 模型引用） ──
    const masterRef = document.getElementById('agent-master-model').value;
    if (!masterRef) {
        statusEl.textContent = '✗ Master 模型必选';
        return;
    }
    payload.agents = { master: { model: masterRef } };
    const workerRef = document.getElementById('agent-worker-model').value;
    const liteRef = document.getElementById('agent-lite-model').value;
    if (workerRef) payload.agents.worker = { model: workerRef };
    if (liteRef) payload.agents.lite = { model: liteRef };

    // ── System parameters ──
    const pipMirror = document.getElementById('system-pip-mirror').value.trim();
    const browserPath = document.getElementById('system-browser-path').value.trim();
    const searchEngine = document.getElementById('system-search-engine').value;
    const mineruApiKey = document.getElementById('system-mineru-api-key').value.trim();
    const allowedIpsStr = document.getElementById('system-allowed-ips').value.trim();
    const allowedIps = allowedIpsStr ? allowedIpsStr.split(',').map(ip => ip.trim()).filter(Boolean) : [];
    const maxIterations = document.getElementById('system-max-iterations').value;
    const maxConcurrentAgents = document.getElementById('system-max-concurrent-agents').value;
    payload.system = {
        pip_mirror: pipMirror,
        browser_path: browserPath,
        search_engine: searchEngine,
        allowed_ips: allowedIps,
        max_iterations: parseInt(maxIterations) || 200,
        max_concurrent_agents: parseInt(maxConcurrentAgents) || 2
    };
    // Only include mineru_api_key if user typed a new value; empty means "don't change"
    if (mineruApiKey) {
        payload.system.mineru_api_key = mineruApiKey;
    }

    // ── MCP servers（含表单新增项；已加载的服务器保留原 headers） ──
    const mcpServersPayload = collectMcpServers();
    if (mcpServersPayload === null) {
        statusEl.textContent = '✗ MCP 服务器名称冲突，无法保存';
        return;
    }
    payload.mcp_servers = mcpServersPayload;

    // ── QQ 接入端（账号列表） ──
    payload.qq = { accounts: collectQQAccounts() };

    // ── 微信接入端（账号列表） ──
    payload.wechat = { accounts: collectWechatAccounts() };

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

// 测试某个 provider 下的具体模型连接（btn 在 provider 卡片内，取同卡片字段）
async function testProviderModel(btn) {
    const statusEl = document.getElementById('settings-status');
    statusEl.textContent = '测试连接中...';
    const card = btn.closest('.provider-card');
    const modelKey = btn.dataset.modelKey;
    const keyInput = card.querySelector('.p-model-key[data-model-key="' + modelKey + '"]');
    const nameInput = card.querySelector('.p-model-name[data-model-key="' + modelKey + '"]');
    const apiId = nameInput ? nameInput.value.trim() : '';

    const payload = {
        interface_type: card.querySelector('.p-interface-type').value || 'anthropic',
        auth_type: card.querySelector('.p-auth-type').value || 'auto',
        base_url: card.querySelector('.p-base-url').value.trim(),
        model_name: apiId || (keyInput ? keyInput.value.trim() : modelKey),
        // provider 名：api_key 输入框为空（重开设置后只剩掩码）时，服务端据此取已保存的 key
        provider: card.querySelector('.p-key').value.trim(),
    };
    const apiKey = card.querySelector('.p-api-key').value.trim();
    if (apiKey) payload.api_key = apiKey;
    const headersJson = card.querySelector('.p-headers').value.trim();
    if (headersJson) {
        try { payload.headers = JSON.parse(headersJson); }
        catch (e) { statusEl.textContent = '✗ Headers JSON 语法错误: ' + e.message; return; }
    }

    try {
        const response = await fetch('/api/config/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (data.success) {
            statusEl.textContent = '✓ ' + modelKey + ' 连接成功 (' + payload.interface_type + ')';
        } else {
            statusEl.textContent = '✗ 连接失败: ' + data.message;
        }
    } catch (error) {
        statusEl.textContent = '✗ 测试失败: ' + error.message;
    }
}

// 自动检测网关模型列表（一次性导入：确认后合入 models 表）
async function detectProviderModels(btn) {
    const statusEl = document.getElementById('settings-status');
    statusEl.textContent = '检测模型列表中...';
    const card = btn.closest('.provider-card');

    const payload = {
        interface_type: card.querySelector('.p-interface-type').value || 'anthropic',
        auth_type: card.querySelector('.p-auth-type').value || 'auto',
        base_url: card.querySelector('.p-base-url').value.trim(),
        // provider 名：api_key 输入框为空（重开设置后只剩掩码）时，服务端据此取已保存的 key
        provider: card.querySelector('.p-key').value.trim(),
    };
    const apiKey = card.querySelector('.p-api-key').value.trim();
    if (apiKey) payload.api_key = apiKey;
    const headersJson = card.querySelector('.p-headers').value.trim();
    if (headersJson) {
        try { payload.headers = JSON.parse(headersJson); }
        catch (e) { statusEl.textContent = '✗ Headers JSON 语法错误: ' + e.message; return; }
    }

    try {
        const response = await fetch('/api/config/detect-models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!data.success) {
            statusEl.textContent = '✗ 检测失败（网关可能不支持模型列表端点，请手动添加）: ' + (data.error || '');
            return;
        }
        const existing = new Set(
            Array.from(card.querySelectorAll('.p-model-key')).map(el => el.value.trim())
        );
        const candidates = (data.models || []).filter(function (id) { return !existing.has(id); });
        if (!candidates.length) {
            statusEl.textContent = '✓ 未发现新模型（或全部已存在）';
            return;
        }
        if (!confirm('检测到 ' + candidates.length + ' 个模型（已排除现有）。全部加入该 Provider？\n\n' + candidates.join('\n'))) {
            statusEl.textContent = '已取消导入';
            return;
        }
        const modelsContainer = card.querySelector('.p-models-list');
        const iface = card.querySelector('.p-interface-type').value;
        candidates.forEach(function (id) {
            modelsContainer.insertAdjacentHTML('beforeend', renderModelRow(id, {}, iface));
        });
        statusEl.textContent = '✓ 已加入 ' + candidates.length + ' 个模型（保存后生效）';
    } catch (error) {
        statusEl.textContent = '✗ 检测失败: ' + error.message;
    }
}

// ===== Providers 渲染与收集（模型列表 tab）=====

function renderReasoningOptions(interfaceType, selected) {
    const opts = interfaceType === 'openai'
        ? [['default', '缺省（不传）'], ['off', '关闭 (none)'], ['minimal', '最小 (minimal)'], ['low', '低'], ['medium', '中'], ['high', '高']]
        : [['default', '缺省（不传）'], ['off', '关闭'], ['low', '低'], ['medium', '中'], ['high', '高']];
    return opts.map(function (o) {
        return '<option value="' + o[0] + '"' + (o[0] === (selected || 'default') ? ' selected' : '') + '>' + o[1] + '</option>';
    }).join('');
}

function renderModalities(selected) {
    const list = selected && selected.length ? selected : ['text', 'image'];
    const mods = ['text', 'image', 'audio', 'video', 'pdf'];
    return mods.map(function (m) {
        const checked = m === 'text' || list.includes(m);
        const disabled = m === 'text' ? ' disabled' : '';
        return '<label><input type="checkbox" class="p-model-input" value="' + m + '"' + (checked ? ' checked' : '') + disabled + '> ' + m + '</label>';
    }).join('');
}

function toggleProviderKeyVisibility(btn) {
    const input = btn.parentElement.querySelector('.p-api-key');
    if (!input) return;
    if (input.type === 'password') {
        input.type = 'text';
        btn.textContent = '🙈';
    } else {
        input.type = 'password';
        btn.textContent = '👁';
    }
}

function renderModelRow(key, m, iface) {
    m = m || {};
    return '<div class="p-model-row">'
        + '<div class="p-model-row-main">'
        + '<div class="field"><label class="field-label">别名</label>'
        + '<input type="text" class="p-model-key" data-model-key="' + key + '" value="' + key + '" placeholder="别名" title="别名（agents 引用它）"></div>'
        + '<span class="model-arrow">→</span>'
        + '<div class="field"><label class="field-label">模型 ID</label>'
        + '<input type="text" class="p-model-name" data-model-key="' + key + '" value="' + String(m.name || '').replace(/"/g, '&quot;') + '" placeholder="API 模型 ID（留空=用别名）"></div>'
        + '<div class="field"><label class="field-label">最大输出</label>'
        + '<input type="number" class="p-model-max-output" data-model-key="' + key + '" value="' + (m.max_output_tokens || 8192) + '" title="max_output_tokens：单次响应最大输出 token"></div>'
        + '<div class="field"><label class="field-label">上下文窗口</label>'
        + '<input type="number" class="p-model-max-context" data-model-key="' + key + '" value="' + (m.max_context_tokens || 128000) + '" title="max_context_tokens：上下文窗口大小"></div>'
        + '<button type="button" class="btn-outline p-model-test" data-model-key="' + key + '">▶ 测试</button>'
        + '<button type="button" class="btn-outline-danger p-model-remove" data-model-key="' + key + '" title="删除模型">✕</button>'
        + '</div>'
        + '<div class="p-model-row-opts">'
        + '<label>类型: <select class="p-model-type" data-model-key="' + key + '">'
        + '<option value="reasoning"' + ((m.model_type || 'reasoning') === 'reasoning' ? ' selected' : '') + '>reasoning</option>'
        + '<option value="chat"' + (m.model_type === 'chat' ? ' selected' : '') + '>chat</option>'
        + '</select></label>'
        + '<label>思考: <select class="p-model-reasoning" data-model-key="' + key + '">' + renderReasoningOptions(iface || 'anthropic', m.reasoning) + '</select></label>'
        + '<span class="modality-group">输入模态: ' + renderModalities(m.input) + '</span>'
        + '</div></div>';
}

function renderProviderCard(key, p) {
    p = p || {};
    const configured = Boolean(p.api_key_masked || p.api_key);
    const status = '<span class="provider-status" data-masked="' + (p.api_key_masked ? 1 : 0) + '">'
        + '<span class="status-dot ' + (configured ? 'ok' : 'pending') + '"></span>'
        + (configured ? '已配置' : '未配置') + '</span>';
    const masked = p.api_key_masked
        ? '<div class="apikey-hint" title="留空保持不变">当前: ' + p.api_key_masked + '</div>'
        : '';
    const models = p.models && Object.keys(p.models).length ? p.models : { default: {} };
    const modelRows = Object.keys(models).map(function (mk) {
        return renderModelRow(mk, models[mk], p.interface_type);
    }).join('');
    const esc = function (s) { return String(s).replace(/"/g, '&quot;'); };
    return '<div class="provider-card">'
        + '<div class="provider-header">'
        + '<div class="provider-name-row">'
        + '<label class="field-label">模型提供商</label>'
        + '<input type="text" class="provider-key-input p-key" value="' + esc(key) + '" placeholder="模型提供商名称">'
        + status
        + '</div>'
        + '<span class="spacer"></span>'
        + '<button type="button" class="btn-outline p-detect">🔍 自动检测模型</button>'
        + '<button type="button" class="btn-outline-danger p-remove">🗑 删除</button>'
        + '</div>'
        + '<div class="provider-body">'
        + '<div class="field-grid">'
        + '<div class="field"><label class="field-label">协议</label><select class="p-interface-type">'
        + '<option value="anthropic"' + ((p.interface_type || 'anthropic') === 'anthropic' ? ' selected' : '') + '>Anthropic</option>'
        + '<option value="openai"' + (p.interface_type === 'openai' ? ' selected' : '') + '>OpenAI</option>'
        + '</select></div>'
        + '<div class="field"><label class="field-label">认证</label><select class="p-auth-type">'
        + '<option value="auto"' + ((p.auth_type || 'auto') === 'auto' ? ' selected' : '') + '>自动</option>'
        + '<option value="bearer"' + (p.auth_type === 'bearer' ? ' selected' : '') + '>Bearer</option>'
        + '<option value="x-api-key"' + (p.auth_type === 'x-api-key' ? ' selected' : '') + '>X-API-Key</option>'
        + '<option value="none"' + (p.auth_type === 'none' ? ' selected' : '') + '>无</option>'
        + '</select></div>'
        + '<div class="field"><label class="field-label">Base URL</label><input type="text" class="p-base-url" value="' + esc(p.base_url || '') + '" placeholder="https://api.anthropic.com"></div>'
        + '</div>'
        + '<div class="field-grid keys-row">'
        + '<div class="field">'
        + '<label class="field-label">API Key</label>'
        + '<div class="apikey-row">'
        + '<div class="apikey-group">'
        + '<input type="password" class="p-api-key" placeholder="sk-...（留空保持不变）">'
        + '<button type="button" class="toggle-password" onclick="toggleProviderKeyVisibility(this)">👁</button>'
        + '</div>'
        + masked
        + '</div>'
        + '</div>'
        + '<div class="field"><label class="field-label">后端服务器</label><select class="p-backend">'
        + '<option value="custom"' + ((p.backend || 'custom') === 'custom' ? ' selected' : '') + '>自定义</option>'
        + '<option value="codex"' + (p.backend === 'codex' ? ' selected' : '') + '>Codex</option>'
        + '<option value="claude-code"' + (p.backend === 'claude-code' ? ' selected' : '') + '>Claude Code</option>'
        + '<option value="opencode"' + (p.backend === 'opencode' ? ' selected' : '') + '>OpenCode</option>'
        + '<option value="openrouter"' + (p.backend === 'openrouter' ? ' selected' : '') + '>OpenRouter</option>'
        + '<option value="litellm"' + (p.backend === 'litellm' ? ' selected' : '') + '>LiteLLM</option>'
        + '</select></div>'
        + '</div>'
        + '<details class="p-headers-panel">'
        + '<summary>自定义请求参数 / Headers</summary>'
        + '<textarea class="p-headers" placeholder="JSON 对象，值支持 {session_id} 占位符，如 {\"X-Session-ID\": \"{session_id}\"}">' + JSON.stringify(p.headers || {}).replace(/</g, '&lt;') + '</textarea>'
        + '</details>'
        + '<div class="models-section">'
        + '<div class="models-section-title">☰ 模型配置</div>'
        + '<div class="p-models-list">' + modelRows + '</div>'
        + '<div class="provider-add-row"><button type="button" class="btn-outline p-model-add">+ 新增模型</button></div>'
        + '</div>'
        + '</div>'
        + '</div>';
}

function renderProviders(providers) {
    const list = document.getElementById('providers-list');
    const entries = Object.keys(providers || {})
        .filter(function (key) { return key !== '_comment'; })
        .map(function (key) { return [key, providers[key] || {}]; });
    list.innerHTML = entries.map(function (e) { return renderProviderCard(e[0], e[1]); }).join('');
    fillAgentSelects(collectAllRefs(providers));
}

// 收集页面上全部模型引用（含未保存的新卡片），填充 agents 三个下拉
function collectAllRefs(providersArg) {
    const refs = [];
    document.querySelectorAll('.provider-card').forEach(function (card) {
        const pkey = card.querySelector('.p-key').value.trim();
        if (!pkey) return;
        card.querySelectorAll('.p-model-key').forEach(function (el) {
            const mkey = el.value.trim();
            if (mkey) refs.push(pkey + '/' + mkey);
        });
    });
    if (!refs.length && providersArg) {
        Object.keys(providersArg).forEach(function (pkey) {
            const p = providersArg[pkey] || {};
            Object.keys(p.models || {}).forEach(function (mkey) { refs.push(pkey + '/' + mkey); });
        });
    }
    return refs;
}

function fillAgentSelects(refs) {
    const master = document.getElementById('agent-master-model');
    const worker = document.getElementById('agent-worker-model');
    const lite = document.getElementById('agent-lite-model');
    const options = refs.map(function (r) { return '<option value="' + r + '">' + r + '</option>'; }).join('');
    [master, worker, lite].forEach(function (sel) {
        const current = sel.value;
        sel.innerHTML = (sel !== master ? '<option value="">（与 Master 一致）</option>' : '') + options;
        if (current && refs.includes(current)) sel.value = current;
    });
}

function restoreAgentSelection(agents) {
    const a = agents || {};
    if (a.master && a.master.model) document.getElementById('agent-master-model').value = a.master.model;
    if (a.worker && a.worker.model) document.getElementById('agent-worker-model').value = a.worker.model;
    if (a.lite && a.lite.model) document.getElementById('agent-lite-model').value = a.lite.model;
}

function collectProviders() {
    const statusEl = document.getElementById('settings-status');
    const providers = {};
    let ok = true;
    document.querySelectorAll('.provider-card').forEach(function (card) {
        if (!ok) return;
        const key = card.querySelector('.p-key').value.trim();
        if (!key) { statusEl.textContent = '✗ Provider 名称不能为空'; ok = false; return; }
        if (providers[key]) { statusEl.textContent = '✗ Provider 名称冲突: ' + key; ok = false; return; }
        let headers = {};
        const headersJson = card.querySelector('.p-headers').value.trim();
        if (headersJson) {
            try { headers = JSON.parse(headersJson); }
            catch (e) { statusEl.textContent = '✗ ' + key + ' 的 Headers JSON 语法错误: ' + e.message; ok = false; return; }
        }
        const models = {};
        card.querySelectorAll('.p-model-row').forEach(function (row) {
            const mkey = row.querySelector('.p-model-key').value.trim();
            if (!mkey || models[mkey]) return;
            const spec = {};
            const name = row.querySelector('.p-model-name').value.trim();
            if (name) spec.name = name;
            spec.max_output_tokens = parseInt(row.querySelector('.p-model-max-output').value) || 8192;
            spec.max_context_tokens = parseInt(row.querySelector('.p-model-max-context').value) || 128000;
            spec.model_type = row.querySelector('.p-model-type').value;
            spec.reasoning = row.querySelector('.p-model-reasoning').value;
            spec.input = Array.prototype.map.call(row.querySelectorAll('.p-model-input:checked'), function (el) { return el.value; });
            if (spec.input.indexOf('text') === -1) spec.input.unshift('text');
            models[mkey] = spec;
        });
        const provider = {
            interface_type: card.querySelector('.p-interface-type').value,
            auth_type: card.querySelector('.p-auth-type').value,
            backend: card.querySelector('.p-backend').value,
            base_url: card.querySelector('.p-base-url').value.trim(),
            headers: headers,
            models: models,
        };
        const apiKey = card.querySelector('.p-api-key').value.trim();
        if (apiKey) provider.api_key = apiKey;  // 留空 = 后端继承已保存值
        providers[key] = provider;
    });
    return ok ? providers : null;
}

// Providers 卡片事件（委托）
(function initProviderSettings() {
    const list = document.getElementById('providers-list');
    if (!list) return;

    const addBtn = document.getElementById('provider-add-btn');
    if (addBtn) {
        addBtn.addEventListener('click', function () {
            list.insertAdjacentHTML('beforeend', renderProviderCard('new-provider', {}));
            fillAgentSelects(collectAllRefs());
        });
    }

    list.addEventListener('click', function (e) {
        const card = e.target.closest('.provider-card');
        if (!card) return;
        if (e.target.classList.contains('p-remove')) {
            card.remove();
            fillAgentSelects(collectAllRefs());
        } else if (e.target.classList.contains('p-model-add')) {
            const iface = card.querySelector('.p-interface-type').value;
            const modelsList = card.querySelector('.p-models-list');
            let key = 'model-1';
            let i = 1;
            const taken = new Set(Array.from(card.querySelectorAll('.p-model-key')).map(function (el) { return el.value.trim(); }));
            while (taken.has(key)) { i += 1; key = 'model-' + i; }
            modelsList.insertAdjacentHTML('beforeend', renderModelRow(key, {}, iface));
            fillAgentSelects(collectAllRefs());
        } else if (e.target.classList.contains('p-model-remove')) {
            const key = e.target.dataset.modelKey;
            const row = card.querySelector('.p-model-key[data-model-key="' + key + '"]');
            if (row) row.closest('.p-model-row').remove();
            fillAgentSelects(collectAllRefs());
        } else if (e.target.classList.contains('p-model-test')) {
            testProviderModel(e.target);
        } else if (e.target.classList.contains('p-detect')) {
            detectProviderModels(e.target);
        }
    });

    // API Key 输入 → 更新状态点
    list.addEventListener('input', function (e) {
        if (!e.target.classList.contains('p-api-key')) return;
        const card = e.target.closest('.provider-card');
        const status = card.querySelector('.provider-status');
        if (!status) return;
        const configured = Boolean(e.target.value.trim()) || status.dataset.masked === '1';
        status.innerHTML = '<span class="status-dot ' + (configured ? 'ok' : 'pending') + '"></span>'
            + (configured ? '已配置' : '未配置');
    });

    // interface_type 变化 → 重渲染各模型行的思考下拉（选项集按协议）
    list.addEventListener('change', function (e) {
        if (!e.target.classList.contains('p-interface-type')) return;
        const card = e.target.closest('.provider-card');
        const iface = e.target.value;
        card.querySelectorAll('.p-model-row').forEach(function (row) {
            const sel = row.querySelector('.p-model-reasoning');
            const current = sel.value;
            sel.innerHTML = renderReasoningOptions(iface, current);
        });
    });
})();

// ─── 关于 Tab：版本与升级 ─────────────────────────────────────────────
// 只负责检查版本和下载升级包到脚本目录；解压覆盖统一在服务重启时进行。

let updatePollTimer = null;
// 轮询间隔 10 秒；上限 15 分钟（后台最坏情况：4 个镜像 × 120s 超时 + 传输）
const UPDATE_POLL_INTERVAL_MS = 10000;
const UPDATE_POLL_MAX_MS = 15 * 60 * 1000;

async function loadAboutStatus() {
    const statusEl = document.getElementById('about-update-status');
    if (!statusEl) return;
    try {
        const resp = await fetch('/api/update/status');
        const data = await resp.json();
        document.getElementById('about-local-version').textContent = data.local_version || '-';
        const zipEl = document.getElementById('about-zip-status');
        zipEl.textContent = (data.zip_present && data.zip_version)
            ? `（升级包 ${data.zip_version} 已就绪，重启服务后自动应用）` : '';
        if (data.downloading) {
            statusEl.textContent = '正在下载升级包...';
            pollDownloadStatus();
        } else if (data.last_result && !data.last_result.success) {
            // 自动下载的失败也在这里可见（结果由 core.updater 统一记录）
            statusEl.textContent = '✗ ' + (data.last_result.error || '上次下载失败');
        }
    } catch (e) {
        statusEl.textContent = '✗ 加载升级状态失败: ' + e.message;
    }
}

async function checkForUpdate() {
    const statusEl = document.getElementById('about-update-status');
    statusEl.textContent = '正在检查更新...';
    try {
        const resp = await fetch('/api/update/check');
        const data = await resp.json();
        if (data.error) {
            document.getElementById('about-remote-version').textContent = '检查失败';
            statusEl.textContent = '✗ ' + data.error;
            return;
        }
        document.getElementById('about-remote-version').textContent = data.remote_version;
        document.getElementById('btn-download-update').style.display = data.has_update ? '' : 'none';
        statusEl.textContent = data.has_update
            ? `发现新版本 ${data.remote_version}（当前 ${data.local_version}）`
            : '已是最新版本';
    } catch (e) {
        statusEl.textContent = '✗ 检查更新失败: ' + e.message;
    }
}

async function downloadUpdate() {
    const statusEl = document.getElementById('about-update-status');
    try {
        const resp = await fetch('/api/update/download', { method: 'POST' });
        const data = await resp.json();
        if (!data.success) {
            statusEl.textContent = '✗ ' + (data.error || '下载失败');
            return;
        }
        statusEl.textContent = '正在下载升级包...';
        pollDownloadStatus();
    } catch (e) {
        statusEl.textContent = '✗ 发起下载失败: ' + e.message;
    }
}

function pollDownloadStatus() {
    if (updatePollTimer) return;
    const startedAt = Date.now();
    updatePollTimer = setInterval(async () => {
        try {
            const resp = await fetch('/api/update/status');
            const data = await resp.json();
            // 是否结束只看 downloading：不能用 last_result 为空当「尚未结束」，
            // 否则自动下载失败（没有结果写入口）后会无限轮询。
            if (data.downloading && Date.now() - startedAt < UPDATE_POLL_MAX_MS) return;
            clearInterval(updatePollTimer);
            updatePollTimer = null;
            const statusEl = document.getElementById('about-update-status');
            if (data.downloading) {
                // 超时兜底：后台仍在下载但轮询已达上限，停止轮询并提示
                statusEl.textContent = '⚠ 下载耗时过长，仍在后台进行；稍后重新打开本页查看结果';
                return;
            }
            const result = data.last_result;
            if (result && result.success) {
                statusEl.textContent = '✓ ' + (result.message || '升级包已下载');
                loadAboutStatus();  // 刷新升级包就绪提示
            } else if (result) {
                statusEl.textContent = '✗ ' + (result.error || '下载失败');
            } else {
                // 无结果记录（异常路径兜底）：刷新整体状态并停止轮询
                loadAboutStatus();
            }
        } catch (e) {
            // 网络抖动：下一轮继续（受上面的时间上限约束）
        }
    }, UPDATE_POLL_INTERVAL_MS);
}

(function initAboutSettings() {
    document.addEventListener('DOMContentLoaded', () => {
        const checkBtn = document.getElementById('btn-check-update');
        const downloadBtn = document.getElementById('btn-download-update');
        if (checkBtn) checkBtn.addEventListener('click', checkForUpdate);
        if (downloadBtn) downloadBtn.addEventListener('click', downloadUpdate);
        // 切到「关于」页签时刷新状态
        document.querySelectorAll('.settings-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                if (tab.dataset.tab === 'about') loadAboutStatus();
            });
        });
    });
})();
