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
