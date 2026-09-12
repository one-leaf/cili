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
function loadRoleModel(prefix, cfg, masterKeyMasked) {
    document.getElementById(`${prefix}-api-key`).value = '';
    document.getElementById(`${prefix}-base-url`).value = cfg.base_url || '';
    document.getElementById(`${prefix}-model`).value = cfg.name || '';
    document.getElementById(`${prefix}-interface-type`).value = cfg.interface_type || '';
    document.getElementById(`${prefix}-max-tokens`).value = cfg.max_tokens || 16384;
    document.getElementById(`${prefix}-max-context-tokens`).value = cfg.max_context_tokens || 256000;
    document.getElementById(`${prefix}-multimodal`).checked = cfg.multimodal !== false;

    const tempVal = cfg.temperature != null ? cfg.temperature : 0.2;
    document.getElementById(`${prefix}-temperature`).value = tempVal;
    document.getElementById(`${prefix}-temperature-value`).textContent = tempVal;
    document.getElementById(`${prefix}-reasoning-effort`).value = cfg.reasoning_effort || '';

    const hint = document.getElementById(`${prefix}-api-key-masked`);
    if (cfg.api_key_masked) {
        hint.textContent = `当前: ${cfg.api_key_masked} (留空保持不变)`;
    } else if (masterKeyMasked) {
        hint.textContent = '未单独配置（将继承主模型的 Key）';
    } else {
        hint.textContent = '未配置';
    }
}

// 收集某个角色（worker/lite）模型配置；name 留空 → 信号后端移除、回退继承 Master
function collectRoleModel(prefix, roleKey, payload) {
    payload[roleKey] = {};
    const apiKey = document.getElementById(`${prefix}-api-key`).value.trim();
    if (apiKey) payload[roleKey].api_key = apiKey;

    const baseUrl = document.getElementById(`${prefix}-base-url`).value.trim();
    if (baseUrl) payload[roleKey].base_url = baseUrl;

    const model = document.getElementById(`${prefix}-model`).value.trim();
    if (model) payload[roleKey].name = model;

    const interfaceType = document.getElementById(`${prefix}-interface-type`).value;
    if (interfaceType) payload[roleKey].interface_type = interfaceType;

    const maxTokens = document.getElementById(`${prefix}-max-tokens`).value;
    if (maxTokens) payload[roleKey].max_tokens = parseInt(maxTokens);

    const maxContextTokens = document.getElementById(`${prefix}-max-context-tokens`).value;
    if (maxContextTokens) payload[roleKey].max_context_tokens = parseInt(maxContextTokens);

    payload[roleKey].multimodal = document.getElementById(`${prefix}-multimodal`).checked;
    payload[roleKey].temperature = parseFloat(document.getElementById(`${prefix}-temperature`).value);
    payload[roleKey].reasoning_effort = document.getElementById(`${prefix}-reasoning-effort`).value;

    if (!payload[roleKey].name) {
        payload[roleKey] = { name: '', api_key: '' };  // Signal to backend to remove (fall back to Master)
    }
}

// ── MCP 服务器管理 ──
// 前端内存态：加载的服务器（保留原 headers，保存时 _preserve_headers）+
// 表单新填的服务器（带完整 headers），一次性随 saveSettings 提交。
let mcpServers = {};  // name -> server config

function mcpTypeChanged() {
    const type = document.getElementById('mcp-type').value;
    const httpLike = (type === 'streamableHttp' || type === 'sse');
    document.getElementById('mcp-stdio-fields').style.display = httpLike ? 'none' : '';
    document.getElementById('mcp-http-fields').style.display = httpLike ? '' : 'none';
}

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
        mcpServers = data.servers || {};
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
        const typeLabel = s.type === 'stdio' ? 'stdio' : s.type === 'streamableHttp' ? 'streamableHttp' : s.type === 'sse' ? 'sse' : '自动';
        const target = (s.command ? s.command + ' ' + (s.args || []).join(' ') : (s.url || '')).trim();
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
                ${escapeHtml(typeLabel)} · ${escapeHtml(target || '未配置目标')}
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
        type: s.type || '',
        command: s.command || '',
        args: s.args || [],
        cwd: s.cwd || '',
        env: s.env || {},
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
            type: s.type || '',
            command: s.command || '',
            args: s.args || [],
            cwd: s.cwd || '',
            url: s.url || '',
            tool_timeout: s.tool_timeout != null ? s.tool_timeout : 30,
            enabled_tools: s.enabled_tools || ['*'],
            _preserve_headers: true,  // 后端据此继承原 headers/env（不覆盖密钥）
            _preserve_env: true
        };
        // env/headers 仅新填时带值；已有服务器不重写
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
            type: document.getElementById('mcp-type').value,
            command: document.getElementById('mcp-command').value.trim(),
            args: document.getElementById('mcp-args').value.trim().split(/\s+/).filter(Boolean),
            cwd: document.getElementById('mcp-cwd').value.trim(),
            env: mcpParseKv(document.getElementById('mcp-env').value.trim()),
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
        type: document.getElementById('mcp-type').value,
        command: document.getElementById('mcp-command').value.trim(),
        args: document.getElementById('mcp-args').value.trim().split(/\s+/).filter(Boolean),
        cwd: document.getElementById('mcp-cwd').value.trim(),
        env: mcpParseKv(document.getElementById('mcp-env').value.trim()),
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
    const typeSelect = document.getElementById('mcp-type');
    const addBtn = document.getElementById('mcp-add-btn');
    const reloadBtn = document.getElementById('mcp-reload-btn');
    const testNewBtn = document.getElementById('mcp-test-new-btn');
    if (typeSelect) typeSelect.addEventListener('change', mcpTypeChanged);
    if (testNewBtn) testNewBtn.addEventListener('click', testNewMcp);
    if (addBtn) addBtn.addEventListener('click', () => {
        const name = document.getElementById('mcp-name').value.trim();
        if (!name) { showToast('请先填写服务器名称'); return; }
        if (mcpServers[name]) { showToast('服务器名称已存在：' + name); return; }
        const mcp = collectMcpServers();
        if (!mcp || !mcp[name]) return;
        // 把表单新增项并入内存态并重渲染（不立即保存）
        mcpServers[name] = {
            type: mcp[name].type, command: mcp[name].command, args: mcp[name].args,
            cwd: mcp[name].cwd, url: mcp[name].url, tool_timeout: mcp[name].tool_timeout,
            enabled_tools: mcp[name].enabled_tools, status: 'pending', tool_count: 0,
            headers: mcp[name].headers,  // 保留原值，供卡片"测试连接"回退用
            headers_masked: Object.fromEntries(Object.entries(mcp[name].headers || {}).map(([k, v]) =>
                [k, v.length > 8 ? v.slice(0, 4) + '...' + v.slice(-4) : '***']))
        };
        renderMcpServers();
        ['mcp-name', 'mcp-command', 'mcp-args', 'mcp-cwd', 'mcp-env', 'mcp-url', 'mcp-headers'].forEach(id => {
            document.getElementById(id).value = '';
        });
        showToast('已添加（保存后生效）');
    });
    if (reloadBtn) reloadBtn.addEventListener('click', reloadMcp);
})();

// 打开全局设置弹窗
async function openSettings() {
    document.getElementById('settings-modal').style.display = 'flex';
    document.getElementById('settings-status').textContent = '加载中...';

    // Reset to Master model tab
    document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('.settings-tab[data-tab="root"]').classList.add('active');
    document.getElementById('tab-root').classList.add('active');

    try {
        const response = await fetch('/api/config');
        const data = await response.json();
        const cfg = data.config || {};
        const modelCfg = cfg.model || {};

        // ── Master model settings ──
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

        // Reasoning effort
        document.getElementById('setting-reasoning-effort').value = modelCfg.reasoning_effort || '';

        // Show masked key
        const maskedHint = document.getElementById('setting-api-key-masked');
        if (modelCfg.api_key_masked) {
            maskedHint.textContent = `当前: ${modelCfg.api_key_masked} (留空保持不变)`;
        } else {
            maskedHint.textContent = '未配置 API Key';
        }

        // ── Worker/Lite model settings（未配置则继承 Master） ──
        loadRoleModel('worker', cfg.worker_model || {}, modelCfg.api_key_masked);
        loadRoleModel('lite', cfg.lite_model || {}, modelCfg.api_key_masked);

        // ── System parameters ──
        const systemCfg = cfg.system || {};
        document.getElementById('system-pip-mirror').value = systemCfg.pip_mirror || '';
        document.getElementById('system-browser-path').value = systemCfg.browser_path || '';
        document.getElementById('system-search-engine').value = systemCfg.search_engine || 'bing';
        document.getElementById('system-mineru-api-key').value = '';
        document.getElementById('system-allowed-ips').value = (systemCfg.allowed_ips || []).join(', ');
        document.getElementById('system-max-iterations').value = systemCfg.max_iterations || 200;
        document.getElementById('system-max-concurrent-agents').value = systemCfg.max_concurrent_agents || 5;

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
    } catch (error) {
        document.getElementById('settings-status').textContent = '加载失败: ' + error.message;
    }
}

// 保存全局设置
async function saveSettings() {
    const statusEl = document.getElementById('settings-status');
    statusEl.textContent = '保存中...';

    const payload = {
        model: {}
    };

    // ── Master model settings ──
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
    payload.model.reasoning_effort = document.getElementById('setting-reasoning-effort').value;

    // ── Worker/Lite model settings（name 留空移除 → 回退继承 Master） ──
    collectRoleModel('worker', 'worker_model', payload);
    collectRoleModel('lite', 'lite_model', payload);

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
        max_concurrent_agents: parseInt(maxConcurrentAgents) || 5
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

// 测试模型连接（'model' = Master；'worker'/'lite' = 子代理角色）
async function testSettings(which = 'model') {
    const statusEl = document.getElementById('settings-status');
    statusEl.textContent = '测试连接中...';

    const isRoleTab = which === 'worker' || which === 'lite';
    const modelLabel = which === 'worker' ? 'Worker 模型' : which === 'lite' ? 'Lite 模型' : 'Agent 模型';

    let payload = {};
    if (isRoleTab) {
        // Test worker/lite model config（未填名称 = 继承主模型，无需测试）
        const model = document.getElementById(`${which}-model`).value.trim();
        if (!model) {
            statusEl.textContent = `✗ ${modelLabel} 未配置独立模型（继承主模型），无需测试`;
            return;
        }
        payload = {
            config: {
                name: model,
                interface_type: document.getElementById(`${which}-interface-type`).value || 'anthropic',
            }
        };
        const baseUrl = document.getElementById(`${which}-base-url`).value.trim();
        if (baseUrl) payload.config.base_url = baseUrl;
        const apiKey = document.getElementById(`${which}-api-key`).value.trim();
        if (apiKey) payload.config.api_key = apiKey;
    } else {
        // Test Master model config
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
            statusEl.textContent = `✓ ${modelLabel}连接成功${interfaceInfo}`;
        } else {
            statusEl.textContent = `✗ 连接失败: ${data.message}`;
        }
    } catch (error) {
        statusEl.textContent = '✗ 测试失败: ' + error.message;
    }
}

// ── 升级功能 ──

// 执行升级
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
