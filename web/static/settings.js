// ── settings.js ── 设置相关逻辑
// 依赖 app.js 全局变量: currentWorkspace, chatInput, composerBtn
// 依赖 utils.js 函数: showToast

// ── 全局设置 ──
let settingsLoading = false;
let settingsLoadError = '';
let teenWorkspaceLoadFailed = false;
// 接入服务商预设（GET /api/config 下发；静态文件与后端版本错配时用内置兜底）
let settingsServerPresets = [];

// 设置弹窗统一使用的线性图标（与 style.css 的 icon-btn 配套）
const MS_ICONS = {
    play: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M8 5l11 7-11 7z"/></svg>',
    trash: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>',
    refresh: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>',
    eye: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
    eyeOff: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/><path d="M1 1l22 22"/></svg>',
};

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
        settingsLoadError = 'MCP 服务器列表加载失败';
    }
    renderMcpServers();
}

function mcpStatusChip(s) {
    if (s.status === 'connected') return `<span class="chip chip-ok"><i></i>已连接 · ${s.tool_count} 工具</span>`;
    if (s.status === 'connecting') return '<span class="chip chip-ask"><i></i>连接中…</span>';
    if (s.status === 'failed') return '<span class="chip chip-deny"><i></i>连接失败</span>';
    return '<span class="chip chip-ask"><i></i>离线</span>';
}

function renderMcpServers() {
    const container = document.getElementById('mcp-server-list');
    const statusEl = document.getElementById('mcp-status');
    const names = Object.keys(mcpServers);
    if (names.length === 0) {
        container.innerHTML = '<div class="settings-empty" style="margin-bottom: 14px;">尚未配置任何 MCP 服务器。</div>';
        statusEl.textContent = '';
        return;
    }
    container.innerHTML = names.map(name => {
        const s = mcpServers[name];
        const target = (s.url || '').trim();
        const headersInfo = Object.keys(s.headers_masked || {}).map(k => `${k}=${s.headers_masked[k]}`).join(', ');
        return `
        <div class="settings-card mcp-server-card">
            <div class="settings-card-head">
                <span class="title">${escapeHtml(name)}</span>
                ${mcpStatusChip(s)}
                <span class="spacer" style="flex:1"></span>
                <button class="icon-btn play mcp-test-btn" data-name="${escapeHtml(name)}" title="测试连接">${MS_ICONS.play}</button>
                <button class="btn-ghost mcp-expand-btn" data-name="${escapeHtml(name)}" style="height:28px;">工具清单</button>
                <button class="icon-btn danger mcp-remove-btn" data-name="${escapeHtml(name)}" title="删除服务器">${MS_ICONS.trash}</button>
            </div>
            <div class="mcp-target">${escapeHtml(target || '未配置 URL')}</div>
            ${headersInfo ? '<div class="mcp-auth">认证：' + escapeHtml(headersInfo) + '</div>' : ''}
            <div class="mcp-test-result"></div>
            <div class="mcp-tools-list" style="display:none;"></div>
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
    resultEl.textContent = '测试中…';
    resultEl.className = 'mcp-test-result';
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
            resultEl.className = 'mcp-test-result ok';
        } else {
            resultEl.textContent = '✗ 连接失败: ' + (data.error || '未知错误');
            resultEl.className = 'mcp-test-result err';
        }
    } catch (e) {
        resultEl.textContent = '✗ 请求失败: ' + e.message;
        resultEl.className = 'mcp-test-result err';
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
        listEl.innerHTML = '<div class="settings-empty">未连接或无工具（连接后可见，或点击「重新连接」刷新）。</div>';
    } else {
        listEl.innerHTML = tools.map(t => `
            <div class="mcp-tool-item">
                <code class="mcp-tool-name">${escapeHtml(t.name)}</code>
                ${t.description ? `<div class="mcp-tool-desc">${escapeHtml(t.description)}</div>` : ''}
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

let settingsWorkspaceOptions = [];

async function loadSettingsWorkspaceOptions() {
    try {
        const resp = await fetch('/api/workspaces');
        const data = await resp.json();
        settingsWorkspaceOptions = sortWorkspacesByName(
            (data.workspaces || []).filter(ws => !ws.system)
        ).map(ws => ({ uuid: ws.uuid, name: ws.name }));
    } catch (e) {
        settingsWorkspaceOptions = [];
        settingsLoadError = '工作区列表加载失败';
    }
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

function accountHeaderHtml(acc, connected, statusText) {
    const chipCls = connected ? 'chip chip-ok' : 'chip chip-ask';
    const label = statusText || (connected ? '已连接' : '未连接');
    return `
        <div class="settings-card-head">
            <input class="acc-title acc-label" placeholder="账号备注（仅用于区分）"
                   value="${escapeHtml(acc.label || '')}">
            <span class="acc-login-status ${chipCls}">${escapeHtml(label)}</span>
            <span class="spacer"></span>
            <label class="switch"><input type="checkbox" class="acc-enabled" ${acc.enabled ? 'checked' : ''}>启用</label>
            <button type="button" class="acc-delete icon-btn danger" title="删除账号">${MS_ICONS.trash}</button>
        </div>`;
}

function workspaceFieldsHtml(acc) {
    const mode = acc.session_mode === 'latest' ? 'latest' : 'dedicated';
    const modeOpts = [
        ['dedicated', '独立会话（每个账号/聊天各一条）'],
        ['latest', '接着最新会话（与 Web 连续）'],
    ].map(([val, text]) =>
        `<option value="${val}"${val === mode ? ' selected' : ''}>${text}</option>`
    ).join('');
    return `
        <div class="setting-row">
            <label>工作区</label>
            <div class="control">
                <div class="inline">
                    <select class="acc-workspace">${workspaceOptionsHtml(acc.default_workspace_uuid || '')}</select>
                    <label class="switch"><input type="checkbox" class="acc-locked" ${acc.workspace_locked ? 'checked' : ''}>锁定</label>
                </div>
                <div class="hint">对话默认落在这个工作区；锁定后 /w 不可用，需在此解锁</div>
            </div>
        </div>
        <div class="setting-row">
            <label>会话</label>
            <div class="control">
                <div class="inline">
                    <select class="acc-session-mode">${modeOpts}</select>
                    <label class="switch"><input type="checkbox" class="acc-session-locked" ${acc.session_locked ? 'checked' : ''}>锁定</label>
                </div>
                <div class="hint">独立会话按账号与聊天标记找回；锁定后不能切换会话（/s、/new、/mode 不可用）</div>
            </div>
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
    card.className = 'account-card settings-card';
    card.dataset.accountId = acc.id || '';
    const bound = !!(acc.app_id && acc.secret_masked);
    card.innerHTML = `
        ${accountHeaderHtml(acc, bound, bound ? '已连接' : '未连接')}
        <div class="setting-row">
            <label>连接</label>
            <div class="control">
                <div class="inline">
                    <button type="button" class="acc-onboard-btn btn-secondary">扫码连接</button>
                    <button type="button" class="acc-disconnect-btn btn-soft">断开连接</button>
                    <span class="hint">凭据由扫码写入，不显示在表单里</span>
                </div>
            </div>
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
        session_mode: card.querySelector('.acc-session-mode').value,
        session_locked: card.querySelector('.acc-session-locked').checked,
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
        fetch(`/api/qq/accounts/${encodeURIComponent(accountId)}/login/cancel?session_id=${encodeURIComponent(sid)}`,
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
            `/api/qq/accounts/${encodeURIComponent(accountId)}/login/start`,
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
            `/api/qq/accounts/${encodeURIComponent(qqOnboardAccountId)}/login/status` +
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
            const connected = !!acc.bound;
            const parts = [connected ? '已连接' : '未连接'];
            if (connected && acc.app_id) parts.push(`机器人 ${acc.app_id}`);
            if (acc.running) parts.push('运行中');
            const el = card.querySelector('.acc-login-status');
            if (!el) return;
            el.textContent = parts.join(' · ');
            el.className = 'acc-login-status chip ' + (connected ? 'chip-ok' : 'chip-ask');
        });
    } catch (e) {
        // 状态刷新失败不打扰用户
    }
}

async function qqDisconnect(card) {
    const accountId = card.dataset.accountId || '';
    try {
        const resp = await fetch(
            `/api/qq/accounts/${encodeURIComponent(accountId)}/logout`, { method: 'POST' });
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
    card.className = 'account-card settings-card';
    card.dataset.accountId = acc.id || '';
    const connected = !!acc.logged_in;
    const parts = [connected ? '已连接' : '未连接'];
    if (connected && acc.ilink_user_id) parts.push(`账号 ${acc.ilink_user_id}`);
    if (acc.running) parts.push('运行中');
    card.innerHTML = `
        ${accountHeaderHtml(acc, connected, parts.join(' · '))}
        <div class="setting-row">
            <label>连接</label>
            <div class="control">
                <div class="inline">
                    <button type="button" class="acc-login-btn btn-secondary">扫码连接</button>
                    <button type="button" class="acc-logout-btn btn-soft">断开连接</button>
                    <span class="hint">凭证来自扫码，按账号存放，不写进设置文件</span>
                </div>
            </div>
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
        session_mode: card.querySelector('.acc-session-mode').value,
        session_locked: card.querySelector('.acc-session-locked').checked,
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
            const connected = !!acc.logged_in;
            const parts = [connected ? '已连接' : '未连接'];
            if (connected && acc.ilink_user_id) parts.push(`账号 ${acc.ilink_user_id}`);
            if (acc.running) parts.push('运行中');
            const el = card.querySelector('.acc-login-status');
            if (!el) return;
            el.textContent = parts.join(' · ');
            el.className = 'acc-login-status chip ' + (connected ? 'chip-ok' : 'chip-ask');
        });
    } catch (e) {
        // 状态刷新失败不打扰用户
    }
}

// ─── 邮件接入端设置 ──────────────────────────────────────────────────

function buildEmailCard(acc) {
    const card = document.createElement('div');
    card.className = 'account-card settings-card';
    card.dataset.accountId = acc.id || '';
    const configured = !!(acc.email_address && acc.imap_host && acc.smtp_host);
    const pwdPlaceholder = acc.password_masked
        ? `当前: ${acc.password_masked}（留空保持不变）`
        : '授权码 / 应用专用密码';
    const senders = Array.isArray(acc.allowed_senders) ? acc.allowed_senders.join('\n') : '';
    const smtpSel = acc.smtp_security === 'starttls' ? ' selected' : '';
    card.innerHTML = `
        ${accountHeaderHtml(acc, configured, configured ? '已配置' : '未配置')}
        <div class="setting-row">
            <label>邮箱</label>
            <div class="control">
                <div class="inline">
                    <input class="acc-email" placeholder="agent@example.com" value="${escapeHtml(acc.email_address || '')}">
                    <button type="button" class="acc-test-btn btn-secondary">测试连接</button>
                    <span class="hint acc-test-result"></span>
                </div>
            </div>
        </div>
        <div class="setting-row">
            <label>IMAP</label>
            <div class="control">
                <div class="inline">
                    <input class="acc-imap-host" placeholder="imap.example.com" value="${escapeHtml(acc.imap_host || '')}">
                    <input class="acc-imap-port" type="number" style="width:80px" placeholder="993" value="${acc.imap_port || 993}">
                    <label class="switch"><input type="checkbox" class="acc-imap-ssl" ${acc.imap_ssl !== false ? 'checked' : ''}>SSL</label>
                </div>
            </div>
        </div>
        <div class="setting-row">
            <label>SMTP</label>
            <div class="control">
                <div class="inline">
                    <input class="acc-smtp-host" placeholder="smtp.example.com" value="${escapeHtml(acc.smtp_host || '')}">
                    <input class="acc-smtp-port" type="number" style="width:80px" placeholder="465" value="${acc.smtp_port || 465}">
                    <select class="acc-smtp-security">
                        <option value="ssl"${smtpSel ? '' : ' selected'}>SSL (465)</option>
                        <option value="starttls"${smtpSel}>STARTTLS (587)</option>
                    </select>
                </div>
            </div>
        </div>
        <div class="setting-row">
            <label>登录</label>
            <div class="control">
                <div class="inline">
                    <input class="acc-username" placeholder="用户名（通常是邮箱地址）" value="${escapeHtml(acc.username || '')}">
                    <input class="acc-password" type="password" placeholder="${escapeHtml(pwdPlaceholder)}">
                </div>
                <div class="hint">密码填授权码/应用专用密码，不是登录密码</div>
            </div>
        </div>
        <div class="setting-row">
            <label>白名单</label>
            <div class="control">
                <textarea class="acc-senders" rows="2" placeholder="每行一个发件邮箱，如：me@example.com">${escapeHtml(senders)}</textarea>
                <div class="hint">只有这些地址的来信会触发回复；其余静默丢弃、不回信。<strong>留空 = 该账号不处理任何来信</strong></div>
            </div>
        </div>
        <div class="acc-email-status hint"></div>
        ${workspaceFieldsHtml(acc)}`;
    bindAccountCommon(card, '删除该邮箱？保存后其配置与会话绑定将被移除。');
    card.querySelector('.acc-test-btn').addEventListener('click', () => testEmailConnection(card));
    return card;
}

function renderEmailAccounts(accounts) {
    const list = document.getElementById('email-accounts-list');
    list.innerHTML = '';
    (Array.isArray(accounts) ? accounts : []).forEach(acc => list.appendChild(buildEmailCard(acc)));
}

function collectEmailCard(card) {
    const email = card.querySelector('.acc-email').value.trim().toLowerCase();
    const senders = card.querySelector('.acc-senders').value
        .split(/[\n,，]/).map(s => s.trim()).filter(Boolean);
    return {
        id: email,
        label: card.querySelector('.acc-label').value.trim(),
        enabled: card.querySelector('.acc-enabled').checked,
        email_address: email,
        imap_host: card.querySelector('.acc-imap-host').value.trim(),
        imap_port: parseInt(card.querySelector('.acc-imap-port').value, 10) || 993,
        imap_ssl: card.querySelector('.acc-imap-ssl').checked,
        smtp_host: card.querySelector('.acc-smtp-host').value.trim(),
        smtp_port: parseInt(card.querySelector('.acc-smtp-port').value, 10) || 465,
        smtp_security: card.querySelector('.acc-smtp-security').value,
        username: card.querySelector('.acc-username').value.trim(),
        password: card.querySelector('.acc-password').value,
        allowed_senders: senders,
        default_workspace_uuid: card.querySelector('.acc-workspace').value,
        workspace_locked: card.querySelector('.acc-locked').checked && !card.querySelector('.acc-locked').disabled,
        session_mode: card.querySelector('.acc-session-mode').value,
        session_locked: card.querySelector('.acc-session-locked').checked,
    };
}

function collectEmailAccounts() {
    return Array.from(document.querySelectorAll('#email-accounts-list .account-card'))
        .map(collectEmailCard);
}

async function testEmailConnection(card) {
    const resultEl = card.querySelector('.acc-test-result');
    resultEl.textContent = '测试中…';
    try {
        const resp = await fetch('/api/config/test-email', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ account: collectEmailCard(card) }),
        });
        const data = await resp.json();
        if (data.ok) {
            resultEl.textContent = '✓ IMAP 与 SMTP 均可用';
            resultEl.style.color = 'var(--ms-ok, #2e7d32)';
        } else {
            resultEl.textContent = `✗ ${data.error || '连接失败'}`;
            resultEl.style.color = 'var(--ms-danger, #c62828)';
            card.querySelector('.acc-email-status').textContent =
                `IMAP: ${data.imap || '-'}　SMTP: ${data.smtp || '-'}`;
        }
    } catch (e) {
        resultEl.textContent = '✗ ' + e.message;
    }
}

async function refreshEmailStatus() {
    try {
        const resp = await fetch('/api/email/status');
        const data = await resp.json();
        const byId = {};
        (data.accounts || []).forEach(a => { byId[a.id] = a; });
        document.querySelectorAll('#email-accounts-list .account-card').forEach(card => {
            const acc = byId[card.dataset.accountId];
            if (!acc) return;
            const parts = [acc.configured ? '已配置' : '未配置'];
            if (acc.running) parts.push('运行中');
            const chip = card.querySelector('.acc-login-status');
            if (chip) {
                chip.textContent = parts.join(' · ');
                chip.className = 'acc-login-status chip ' + (acc.configured ? 'chip-ok' : 'chip-ask');
            }
            const statusEl = card.querySelector('.acc-email-status');
            if (!statusEl) return;
            const lines = [];
            if (acc.last_poll_at) lines.push(`最近轮询：${acc.last_poll_at}`);
            if (acc.last_error) lines.push(`⚠️ ${acc.last_error}`);
            const ignored = acc.ignored_24h || {};
            lines.push(`24h 拦截：${ignored.total || 0} 封`);
            (acc.recent_ignored || []).slice(-3).forEach(r => {
                lines.push(`· ${r.from || '(未知)'}（${r.reason}，${r.at}）`);
            });
            statusEl.textContent = lines.join('　');
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
    if (needCode) {
        // 轮询每 2 秒重绘一次二维码；只在验证码首次出现时抢一次焦点，
        // 否则用户在其他输入框打字会被反复打断。
        const input = document.getElementById('wechat-verify-code');
        if (input.dataset.autoFocused !== '1') {
            input.dataset.autoFocused = '1';
            input.focus();
        }
    }
}

function openWechatLoginModal() {
    document.getElementById('wechat-login-modal').style.display = 'flex';
    const input = document.getElementById('wechat-verify-code');
    if (input) delete input.dataset.autoFocused;
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
        const emailAdd = document.getElementById('email-add-account-btn');
        if (emailAdd) emailAdd.addEventListener('click', () => {
            document.getElementById('email-accounts-list').appendChild(buildEmailCard({ enabled: true }));
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
    settingsLoading = true;
    settingsLoadError = '';
    const saveBtn = document.getElementById('settings-save-btn');
    if (saveBtn) saveBtn.disabled = true;

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
        settingsServerPresets = Array.isArray(data.server_presets) ? data.server_presets : [];
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

        // ── MCP servers ──
        await loadMcpServers();

        // ── 接入端账号列表（工作区下拉取一次，卡片复用） ──
        await loadSettingsWorkspaceOptions();
        renderQQAccounts(cfg.qq && cfg.qq.accounts);
        refreshQQStatus();

        // ── 微信接入端 ──
        renderWechatAccounts(cfg.wechat && cfg.wechat.accounts);
        refreshWechatStatus();  // 配置里没有登录态，从 /api/wechat/status 补齐

        // ── 邮件接入端 ──
        renderEmailAccounts(cfg.email && cfg.email.accounts);
        refreshEmailStatus();   // 运行态与拦截统计从 /api/email/status 补齐

        // ── 关于（版本与升级） ──
        loadAboutStatus();

        if (settingsLoadError) {
            document.getElementById('settings-status').textContent = '✗ 部分设置加载失败：' + settingsLoadError;
        } else {
            document.getElementById('settings-status').textContent = `配置文件: ${data.config_path}`;
        }
    } catch (error) {
        settingsLoadError = error.message || '设置加载失败';
        document.getElementById('settings-status').textContent = '加载失败: ' + settingsLoadError;
    } finally {
        settingsLoading = false;
        if (saveBtn) saveBtn.disabled = false;
    }
}

// 保存全局设置
async function saveSettings() {
    const activeTab = document.querySelector('.settings-tab.active');
    if (activeTab && activeTab.dataset.tab === 'permissions') {
        return savePermissions();
    }
    const statusEl = document.getElementById('settings-status');
    if (settingsLoading) {
        statusEl.textContent = '设置仍在加载，请稍候再保存';
        return;
    }
    if (settingsLoadError) {
        statusEl.textContent = '✗ 设置未完整加载，已阻止保存：' + settingsLoadError;
        return;
    }
    if (teenWorkspaceLoadFailed && document.querySelector('[data-teen-policy]')) {
        statusEl.textContent = '✗ 青少年工作区列表加载失败，已阻止保存';
        return;
    }
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
    const workflowEl = document.getElementById('agent-workflow-model');
    const workflowRef = workflowEl ? workflowEl.value : '';
    if (workerRef) payload.agents.worker = { model: workerRef };
    if (liteRef) payload.agents.lite = { model: liteRef };
    if (workflowRef) payload.agents.workflow = { model: workflowRef };

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

    // ── 邮件接入端（账号列表；未填邮箱地址的卡片不提交） ──
    payload.email = {
        accounts: collectEmailAccounts().filter(a => a.email_address)
    };

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

        // ── 青少年模式策略（面板在解锁视图时随主保存一并提交） ──
        const teenPayload = collectTeenPolicyPayload();
        if (teenPayload) {
            const teenResp = await fetch('/api/teen/policy', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(teenPayload)
            });
            if (!teenResp.ok) {
                const err = await teenResp.json().catch(() => ({}));
                throw new Error(err.detail || '青少年策略保存失败');
            }
            await loadTeenStatus();
            await renderTeenWorkspaceList(teenStatusCache);
        }

        statusEl.textContent = '✓ 保存成功';
        setTimeout(() => {
            document.getElementById('settings-modal').style.display = 'none';
        }, 800);
    } catch (error) {
        statusEl.textContent = '✗ 保存失败: ' + error.message;
    }
}

// 测试某个 provider 下的具体模型连接（结果就地显示在该行，不再写到弹窗页脚）
async function testProviderModel(btn) {
    const card = btn.closest('.provider-card');
    const row = btn.closest('.p-model-row');
    const modelKey = btn.dataset.modelKey;
    const resultEl = row ? row.querySelector('.p-model-result') : null;
    const setResult = function (text, cls) {
        if (!resultEl) return;
        resultEl.hidden = false;
        resultEl.className = 'p-model-result' + (cls ? ' ' + cls : '');
        resultEl.textContent = text;
    };
    const keyInput = card.querySelector('.p-model-key[data-model-key="' + modelKey + '"]');
    const nameInput = card.querySelector('.p-model-name[data-model-key="' + modelKey + '"]');
    const apiId = nameInput ? nameInput.value.trim() : '';

    const apiPathInput = card.querySelector('.p-api-path');
    const payload = {
        protocol: card.querySelector('.p-interface-type').value || 'anthropic',
        auth_type: card.querySelector('.p-auth-type').value || 'auto',
        backend: card.querySelector('.p-server').value || 'custom',
        base_url: card.querySelector('.p-base-url').value.trim(),
        api_path: apiPathInput ? apiPathInput.value.trim() : '',
        model_name: apiId || (keyInput ? keyInput.value.trim() : modelKey),
        // provider 名：api_key 输入框为空（重开设置后只剩掩码）时，服务端据此取已保存的 key
        provider: card.querySelector('.p-key').value.trim(),
    };
    if (payload.auth_type === 'oauth') payload.credential = card.dataset.credential || '';
    const apiKey = card.querySelector('.p-api-key').value.trim();
    if (apiKey) payload.api_key = apiKey;
    const headersJson = card.querySelector('.p-headers').value.trim();
    if (headersJson) {
        try { payload.headers = JSON.parse(headersJson); }
        catch (e) { setResult('✗ Headers JSON 语法错误: ' + e.message, 'err'); return; }
    }

    btn.disabled = true;
    setResult('测试中…', '');
    try {
        const response = await fetch('/api/config/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (data.success) {
            setResult('✓ 连接成功 (' + payload.protocol + ')', 'ok');
        } else {
            setResult('✗ 连接失败: ' + data.message, 'err');
        }
    } catch (error) {
        setResult('✗ 测试失败: ' + error.message, 'err');
    } finally {
        btn.disabled = false;
    }
}

// 自动检测网关模型列表（一次性导入：确认后合入 models 表）
async function detectProviderModels(btn) {
    const statusEl = document.getElementById('settings-status');
    statusEl.textContent = '检测模型列表中...';
    const card = btn.closest('.provider-card');

    const detectPreset = getServerPreset(card.querySelector('.p-server').value);
    const payload = {
        protocol: card.querySelector('.p-interface-type').value || 'anthropic',
        auth_type: card.querySelector('.p-auth-type').value || 'auto',
        backend: card.querySelector('.p-server').value || 'custom',
        base_url: card.querySelector('.p-base-url').value.trim(),
        models_url: detectPreset && detectPreset.models_url ? detectPreset.models_url : '',
        // provider 名：api_key 输入框为空（重开设置后只剩掩码）时，服务端据此取已保存的 key
        provider: card.querySelector('.p-key').value.trim(),
    };
    if (payload.auth_type === 'oauth') payload.credential = card.dataset.credential || '';
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
        updateModelCount(card);
        fillAgentSelects(collectAllRefs());
        statusEl.textContent = '✓ 已加入 ' + candidates.length + ' 个模型（保存后生效）';
    } catch (error) {
        statusEl.textContent = '✗ 检测失败: ' + error.message;
    }
}

// ===== Providers 渲染与收集（模型列表 tab）=====

function renderReasoningOptions(interfaceType, selected) {
    // openai / openai-responses 的 reasoning.effort 取值域相同（含 minimal）
    const openaiFamily = interfaceType === 'openai' || interfaceType === 'openai-responses';
    const opts = openaiFamily
        ? [['default', '跟随服务端（不传）'], ['off', '关闭 (none)'], ['minimal', '最小 (minimal)'], ['low', '低'], ['medium', '中'], ['high', '高']]
        : [['default', '跟随服务端（不传）'], ['off', '关闭'], ['low', '低'], ['medium', '中'], ['high', '高']];
    // 默认档位 medium：新建模型行/旧数据缺省字段时选中「中」
    return opts.map(function (o) {
        return '<option value="' + o[0] + '"' + (o[0] === (selected || 'medium') ? ' selected' : '') + '>' + o[1] + '</option>';
    }).join('');
}

// ===== 接入服务商预设联动 =====

// 前端兜底：后端未下发 server_presets（静态文件与后端版本错配）时至少保留自定义
const PRESET_FALLBACK = [{ key: 'custom', label: '自定义', group: 'direct', default_protocol: null, base_urls: {}, api_paths: {}, auth_type: 'auto', headers: {}, hint: '', models_url: '' }];

function serverPresets() {
    return settingsServerPresets.length ? settingsServerPresets : PRESET_FALLBACK;
}

function getServerPreset(key) {
    return serverPresets().find(function (p) { return p.key === key; }) || null;
}

function serverPresetUrl(preset, iface) {
    return (preset && preset.base_urls && preset.base_urls[iface]) || '';
}

function serverPresetApiPath(preset, iface) {
    return (preset && preset.api_paths && preset.api_paths[iface]) || '';
}

// 任一预设的已知地址 / 路径都算「自动值」（切换服务商时旧预设地址也能被覆盖）
function isKnownPresetUrl(value) {
    if (!value) return false;
    const v = String(value).replace(/\/+$/, '');
    return serverPresets().some(function (p) {
        return Object.keys(p.base_urls || {}).some(function (k) {
            return String(p.base_urls[k]).replace(/\/+$/, '') === v;
        });
    });
}

function isKnownPresetApiPath(value) {
    if (!value) return false;
    return serverPresets().some(function (p) {
        return Object.keys(p.api_paths || {}).some(function (k) { return p.api_paths[k] === value; });
    });
}

function isAutoBaseUrl(card) {
    const input = card.querySelector('.p-base-url');
    if (!input) return true;
    if (input.dataset.urlCustom === '1') return false;
    if (!input.value.trim()) return true;
    return isKnownPresetUrl(input.value.trim());
}

function isAutoApiPath(card) {
    const input = card.querySelector('.p-api-path');
    if (!input) return true;
    if (input.dataset.pathCustom === '1') return false;
    if (!input.value.trim()) return true;
    return isKnownPresetApiPath(input.value.trim());
}

function renderServerOptions(selected) {
    const groups = { plan: [], direct: [] };
    serverPresets().forEach(function (p) {
        (groups[p.group] || groups.direct).push(p);
    });
    return [['plan', '订阅套餐'], ['direct', '直连 / 网关']].map(function (g) {
        if (!groups[g[0]].length) return '';
        return '<optgroup label="' + g[1] + '">' + groups[g[0]].map(function (p) {
            return '<option value="' + escAttr(p.key) + '"' + (p.key === selected ? ' selected' : '') + '>' + escAttr(p.label) + '</option>';
        }).join('') + '</optgroup>';
    }).join('');
}

function refreshReasoningOptions(card) {
    const iface = card.querySelector('.p-interface-type').value || 'anthropic';
    card.querySelectorAll('.p-model-row').forEach(function (row) {
        const sel = row.querySelector('.p-model-reasoning');
        if (!sel) return;
        sel.innerHTML = renderReasoningOptions(iface, sel.value);
    });
}

function updateUrlPlaceholder(card) {
    const input = card.querySelector('.p-base-url');
    if (!input) return;
    const preset = getServerPreset(card.querySelector('.p-server').value);
    const iface = card.querySelector('.p-interface-type').value || 'anthropic';
    input.placeholder = serverPresetUrl(preset, iface) || 'https://api.anthropic.com';
}

// 选择接入服务商 / 切换协议后，按预设填充默认值（手改过的 URL / 路径不覆盖）
function applyPresetDefaults(card) {
    const preset = getServerPreset(card.querySelector('.p-server').value);
    const iface = card.querySelector('.p-interface-type').value || 'anthropic';
    if (!preset) return;
    const urlInput = card.querySelector('.p-base-url');
    if (urlInput && isAutoBaseUrl(card)) {
        urlInput.value = serverPresetUrl(preset, iface);
        urlInput.dataset.urlCustom = '';
    }
    const pathInput = card.querySelector('.p-api-path');
    if (pathInput && isAutoApiPath(card)) {
        pathInput.value = serverPresetApiPath(preset, iface);
        pathInput.dataset.pathCustom = '';
    }
    updateUrlPlaceholder(card);
}

// 选服务商是显式动作：直接套用该预设的协议默认值（认证、URL、路径）；
// 选「自定义」时不改动用户已填的 URL / 路径。
function applyServerSelection(card) {
    const preset = getServerPreset(card.querySelector('.p-server').value);
    if (!preset) return;
    if (preset.auth_type) {
        const authSel = card.querySelector('.p-auth-type');
        if (authSel) authSel.value = preset.auth_type;
    }
    if (preset.key !== 'custom') {
        const iface = card.querySelector('.p-interface-type').value || 'anthropic';
        const urlInput = card.querySelector('.p-base-url');
        if (urlInput) {
            urlInput.value = serverPresetUrl(preset, iface);
            urlInput.dataset.urlCustom = '';
        }
        const pathInput = card.querySelector('.p-api-path');
        if (pathInput) {
            pathInput.value = serverPresetApiPath(preset, iface);
            pathInput.dataset.pathCustom = '';
        }
    }
    updateUrlPlaceholder(card);
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
    const wrap = btn.closest('.key-wrap') || btn.parentElement;
    const input = wrap.querySelector('.p-api-key');
    if (!input) return;
    if (input.type === 'password') {
        input.type = 'text';
        btn.innerHTML = MS_ICONS.eyeOff;
        btn.title = '隐藏';
    } else {
        input.type = 'password';
        btn.innerHTML = MS_ICONS.eye;
        btn.title = '显示';
    }
}

function escAttr(s) { return String(s == null ? '' : s).replace(/"/g, '&quot;'); }

// 模型行：一行值 + 一行元数据；列头只在区块顶部出现一次
function renderModelRow(key, m, iface) {
    m = m || {};
    const type = m.model_type || 'reasoning';
    return '<div class="p-model-row">'
        + '<input type="text" class="p-model-key" data-model-key="' + escAttr(key) + '" value="' + escAttr(key) + '" placeholder="别名" title="别名（agents 引用它）">'
        + '<input type="text" class="p-model-name" data-model-key="' + escAttr(key) + '" value="' + escAttr(m.name) + '" placeholder="API 模型 ID（留空=用别名）">'
        + '<input type="number" class="p-model-max-output" data-model-key="' + escAttr(key) + '" value="' + (m.max_output_tokens || 32768) + '" title="max_output_tokens：单次响应最大输出 token">'
        + '<input type="number" class="p-model-max-context" data-model-key="' + escAttr(key) + '" value="' + (m.max_context_tokens || 200000) + '" title="max_context_tokens：上下文工作预算（达到 80% 触发压缩）">'
        + '<span class="p-model-actions">'
        + '<button type="button" class="icon-btn play p-model-test" data-model-key="' + escAttr(key) + '" title="测试连接">' + MS_ICONS.play + '</button>'
        + '<button type="button" class="icon-btn danger p-model-remove" data-model-key="' + escAttr(key) + '" title="删除模型">' + MS_ICONS.trash + '</button>'
        + '</span>'
        + '<div class="p-model-meta">'
        + '<span class="group">类型'
        + '<select class="p-model-type" data-model-key="' + escAttr(key) + '">'
        + '<option value="reasoning"' + (type === 'reasoning' ? ' selected' : '') + '>reasoning</option>'
        + '<option value="chat"' + (type === 'chat' ? ' selected' : '') + '>chat</option>'
        + '</select></span>'
        + '<span class="group">思考'
        + '<select class="p-model-reasoning" data-model-key="' + escAttr(key) + '"'
        + (type === 'chat' ? ' disabled title="chat 模型不发送思考参数"' : '') + '>'
        + renderReasoningOptions(iface || 'anthropic', m.reasoning) + '</select></span>'
        + '<span class="group">输入模态<span class="modality-group">' + renderModalities(m.input) + '</span></span>'
        + '<span class="p-model-result" data-model-key="' + escAttr(key) + '" hidden></span>'
        + '</div>'
        + '</div>';
}

function renderProviderCard(key, p) {
    p = p || {};
    // 兼容手写旧别名：openai-compatible 等价于 openai（Chat Completions）
    const iface = p.protocol === 'openai-compatible' ? 'openai' : (p.protocol || 'anthropic');
    // 未知 / 已移除的 backend（codex / claude-code / opencode）一律按自定义显示
    const serverKey = getServerPreset(p.backend) ? p.backend : 'custom';
    const preset = getServerPreset(serverKey);
    const urlCustom = p.base_url && !isKnownPresetUrl(p.base_url) ? '1' : '';
    const pathCustom = p.api_path && !isKnownPresetApiPath(p.api_path) ? '1' : '';
    const defaultUrl = serverPresetUrl(preset, iface);
    const defaultPath = serverPresetApiPath(preset, iface);
    const isOAuth = p.auth_type === 'oauth' || serverKey === 'chatgpt-oauth';
    const siwc = p.siwc_status || null;
    const siwcConnected = Boolean(siwc && siwc.connected);
    const configured = isOAuth
        ? Boolean(p.credential && siwcConnected)
        : Boolean(p.api_key_masked || p.api_key);
    const statusText = isOAuth
        ? (siwcConnected ? '已连接' : '未登录')
        : (configured ? '已配置' : '未配置');
    const status = '<span class="provider-status" data-masked="' + (p.api_key_masked ? 1 : 0) + '">'
        + '<span class="status-dot ' + (configured ? 'ok' : 'pending') + '"></span>'
        + statusText + '</span>';
    const masked = p.api_key_masked
        ? '<div class="apikey-hint" title="留空保持不变">已保存：' + p.api_key_masked + '，留空则保持不变</div>'
        : '';
    const models = p.models && Object.keys(p.models).length ? p.models : { default: {} };
    const modelKeys = Object.keys(models);
    const modelRows = modelKeys.map(function (mk) {
        return renderModelRow(mk, models[mk], iface);
    }).join('');
    const esc = escAttr;
    const apiKeyField = '<div class="field w-key p-api-key-field"' + (isOAuth ? ' hidden' : '') + '>'
        + '<label class="field-label">API Key</label>'
        + '<div class="key-wrap">'
        + '<input type="password" class="p-api-key" placeholder="sk-...（留空保持不变）">'
        + '<button type="button" class="icon-btn toggle-password" onclick="toggleProviderKeyVisibility(this)" title="显示">' + MS_ICONS.eye + '</button>'
        + '</div>'
        + masked
        + '</div>';
    const oauthPanel = '<div class="field w-key p-siwc-panel"' + (isOAuth ? '' : ' hidden') + '>'
        + '<label class="field-label">ChatGPT 账号</label>'
        + '<div class="siwc-account">'
        + '<span class="siwc-account-state ' + (siwcConnected ? 'ok' : 'pending') + '">'
        + (siwcConnected ? ('已连接：' + esc(siwc.email || '')) : '未登录') + '</span>'
        + '<button type="button" class="btn-ghost p-siwc-login">' + (siwcConnected ? '重新登录' : '使用 ChatGPT 登录') + '</button>'
        + (siwcConnected ? '<button type="button" class="btn-ghost danger p-siwc-logout">断开连接</button>' : '')
        + '</div>'
        + '<div class="siwc-hint">需要 ChatGPT Plus/Pro，且账号需开通套餐额度共享。请求会使用你的套餐额度。</div>'
        + '<div class="siwc-manual" hidden>'
        + '<input type="text" class="p-siwc-callback" placeholder="粘贴地址栏中的回调 URL">'
        + '<button type="button" class="btn-ghost p-siwc-complete">提交</button>'
        + '</div>'
        + '<div class="siwc-result" hidden></div>'
        + '</div>';
    const initial = (String(key || '?').trim().charAt(0) || '?').toUpperCase();
    return '<div class="provider-card" data-credential="' + esc(p.credential || '') + '" data-auth-type="' + esc(isOAuth ? 'oauth' : (p.auth_type || 'auto')) + '">'
        + '<div class="provider-header">'
        + '<span class="provider-avatar">' + esc(initial) + '</span>'
        + '<input type="text" class="provider-key-input p-key" value="' + esc(key) + '" placeholder="模型提供商名称">'
        + '<span class="spacer"></span>'
        + status
        + '<button type="button" class="btn-ghost p-detect" title="自动检测网关模型列表">' + MS_ICONS.refresh + '检测模型</button>'
        + '<button type="button" class="icon-btn danger p-remove" title="删除提供商">' + MS_ICONS.trash + '</button>'
        + '</div>'
        + '<div class="provider-body">'
        + '<div class="field-grid">'
        + '<div class="field w-server"><label class="field-label">接入服务商</label><select class="p-server">'
        + renderServerOptions(serverKey)
        + '</select></div>'
        + '<div class="field w-protocol"><label class="field-label">协议</label><select class="p-interface-type"' + (isOAuth ? ' disabled' : '') + '>'
        + '<option value="anthropic"' + (iface === 'anthropic' ? ' selected' : '') + '>Anthropic Messages</option>'
        + '<option value="openai"' + (iface === 'openai' ? ' selected' : '') + '>OpenAI Chat Completions</option>'
        + '<option value="openai-responses"' + (iface === 'openai-responses' ? ' selected' : '') + '>OpenAI Responses</option>'
        + '</select></div>'
        + '<div class="field w-auth"><label class="field-label">认证</label><select class="p-auth-type">'
        + '<option value="auto"' + ((p.auth_type || 'auto') === 'auto' ? ' selected' : '') + '>自动</option>'
        + '<option value="bearer"' + (p.auth_type === 'bearer' ? ' selected' : '') + '>Bearer</option>'
        + '<option value="x-api-key"' + (p.auth_type === 'x-api-key' ? ' selected' : '') + '>X-API-Key</option>'
        + '<option value="none"' + (p.auth_type === 'none' ? ' selected' : '') + '>无</option>'
        + '<option value="oauth"' + (p.auth_type === 'oauth' || serverKey === 'chatgpt-oauth' ? ' selected' : '') + '>OAuth（ChatGPT 登录）</option>'
        + '</select></div>'
        + '<div class="field w-url"><label class="field-label">Base URL</label><input type="text" class="p-base-url" value="' + esc(p.base_url || '') + '" data-url-custom="' + urlCustom + '" placeholder="' + esc(defaultUrl || 'https://api.anthropic.com') + '"' + (isOAuth ? ' readonly' : '') + '></div>'
        + apiKeyField
        + oauthPanel
        + '</div>'
        + '<details class="p-headers-panel">'
        + '<summary>高级：API 路径 / 请求参数 / Headers</summary>'
        + '<div class="p-advanced-grid">'
        + '<label class="p-advanced-label">API 路径<span>留空 = 协议默认</span></label>'
        + '<input type="text" class="p-api-path" value="' + esc(p.api_path || '') + '" data-path-custom="' + pathCustom + '" placeholder="' + esc(defaultPath || '自动') + '">'
        + '</div>'
        + '<textarea class="p-headers" placeholder="JSON 对象，值支持 {session_id} 占位符，如 {\"X-Session-ID\": \"{session_id}\"}">' + JSON.stringify(p.headers || {}).replace(/</g, '&lt;') + '</textarea>'
        + '</details>'
        + '<div class="models-section">'
        + '<div class="models-head">'
        + '<span class="models-section-title">模型</span>'
        + '<span class="chip chip-count p-model-count">' + modelKeys.length + '</span>'
        + '<span class="spacer"></span>'
        + '<button type="button" class="btn-ghost p-model-add">＋ 新增模型</button>'
        + '</div>'
        + '<div class="p-model-head"><span>别名</span><span>模型 ID</span><span>最大输出 (tokens)</span><span>上下文预算 (tokens)</span><span>操作</span></div>'
        + '<div class="p-models-list">' + modelRows + '</div>'
        + '</div>'
        + '</div>'
        + '</div>';
}

// OAuth（ChatGPT 套餐额度）面板联动：锁定协议/端点，切换 API Key 与登录面板
function syncAuthPanel(card) {
    const authSel = card.querySelector('.p-auth-type');
    const serverSel = card.querySelector('.p-server');
    const isOAuth = (authSel && authSel.value === 'oauth') || (serverSel && serverSel.value === 'chatgpt-oauth');
    card.dataset.authType = isOAuth ? 'oauth' : ((authSel && authSel.value) || 'auto');
    const keyField = card.querySelector('.p-api-key-field');
    const panel = card.querySelector('.p-siwc-panel');
    if (keyField) keyField.hidden = isOAuth;
    if (panel) panel.hidden = !isOAuth;
    const ifaceSel = card.querySelector('.p-interface-type');
    const urlInput = card.querySelector('.p-base-url');
    const pathInput = card.querySelector('.p-api-path');
    if (isOAuth) {
        if (ifaceSel) { ifaceSel.value = 'openai-responses'; ifaceSel.disabled = true; }
        if (urlInput) { urlInput.value = 'https://api.openai.com/v1'; urlInput.readOnly = true; }
        if (pathInput) { pathInput.value = ''; pathInput.readOnly = true; }
    } else {
        if (ifaceSel) ifaceSel.disabled = false;
        if (urlInput) urlInput.readOnly = false;
        if (pathInput) pathInput.readOnly = false;
    }
}

function setSiwcResult(card, text, cls) {
    const el = card.querySelector('.siwc-result');
    if (!el) return;
    el.hidden = false;
    el.className = 'siwc-result' + (cls ? ' ' + cls : '');
    el.textContent = text;
}

function applySiwcAuthorized(card, data) {
    const credential = data.credential || '';
    const email = data.email || '';
    // 就地更新当前卡片；不能整页重载，否则未保存的 provider 会被服务端配置覆盖而“消失”
    card.dataset.credential = credential;
    const status = card.querySelector('.provider-status');
    if (status) {
        status.dataset.masked = '0';
        status.innerHTML = '<span class="status-dot ok"></span>已连接';
    }
    const stateEl = card.querySelector('.siwc-account-state');
    if (stateEl) {
        stateEl.className = 'siwc-account-state ok';
        stateEl.textContent = '已连接：' + email;
    }
    const loginBtn = card.querySelector('.p-siwc-login');
    if (loginBtn) loginBtn.textContent = '重新登录';
    const account = card.querySelector('.siwc-account');
    if (account && !card.querySelector('.p-siwc-logout')) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn-ghost danger p-siwc-logout';
        btn.textContent = '断开连接';
        account.appendChild(btn);
    }
    if (data.saved) {
        setSiwcResult(card, '✓ 登录成功，配置已保存', 'ok');
    } else {
        setSiwcResult(card, '✓ 登录成功，但配置保存失败，请点击「保存配置」重试', 'err');
    }
}

function stopSiwcPolling(card) {
    const id = card.dataset.siwcTimer;
    if (id) {
        clearInterval(parseInt(id, 10));
        delete card.dataset.siwcTimer;
    }
}

function collectSiwcProviderConfig(card) {
    let headers = {};
    const headersJson = card.querySelector('.p-headers').value.trim();
    if (headersJson) {
        try { headers = JSON.parse(headersJson); }
        catch (e) { return { error: 'Headers JSON 语法错误: ' + e.message }; }
    }
    const models = {};
    card.querySelectorAll('.p-model-row').forEach(function (row) {
        const mkey = row.querySelector('.p-model-key').value.trim();
        if (!mkey || models[mkey]) return;
        const spec = {};
        const name = row.querySelector('.p-model-name').value.trim();
        if (name) spec.name = name;
        spec.max_output_tokens = parseInt(row.querySelector('.p-model-max-output').value) || 32768;
        spec.max_context_tokens = parseInt(row.querySelector('.p-model-max-context').value) || 200000;
        spec.model_type = row.querySelector('.p-model-type').value;
        spec.reasoning = row.querySelector('.p-model-reasoning').value;
        spec.input = Array.prototype.map.call(row.querySelectorAll('.p-model-input:checked'), function (el) { return el.value; });
        if (spec.input.indexOf('text') === -1) spec.input.unshift('text');
        models[mkey] = spec;
    });
    return {
        backend: card.querySelector('.p-server').value || 'chatgpt-oauth',
        headers: headers,
        models: models,
    };
}

async function startSiwcLogin(card, btn) {
    const provider = card.querySelector('.p-key').value.trim();
    if (!provider) {
        setSiwcResult(card, '请先填写 Provider 名称', 'err');
        return;
    }
    const providerConfig = collectSiwcProviderConfig(card);
    if (providerConfig.error) {
        setSiwcResult(card, '✗ ' + providerConfig.error, 'err');
        return;
    }
    btn.disabled = true;
    setSiwcResult(card, '正在启动登录…', '');
    try {
        const resp = await fetch('/api/config/chatgpt-oauth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                provider: provider,
                credential: card.dataset.credential || '',
                provider_config: providerConfig,
            }),
        });
        const data = await resp.json();
        if (!data.success) throw new Error(data.error || '启动登录失败');
        card.dataset.siwcAttempt = data.attempt_id;
        const manual = card.querySelector('.siwc-manual');
        if (manual) manual.hidden = false;
        const win = window.open(data.auth_url, '_blank', 'noopener');
        if (win) {
            setSiwcResult(card, '已打开 ChatGPT 授权页，完成后会自动连接。', '');
        } else {
            setSiwcResult(card, '浏览器拦截了新窗口，请使用下方手动方式。', '');
            const el = card.querySelector('.siwc-result');
            const link = document.createElement('a');
            link.href = data.auth_url;
            link.target = '_blank';
            link.rel = 'noopener';
            link.textContent = '打开授权页面';
            el.appendChild(document.createElement('br'));
            el.appendChild(link);
        }
        pollSiwcLogin(card, data.attempt_id);
    } catch (e) {
        setSiwcResult(card, '✗ ' + e.message, 'err');
    } finally {
        btn.disabled = false;
    }
}

function pollSiwcLogin(card, attemptId) {
    stopSiwcPolling(card);
    const started = Date.now();
    const timer = setInterval(async function () {
        if (Date.now() - started > 10 * 60 * 1000) {
            stopSiwcPolling(card);
            setSiwcResult(card, '登录超时，请重新开始。', 'err');
            return;
        }
        try {
            const resp = await fetch('/api/config/chatgpt-oauth/login/' + encodeURIComponent(attemptId));
            const data = await resp.json();
            if (data.status === 'authorized') {
                stopSiwcPolling(card);
                applySiwcAuthorized(card, data);
            } else if (data.status === 'failed') {
                stopSiwcPolling(card);
                setSiwcResult(card, '✗ ' + (data.error || '登录失败'), 'err');
            } else if (data.status === 'expired' || data.status === 'cancelled') {
                stopSiwcPolling(card);
                setSiwcResult(card, '登录已过期，请重新开始。', 'err');
            }
        } catch (e) {
            // 轮询失败保持重试
        }
    }, 1000);
    card.dataset.siwcTimer = String(timer);
}

async function completeSiwcManual(card) {
    const input = card.querySelector('.p-siwc-callback');
    const url = input ? input.value.trim() : '';
    if (!url) {
        setSiwcResult(card, '请粘贴回调地址', 'err');
        return;
    }
    const attemptId = card.dataset.siwcAttempt || '';
    if (!attemptId) {
        setSiwcResult(card, '请先点击「使用 ChatGPT 登录」', 'err');
        return;
    }
    try {
        const resp = await fetch('/api/config/chatgpt-oauth/login/' + encodeURIComponent(attemptId) + '/complete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ callback_url: url }),
        });
        const data = await resp.json();
        if (data.status === 'authorized') {
            stopSiwcPolling(card);
            applySiwcAuthorized(card, data);
        } else {
            setSiwcResult(card, '✗ ' + (data.error || '回调校验失败'), 'err');
        }
    } catch (e) {
        setSiwcResult(card, '✗ ' + e.message, 'err');
    }
}

async function siwcLogoutProvider(card) {
    const provider = card.querySelector('.p-key').value.trim();
    if (!provider) return;
    if (!confirm('断开后需要重新登录才能使用套餐额度，继续？')) return;
    stopSiwcPolling(card);
    let revoked = true;
    try {
        const resp = await fetch('/api/config/chatgpt-oauth/logout', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider: provider }),
        });
        const data = await resp.json();
        revoked = Boolean(data.revoked);
    } catch (e) {
        revoked = false;
    }
    if (!revoked) {
        alert('本地凭证已断开，但远程撤销未确认。\n请在 ChatGPT 设置 → Security → Linked apps 中手动移除该应用。');
    }
    await openSettings();
}

function updateModelCount(card) {
    if (!card) return;
    const countEl = card.querySelector('.p-model-count');
    if (countEl) countEl.textContent = card.querySelectorAll('.p-model-row').length;
}

function renderProviders(providers) {
    const list = document.getElementById('providers-list');
    const entries = Object.keys(providers || {})
        .filter(function (key) { return key !== '_comment'; })
        .map(function (key) { return [key, providers[key] || {}]; });
    list.innerHTML = entries.map(function (e) { return renderProviderCard(e[0], e[1]); }).join('');
    list.querySelectorAll('.provider-card').forEach(function (card) {
            updateUrlPlaceholder(card);
    });
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
    const workflow = document.getElementById('agent-workflow-model');
    const options = refs.map(function (r) { return '<option value="' + r + '">' + r + '</option>'; }).join('');
    [master, worker, lite, workflow].forEach(function (sel) {
        if (!sel) return;
        const current = sel.value;
        let fallback = '';
        if (sel !== master) {
            fallback = '（与 Master 一致）';
        }
        sel.innerHTML = (fallback ? '<option value="">' + fallback + '</option>' : '') + options;
        if (current && refs.includes(current)) sel.value = current;
    });
    updateAgentEffective();
}

function restoreAgentSelection(agents) {
    const a = agents || {};
    if (a.master && a.master.model) document.getElementById('agent-master-model').value = a.master.model;
    if (a.worker && a.worker.model) document.getElementById('agent-worker-model').value = a.worker.model;
    if (a.lite && a.lite.model) document.getElementById('agent-lite-model').value = a.lite.model;
    const workflowSel = document.getElementById('agent-workflow-model');
    if (workflowSel) workflowSel.value = (a.workflow && a.workflow.model) || '';
    updateAgentEffective();
}

// 角色卡底部显示实际生效模型（Worker/Lite 留空时回落 Master）
function updateAgentEffective() {
    const master = document.getElementById('agent-master-model');
    if (!master) return;
    const masterRef = master.value || '';
    const set = function (id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value || '-';
    };
    set('agent-master-effective', masterRef);
    const worker = document.getElementById('agent-worker-model');
    const lite = document.getElementById('agent-lite-model');
    const workerRef = (worker && worker.value) || masterRef;
    set('agent-worker-effective', workerRef);
    set('agent-lite-effective', (lite && lite.value) || masterRef);
    const workflow = document.getElementById('agent-workflow-model');
    set('agent-workflow-effective', (workflow && workflow.value) || masterRef);
}

['agent-master-model', 'agent-worker-model', 'agent-lite-model', 'agent-workflow-model'].forEach(function (id) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', updateAgentEffective);
});

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
            spec.max_output_tokens = parseInt(row.querySelector('.p-model-max-output').value) || 32768;
            spec.max_context_tokens = parseInt(row.querySelector('.p-model-max-context').value) || 200000;
            spec.model_type = row.querySelector('.p-model-type').value;
            spec.reasoning = row.querySelector('.p-model-reasoning').value;
            spec.input = Array.prototype.map.call(row.querySelectorAll('.p-model-input:checked'), function (el) { return el.value; });
            if (spec.input.indexOf('text') === -1) spec.input.unshift('text');
            models[mkey] = spec;
        });
        const authType = card.querySelector('.p-auth-type').value;
        const isOAuth = authType === 'oauth' || card.dataset.authType === 'oauth';
        const provider = {
            protocol: card.querySelector('.p-interface-type').value,
            auth_type: isOAuth ? 'oauth' : authType,
            backend: card.querySelector('.p-server').value,
            base_url: card.querySelector('.p-base-url').value.trim(),
            api_path: card.querySelector('.p-api-path').value.trim(),
            headers: headers,
            models: models,
        };
        if (isOAuth) {
            provider.credential = card.dataset.credential || '';
            provider.protocol = 'openai-responses';
            provider.base_url = 'https://api.openai.com/v1';
            provider.api_path = '';
        } else {
            const apiKey = card.querySelector('.p-api-key').value.trim();
            if (apiKey) provider.api_key = apiKey;  // 留空 = 后端继承已保存值
        }
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
        const removeBtn = e.target.closest('.p-remove');
        if (removeBtn) {
            card.remove();
            fillAgentSelects(collectAllRefs());
            return;
        }
        const addBtn = e.target.closest('.p-model-add');
        if (addBtn) {
            const iface = card.querySelector('.p-interface-type').value;
            const modelsList = card.querySelector('.p-models-list');
            let key = 'model-1';
            let i = 1;
            const taken = new Set(Array.from(card.querySelectorAll('.p-model-key')).map(function (el) { return el.value.trim(); }));
            while (taken.has(key)) { i += 1; key = 'model-' + i; }
            modelsList.insertAdjacentHTML('beforeend', renderModelRow(key, {}, iface));
            updateModelCount(card);
            fillAgentSelects(collectAllRefs());
            return;
        }
        const modelRemove = e.target.closest('.p-model-remove');
        if (modelRemove) {
            const row = modelRemove.closest('.p-model-row');
            if (row) row.remove();
            updateModelCount(card);
            fillAgentSelects(collectAllRefs());
            return;
        }
        const testBtn = e.target.closest('.p-model-test');
        if (testBtn) {
            testProviderModel(testBtn);
            return;
        }
        const siwcLogin = e.target.closest('.p-siwc-login');
        if (siwcLogin) {
            startSiwcLogin(card, siwcLogin);
            return;
        }
        const siwcLogout = e.target.closest('.p-siwc-logout');
        if (siwcLogout) {
            siwcLogoutProvider(card);
            return;
        }
        const siwcComplete = e.target.closest('.p-siwc-complete');
        if (siwcComplete) {
            completeSiwcManual(card);
            return;
        }
        const detectBtn = e.target.closest('.p-detect');
        if (detectBtn) detectProviderModels(detectBtn);
    });

    // provider 名称 / URL / 路径 / API Key 输入 → 更新徽标、自定义标记与状态 chip
    list.addEventListener('input', function (e) {
        if (e.target.classList.contains('p-base-url')) {
            e.target.dataset.urlCustom = (!e.target.value.trim() || isKnownPresetUrl(e.target.value.trim())) ? '' : '1';
            return;
        }
        if (e.target.classList.contains('p-api-path')) {
            e.target.dataset.pathCustom = (!e.target.value.trim() || isKnownPresetApiPath(e.target.value.trim())) ? '' : '1';
            return;
        }
        if (e.target.classList.contains('p-key')) {
            const avatar = e.target.closest('.provider-card').querySelector('.provider-avatar');
            if (avatar) avatar.textContent = (e.target.value.trim().charAt(0) || '?').toUpperCase();
            return;
        }
        if (!e.target.classList.contains('p-api-key')) return;
        const card = e.target.closest('.provider-card');
        const status = card.querySelector('.provider-status');
        if (!status) return;
        const configured = Boolean(e.target.value.trim()) || status.dataset.masked === '1';
        status.innerHTML = '<span class="status-dot ' + (configured ? 'ok' : 'pending') + '"></span>'
            + (configured ? '已配置' : '未配置');
    });

    // 接入服务商变化 → 默认协议 + 地址/路径/认证
    list.addEventListener('change', function (e) {
        if (!e.target.classList.contains('p-server')) return;
        const card = e.target.closest('.provider-card');
        const preset = getServerPreset(e.target.value);
        if (preset && preset.default_protocol) {
            card.querySelector('.p-interface-type').value = preset.default_protocol;
            refreshReasoningOptions(card);
        }
        applyServerSelection(card);
        syncAuthPanel(card);
    });

    // 认证方式变化 → OAuth 面板与协议/端点锁定
    list.addEventListener('change', function (e) {
        if (!e.target.classList.contains('p-auth-type')) return;
        syncAuthPanel(e.target.closest('.provider-card'));
    });

    // protocol 变化 → 重渲染思考下拉 + 跟随预设换地址/路径
    list.addEventListener('change', function (e) {
        if (!e.target.classList.contains('p-interface-type')) return;
        const card = e.target.closest('.provider-card');
        refreshReasoningOptions(card);
        applyPresetDefaults(card);
    });

    // model_type 变化 → chat 模型禁用思考档位（请求不携带任何思考参数）
    list.addEventListener('change', function (e) {
        if (!e.target.classList.contains('p-model-type')) return;
        const row = e.target.closest('.p-model-row');
        const sel = row && row.querySelector('.p-model-reasoning');
        if (!sel) return;
        const isChat = e.target.value === 'chat';
        sel.disabled = isChat;
        sel.title = isChat ? 'chat 模型不发送思考参数' : '';
    });
})();

// ─── 关于 Tab：版本与升级 ─────────────────────────────────────────────
// 只负责检查版本和下载升级包到脚本目录；解压覆盖由启动循环在服务重启时进行
// （下载完成后服务端会等会话空闲自动重启，也可手动「立即重启应用」）。

let updatePollTimer = null;
// 轮询间隔 10 秒；上限 15 分钟（后台最坏情况：4 个镜像 × 120s 超时 + 传输）
const UPDATE_POLL_INTERVAL_MS = 10000;
const UPDATE_POLL_MAX_MS = 15 * 60 * 1000;

let aboutVersionPromise = null;

// 读取 version.json 填充品牌头（静态文件缺失时保留默认文案）
async function loadAboutVersionInfo() {
    if (aboutVersionPromise) return aboutVersionPromise;
    aboutVersionPromise = (async () => {
        try {
            const resp = await fetch(`/static/version.json?t=${Date.now()}`);
            const v = await resp.json();
            const nameEl = document.getElementById('about-app-name');
            const authorEl = document.getElementById('about-author');
            if (nameEl && v.app_name) nameEl.textContent = v.app_name;
            if (authorEl && v.author) authorEl.textContent = 'by ' + v.author;
        } catch (e) {
            // 保留默认文案
        }
    })();
    return aboutVersionPromise;
}

function setAboutStatus(el, text, kind) {
    if (!el) return;
    el.className = 'about-status' + (kind ? ' ' + kind : '');
    el.textContent = text;
}

function setAboutLocalVersion(version) {
    const v = version || '-';
    const localEl = document.getElementById('about-local-version');
    const heroEl = document.getElementById('about-hero-version');
    if (localEl) localEl.textContent = v;
    if (heroEl) heroEl.textContent = v;
}

async function loadAboutStatus() {
    const statusEl = document.getElementById('about-update-status');
    if (!statusEl) return;
    loadAboutVersionInfo();
    const card = document.getElementById('about-card');
    try {
        const resp = await fetch('/api/update/status');
        const data = await resp.json();
        setAboutLocalVersion(data.local_version);
        const zipEl = document.getElementById('about-zip-status');
        const restartBtn = document.getElementById('btn-restart-update');
        const launcherEl = document.getElementById('about-launcher');
        if (launcherEl) {
            launcherEl.textContent = data.launcher_managed
                ? '当前运行方式：由 start.cmd 托管'
                : '当前运行方式：手动启动';
        }
        if (data.zip_present && data.zip_version) {
            zipEl.textContent = data.launcher_managed
                ? `升级包 ${data.zip_version} 已就绪，空闲时将自动重启应用。`
                : `升级包 ${data.zip_version} 已就绪，重启服务后自动应用。`;
            if (restartBtn) restartBtn.style.display = data.launcher_managed ? '' : 'none';
        } else {
            zipEl.textContent = '';
            if (restartBtn) restartBtn.style.display = 'none';
        }
        if (data.downloading) {
            if (card) card.classList.add('downloading');
            setAboutStatus(statusEl, '正在下载升级包…', '');
            pollDownloadStatus();
        } else {
            if (card) card.classList.remove('downloading');
            if (data.last_result && !data.last_result.success) {
                // 自动下载的失败也在这里可见（结果由 core.updater 统一记录）
                setAboutStatus(statusEl, '✗ ' + (data.last_result.error || '上次下载失败'), 'err');
            }
        }
    } catch (e) {
        setAboutStatus(statusEl, '✗ 加载升级状态失败: ' + e.message, 'err');
    }
}

async function checkForUpdate() {
    const statusEl = document.getElementById('about-update-status');
    const remoteEl = document.getElementById('about-remote-version');
    const badge = document.getElementById('about-update-badge');
    setAboutStatus(statusEl, '正在检查更新…', '');
    try {
        const resp = await fetch('/api/update/check');
        const data = await resp.json();
        if (data.error) {
            remoteEl.textContent = '检查失败';
            remoteEl.classList.add('pending');
            if (badge) badge.hidden = true;
            setAboutStatus(statusEl, '✗ ' + data.error, 'err');
            return;
        }
        remoteEl.textContent = data.remote_version;
        remoteEl.classList.remove('pending');
        if (badge) badge.hidden = !data.has_update;
        document.getElementById('btn-download-update').style.display = data.has_update ? '' : 'none';
        if (data.has_update) {
            setAboutStatus(statusEl, `发现新版本 ${data.remote_version}（当前 ${data.local_version}）`, 'info');
        } else {
            setAboutStatus(statusEl, '已是最新版本', 'ok');
        }
    } catch (e) {
        setAboutStatus(statusEl, '✗ 检查更新失败: ' + e.message, 'err');
    }
}

async function downloadUpdate() {
    const statusEl = document.getElementById('about-update-status');
    const card = document.getElementById('about-card');
    try {
        const resp = await fetch('/api/update/download', { method: 'POST' });
        const data = await resp.json();
        if (!data.success) {
            setAboutStatus(statusEl, '✗ ' + (data.error || '下载失败'), 'err');
            return;
        }
        if (card) card.classList.add('downloading');
        setAboutStatus(statusEl, '正在下载升级包…', '');
        pollDownloadStatus();
    } catch (e) {
        setAboutStatus(statusEl, '✗ 发起下载失败: ' + e.message, 'err');
    }
}

async function restartUpdateNow() {
    const statusEl = document.getElementById('about-update-status');
    try {
        const resp = await fetch('/api/update/restart', { method: 'POST' });
        const data = await resp.json();
        if (!data.success) {
            setAboutStatus(statusEl, '✗ ' + (data.error || '重启失败'), 'err');
            return;
        }
        setAboutStatus(statusEl, '服务重启中，等待新实例上线…', '');
        pollRestartStatus();
    } catch (e) {
        setAboutStatus(statusEl, '✗ 发起重启失败: ' + e.message, 'err');
    }
}

function pollRestartStatus() {
    if (updatePollTimer) { clearInterval(updatePollTimer); updatePollTimer = null; }
    const startedAt = Date.now();
    updatePollTimer = setInterval(async () => {
        const elapsed = Date.now() - startedAt;
        if (elapsed > 3 * 60 * 1000) {
            clearInterval(updatePollTimer);
            updatePollTimer = null;
            setAboutStatus(document.getElementById('about-update-status'),
                '⚠ 等待重启超时，请稍后刷新页面确认', 'err');
            return;
        }
        try {
            const resp = await fetch('/api/update/status');
            if (!resp.ok) return;
            const data = await resp.json();
            // 新实例已上线的标志：升级包应用后已删除（zip_present 变 false）。
            // 前 8 秒旧实例可能仍在优雅停机，避免把旧实例当作重启完成。
            if (elapsed > 8000 && data.zip_present === false) {
                clearInterval(updatePollTimer);
                updatePollTimer = null;
                setAboutStatus(document.getElementById('about-update-status'),
                    '✓ 服务已重启，页面即将刷新', 'ok');
                setTimeout(() => location.reload(), 800);
            }
        } catch (e) {
            // 服务下线中：继续等待新实例上线
        }
    }, 3000);
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
            const card = document.getElementById('about-card');
            if (data.downloading) {
                // 超时兜底：后台仍在下载但轮询已达上限，停止轮询并提示
                setAboutStatus(statusEl, '⚠ 下载耗时过长，仍在后台进行；稍后重新打开本页查看结果', '');
                return;
            }
            if (card) card.classList.remove('downloading');
            const result = data.last_result;
            if (result && result.success) {
                setAboutStatus(statusEl, '✓ ' + (result.message || '升级包已下载'), 'ok');
                loadAboutStatus();  // 刷新升级包就绪提示
            } else if (result) {
                setAboutStatus(statusEl, '✗ ' + (result.error || '下载失败'), 'err');
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
        const restartBtn = document.getElementById('btn-restart-update');
        if (checkBtn) checkBtn.addEventListener('click', checkForUpdate);
        if (downloadBtn) downloadBtn.addEventListener('click', downloadUpdate);
        if (restartBtn) restartBtn.addEventListener('click', restartUpdateNow);
        // 切到「关于」页签时刷新状态
        document.querySelectorAll('.settings-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                if (tab.dataset.tab === 'about') loadAboutStatus();
            });
        });
    });
})();

// ─── 权限设置（独立页签） ───────────────────────────────────────────
let permissionState = {
    config: {},
    workspaces: [],
    readonlyGroups: {},
    scope: 'global',
    filter: 'all',
};

async function loadPermissionsTab() {
    const statusEl = document.getElementById('permission-status');
    if (statusEl) statusEl.textContent = '加载中...';
    try {
        const resp = await fetch('/api/permissions');
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || '加载失败');
        permissionState.workspaces = data.workspaces || [];
        permissionState.readonlyGroups = data.readonly_groups || {};
        permissionState.config = data.permissions || {};
        fillPermissionForm(permissionState.config);
        renderPermissionScopeOptions();
        renderPermissionReadonlyGroups();
        await loadPermissionRules();
        if (statusEl) statusEl.textContent = '';
    } catch (e) {
        if (statusEl) statusEl.textContent = '加载失败: ' + e.message;
    }
}

function setPermissionField(id, value, prop = 'value') {
    const el = document.getElementById(id);
    if (!el) return;
    if (prop === 'checked') el.checked = !!value;
    else el.value = value == null ? '' : value;
}

function fillPermissionForm(cfg) {
    cfg = cfg || {};
    const outside = cfg.outside || {};
    const judge = cfg.judge || {};
    const approval = cfg.approval || {};
    setPermissionField('permission-enabled', cfg.enabled !== false, 'checked');
    setPermissionField('permission-default-mode', cfg.default_mode || 'allow');
    setPermissionField('permission-outside-read', outside.read || 'allow');
    setPermissionField('permission-outside-write', outside.write || 'ask');
    setPermissionField('permission-outside-delete', outside.delete || 'ask');
    setPermissionField('permission-outside-navigate', outside.navigate || 'ai');
    setPermissionField('permission-readonly-bypass', cfg.readonly_bypass !== false, 'checked');
    setPermissionField('permission-judge-enabled', judge.enabled !== false, 'checked');
    setPermissionField('permission-judge-timeout', judge.timeout_seconds || 60);
    setPermissionField('permission-judge-on-error', judge.on_error || 'ask');
    setPermissionField('permission-judge-persist', judge.persist_allow !== false, 'checked');
    setPermissionField('permission-judge-cache', judge.cache || 'session');
    setPermissionField('permission-judge-failures', judge.max_consecutive_failures || 3);
    setPermissionField('permission-approval-timeout',
        approval.timeout_seconds == null ? 300 : approval.timeout_seconds);
    setPermissionField('permission-approval-auto-resume', approval.auto_resume || 'web');
}

function renderPermissionScopeOptions() {
    const select = document.getElementById('permission-scope');
    if (!select) return;
    const current = select.value || permissionState.scope || 'global';
    const opts = ['<option value="global">全局</option>'];
    (permissionState.workspaces || []).forEach(ws => {
        opts.push(`<option value="${escapeHtml(ws.uuid)}">${escapeHtml(ws.name || ws.uuid)}</option>`);
    });
    select.innerHTML = opts.join('');
    select.value = current;
    if (!select.value) select.value = 'global';
}

function renderPermissionReadonlyGroups() {
    const root = document.getElementById('permission-readonly-groups');
    if (!root) return;
    const groups = permissionState.readonlyGroups || {};
    const parts = [];
    ['bash', 'git', 'npm', 'pip', 'pwsh', 'safe_create'].forEach(key => {
        const items = groups[key] || [];
        if (items.length) {
            const shown = items.slice(0, 12).map(escapeHtml).join(', ');
            parts.push(`<b>${escapeHtml(key)}</b>: ${shown}${items.length > 12 ? ' …' : ''}`);
        }
    });
    root.innerHTML = parts.length ? parts.join('<br>') : '';
}

function collectPermissionConfig() {
    const cfg = permissionState.config || {};
    return {
        enabled: document.getElementById('permission-enabled').checked,
        default_mode: document.getElementById('permission-default-mode').value,
        readonly_bypass: document.getElementById('permission-readonly-bypass').checked,
        outside: {
            read: document.getElementById('permission-outside-read').value,
            write: document.getElementById('permission-outside-write').value,
            delete: document.getElementById('permission-outside-delete').value,
            navigate: document.getElementById('permission-outside-navigate').value,
        },
        rules_hot_reload: cfg.rules_hot_reload !== false,
        global_rules_path: cfg.global_rules_path || '',
        judge: {
            enabled: document.getElementById('permission-judge-enabled').checked,
            timeout_seconds: parseInt(document.getElementById('permission-judge-timeout').value, 10) || 60,
            on_error: document.getElementById('permission-judge-on-error').value,
            persist_allow: document.getElementById('permission-judge-persist').checked,
            cache: document.getElementById('permission-judge-cache').value,
            max_consecutive_failures: parseInt(document.getElementById('permission-judge-failures').value, 10) || 3,
        },
        approval: {
            timeout_seconds: (() => {
                const v = parseInt(document.getElementById('permission-approval-timeout').value, 10);
                return Number.isFinite(v) ? v : 300;
            })(),
            auto_resume: document.getElementById('permission-approval-auto-resume').value || 'web',
        },
    };
}

async function savePermissions() {
    const statusEl = document.getElementById('settings-status');
    if (statusEl) statusEl.textContent = '保存中...';
    try {
        const resp = await fetch('/api/permissions', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({permissions: collectPermissionConfig()}),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || '保存失败');
        permissionState.config = data.permissions || collectPermissionConfig();
        if (statusEl) statusEl.textContent = '✓ 保存成功';
        await loadPermissionRules();
        setTimeout(() => {
            document.getElementById('settings-modal').style.display = 'none';
        }, 800);
    } catch (e) {
        if (statusEl) statusEl.textContent = '✗ ' + e.message;
    }
}

async function loadPermissionRules() {
    const scope = document.getElementById('permission-scope')?.value || 'global';
    const decision = document.getElementById('permission-decision-filter')?.value || 'all';
    permissionState.scope = scope;
    permissionState.filter = decision;
    const root = document.getElementById('permission-rules-list');
    if (root) root.innerHTML = '<div class="form-hint">加载中...</div>';
    try {
        const resp = await fetch(`/api/permissions/rules?scope=${encodeURIComponent(scope)}&decision=${encodeURIComponent(decision)}`);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || '加载失败');
        renderPermissionRules(data);
    } catch (e) {
        if (root) root.innerHTML = `<div class="form-hint">加载失败: ${escapeHtml(e.message)}</div>`;
    }
}

const PERMISSION_DECISION_LABELS = {allow: '白名单', deny: '黑名单', ask: '灰名单'};
const PERMISSION_SOURCE_LABELS = {user: '用户', judge: 'AI', builtin: '内置'};

function permissionRuleDisplay(rule) {
    if (rule.display) return rule.display;
    if (rule.note) return rule.note;
    const value = rule.match && rule.match.value;
    return Array.isArray(value) ? value.join(' ') : (value || '');
}

function renderPermissionRules(data) {
    const root = document.getElementById('permission-rules-list');
    if (!root) return;
    const rules = (data.rules || []).concat(data.builtin_rules || []);
    if (!rules.length) {
        root.innerHTML = '<div class="form-hint">暂无规则</div>';
        return;
    }
    const rows = rules.map(rule => {
        const isBuiltin = rule.source === 'builtin' || rule.builtin;
        const decision = PERMISSION_DECISION_LABELS[rule.decision] || rule.decision;
        const source = PERMISSION_SOURCE_LABELS[rule.source] || rule.source || '用户';
        const matchType = (rule.match && rule.match.type) || '';
        const value = permissionRuleDisplay(rule);
        const enabled = rule.enabled !== false;
        const decisionCls = rule.decision === 'allow' ? 'chip chip-ok'
            : rule.decision === 'deny' ? 'chip chip-deny' : 'chip chip-ask';
        const actions = isBuiltin
            ? '<span class="chip chip-ask">只读</span>'
            : `<button class="btn-ghost" data-perm-toggle="${escapeHtml(rule.id)}" data-enabled="${enabled ? '0' : '1'}">${enabled ? '停用' : '启用'}</button>
               <button class="icon-btn danger" title="删除规则" data-perm-delete="${escapeHtml(rule.id)}">${MS_ICONS.trash}</button>`;
        return `<tr>
            <td><span class="${decisionCls}">${escapeHtml(decision)}</span></td>
            <td>${escapeHtml(value)}</td>
            <td>${escapeHtml(rule.tool || '*')}</td>
            <td>${escapeHtml(rule.kind || '')} / ${escapeHtml(matchType)}</td>
            <td>${escapeHtml(source)}${enabled ? '' : '（停用）'}</td>
            <td>${escapeHtml(String(rule.hits || 0))}</td>
            <td>${actions}</td>
        </tr>`;
    }).join('');
    root.innerHTML = `<table class="settings-table" style="margin-top:8px;">
        <thead><tr><th>裁决</th><th>描述</th><th>工具</th><th>类型</th><th>来源</th><th>命中</th><th>操作</th></tr></thead>
        <tbody>${rows}</tbody>
    </table>`;
    root.querySelectorAll('[data-perm-toggle]').forEach(btn => {
        btn.addEventListener('click', () => togglePermissionRule(btn.dataset.permToggle, btn.dataset.enabled === '1'));
    });
    root.querySelectorAll('[data-perm-delete]').forEach(btn => {
        btn.addEventListener('click', () => deletePermissionRule(btn.dataset.permDelete));
    });
}

async function togglePermissionRule(ruleId, enabled) {
    const scope = document.getElementById('permission-scope')?.value || 'global';
    const statusEl = document.getElementById('permission-status');
    try {
        const resp = await fetch(`/api/permissions/rules/${encodeURIComponent(ruleId)}?scope=${encodeURIComponent(scope)}`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled}),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || '更新失败');
        if (statusEl) statusEl.textContent = enabled ? '✓ 已启用' : '✓ 已停用';
        await loadPermissionRules();
    } catch (e) {
        if (statusEl) statusEl.textContent = '✗ ' + e.message;
    }
}

async function deletePermissionRule(ruleId) {
    if (!confirm('确定删除这条规则？')) return;
    const scope = document.getElementById('permission-scope')?.value || 'global';
    const statusEl = document.getElementById('permission-status');
    try {
        const resp = await fetch(`/api/permissions/rules/${encodeURIComponent(ruleId)}?scope=${encodeURIComponent(scope)}`, {method: 'DELETE'});
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || '删除失败');
        if (statusEl) statusEl.textContent = '✓ 已删除';
        await loadPermissionRules();
    } catch (e) {
        if (statusEl) statusEl.textContent = '✗ ' + e.message;
    }
}

async function createPermissionRule() {
    const statusEl = document.getElementById('permission-status');
    const scope = document.getElementById('permission-scope')?.value || 'global';
    const matchType = document.getElementById('permission-rule-match-type').value;
    const rawValue = document.getElementById('permission-rule-value').value.trim();
    if (!rawValue) {
        if (statusEl) statusEl.textContent = '✗ 匹配值不能为空';
        return;
    }
    const value = matchType === 'argv_glob' ? rawValue.split(/\s+/).filter(Boolean) : rawValue;
    const body = {
        decision: document.getElementById('permission-rule-decision').value,
        tool: document.getElementById('permission-rule-tool').value.trim() || '*',
        kind: document.getElementById('permission-rule-kind').value,
        match: {type: matchType, value},
        note: document.getElementById('permission-rule-note').value.trim(),
        enabled: true,
    };
    try {
        const resp = await fetch(`/api/permissions/rules?scope=${encodeURIComponent(scope)}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || '添加失败');
        if (statusEl) statusEl.textContent = '✓ 规则已添加';
        document.getElementById('permission-rule-value').value = '';
        document.getElementById('permission-rule-note').value = '';
        await loadPermissionRules();
    } catch (e) {
        if (statusEl) statusEl.textContent = '✗ ' + e.message;
    }
}

async function revokeAiPermissionRules() {
    if (!confirm('删除当前作用域内所有 AI 自动落盘的规则？')) return;
    const scope = document.getElementById('permission-scope')?.value || 'global';
    const statusEl = document.getElementById('permission-status');
    try {
        const resp = await fetch(`/api/permissions/rules/revoke-ai?scope=${encodeURIComponent(scope)}`, {method: 'POST'});
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || '回收失败');
        if (statusEl) statusEl.textContent = `✓ 已回收 ${data.removed || 0} 条`;
        await loadPermissionRules();
    } catch (e) {
        if (statusEl) statusEl.textContent = '✗ ' + e.message;
    }
}

(function initPermissionsSettings() {
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('.settings-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                if (tab.dataset.tab === 'permissions') loadPermissionsTab();
            });
        });
        const addBtn = document.getElementById('permission-add-rule-btn');
        if (addBtn) addBtn.addEventListener('click', createPermissionRule);
        const revokeBtn = document.getElementById('permission-revoke-ai-btn');
        if (revokeBtn) revokeBtn.addEventListener('click', revokeAiPermissionRules);
        const scopeSelect = document.getElementById('permission-scope');
        if (scopeSelect) scopeSelect.addEventListener('change', loadPermissionRules);
        const filterSelect = document.getElementById('permission-decision-filter');
        if (filterSelect) filterSelect.addEventListener('change', loadPermissionRules);
    });
})();
