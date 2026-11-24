// ── 青少年模式设置面板：状态轮询、解锁、策略、审计报告 ──
// 依赖 settings.js 的弹窗/表单工具与全局 window.teenStatus（app/chat/sidebar 读取）。

// ─── 青少年模式（设置面板） ───────────────────────────────────────────

let teenStatusCache = null;
let teenCountdownTimer = null;
let teenStatusTimer = null;

function startTeenStatusPolling() {
    if (teenStatusTimer) return;
    teenStatusTimer = setInterval(loadTeenStatus, 60000);
}

function stopTeenStatusPolling() {
    if (!teenStatusTimer) return;
    clearInterval(teenStatusTimer);
    teenStatusTimer = null;
}

async function loadTeenStatus() {
    let knownDisabled = false;
    try {
        const response = await fetch('/api/teen/status');
        if (response.ok) {
            teenStatusCache = await response.json();
            knownDisabled = teenStatusCache.enabled === false;
        } else {
            teenStatusCache = { enabled: false };
        }
    } catch (e) {
        teenStatusCache = { enabled: false };
    }
    window.teenStatus = teenStatusCache;
    updateTeenBadge();
    // 只有服务端明确返回关闭状态才停止轮询；网络/服务异常时保留轮询，
    // 以便服务恢复后重新同步状态。
    if (knownDisabled) stopTeenStatusPolling();
    else startTeenStatusPolling();
    return teenStatusCache;
}

function teenUnlockRemainingMinutes() {
    if (!teenStatusCache || !teenStatusCache.enabled) return 0;
    const until = teenStatusCache.unlock_until;
    if (!until) return 0;
    const ms = new Date(until.replace(' ', 'T')) - new Date();
    return ms > 0 ? Math.ceil(ms / 60000) : 0;
}

function updateTeenBadge() {
    const badge = document.getElementById('teen-badge');
    if (!badge) return;
    const st = teenStatusCache;
    if (!st || !st.enabled) {
        badge.style.display = 'none';
        if (teenCountdownTimer) { clearInterval(teenCountdownTimer); teenCountdownTimer = null; }
        return;
    }
    badge.style.display = 'block';
    const render = () => {
        const mins = teenUnlockRemainingMinutes();
        badge.textContent = mins > 0 ? '🔒 青少年模式 · 已解锁 ' + mins + ' 分钟' : '🔒 青少年模式';
    };
    render();
    if (!teenCountdownTimer) teenCountdownTimer = setInterval(render, 30000);
}

function teenEscape(text) {
    return escapeHtml(String(text == null ? '' : text));
}

function teenEnableWizardHtml() {
    return `
    <div class="teen-section">
        <h4>开启青少年模式</h4>
        <div class="teen-hint">开启后：会话/消息/执行日志不可删除或撤销；工作区外写删直接拒绝；审批需家长授权；所有操作记入审计日志。</div>
        <div class="teen-row">
            <label style="min-width:110px;">家长密码（≥6位）:</label>
            <input type="password" id="teen-pw1" class="form-control" style="width:200px;" autocomplete="new-password">
        </div>
        <div class="teen-row">
            <label style="min-width:110px;">确认密码:</label>
            <input type="password" id="teen-pw2" class="form-control" style="width:200px;" autocomplete="new-password">
        </div>
        <div class="teen-row">
            <label style="min-width:110px;">解锁窗口时长:</label>
            <select id="teen-window" class="form-control" style="width:120px;">
                <option value="5">5 分钟</option>
                <option value="15" selected>15 分钟</option>
                <option value="30">30 分钟</option>
                <option value="60">60 分钟</option>
            </select>
        </div>
        <div class="teen-row">
            <button class="btn btn-primary btn-small" id="teen-enable-btn">开启青少年模式</button>
        </div>
        <div class="teen-hint">忘记密码的恢复路径：关闭服务 → 删除 <code>data/cili/teen_mode.json</code> → 重启（该操作会记录审计日志）。</div>
    </div>`;
}

function teenLockedHtml(st) {
    const usage = st.usage_today || {};
    return `
    <div class="teen-section">
        <h4>🔒 青少年模式已开启</h4>
        <div class="teen-hint">今日使用：${(usage.minutes || 0).toFixed(1)} 分钟 / ${usage.turns || 0} 个回合。聊天与执行记录不可删除。</div>
        <div class="teen-row">
            <label style="min-width:110px;">家长密码:</label>
            <input type="password" id="teen-unlock-pw" class="form-control" style="width:200px;" autocomplete="current-password">
            <button class="btn btn-primary btn-small" id="teen-unlock-btn">解锁 ${st.window_minutes || 15} 分钟</button>
        </div>
        <div class="teen-hint">解锁窗口内可修改设置、审批受限操作；窗口结束自动回锁。忘记密码：关闭服务 → 删除 <code>data/cili/teen_mode.json</code> → 重启。</div>
    </div>`;
}

function teenUnlockedHtml(st) {
    const p = st.policy || {};
    const limits = st.limits || {};
    const checkbox = (key, label, checked) =>
        `<label><input type="checkbox" data-teen-policy="${key}" ${checked ? 'checked' : ''}> ${label}</label>`;
    return `
    <div class="teen-section">
        <h4>🔒 已解锁（家长模式）<span class="teen-countdown" id="teen-countdown"></span></h4>
        <div class="teen-row">
            <button class="btn btn-small" id="teen-lock-btn">立即上锁</button>
            <button class="btn btn-small" id="teen-disable-btn" style="color:#d93026;">关闭青少年模式</button>
        </div>
        <div class="teen-hint">关闭需再次输入家长密码；关闭后删除/撤销等操作恢复可用。</div>
    </div>
    <div class="teen-section">
        <h4>行为限制</h4>
        <div class="teen-policy-grid">
            ${checkbox('deny_hide', '禁止隐藏会话', p.deny_hide)}
            ${checkbox('deny_share', '禁止分享会话链接', p.deny_share)}
            ${checkbox('deny_memory_delete', '禁止删除/归档记忆', p.deny_memory_delete)}
            ${checkbox('deny_file_delete', '锁定工作区内文件删除', p.deny_file_delete)}
            ${checkbox('approval_strict', '审批 strict（未解锁不出卡、不落盘规则）', p.approval_strict)}
            ${checkbox('deny_read_outside', '禁止读取工作区外文件（agent）', p.deny_read_outside)}
            ${checkbox('mcp_tools', '禁用外部 MCP 工具', p.mcp_tools === 'deny')}
            ${checkbox('browser_upload', '禁用浏览器上传', p.browser_upload === 'deny')}
            ${checkbox('cron', '禁用定时任务增删改', p.cron === 'deny')}
            ${checkbox('prompt_block', '注入受限模式提示词', p.prompt_block)}
        </div>
    </div>
    <div class="teen-section">
        <h4>允许使用的工作区（不勾选 = 全部受限）</h4>
        <div class="teen-policy-grid" id="teen-ws-list"><span class="teen-hint">加载中...</span></div>
        <div class="teen-hint">system 工作区永远受限。受限工作区在列表中带锁标记，进入需家长解锁。</div>
    </div>
    <div class="teen-section">
        <h4>时间与用量</h4>
        <div class="teen-row">
            <label style="min-width:130px;">每日总时长（分钟，0=不限）:</label>
            <input type="number" id="teen-daily-minutes" class="form-control" style="width:100px;" min="0" value="${limits.daily_minutes || 0}">
        </div>
        <div class="teen-row">
            <label style="min-width:130px;">允许使用时段:</label>
            <input type="text" id="teen-allowed-windows" class="form-control" style="width:260px;" placeholder="如 08:00-21:00，多段逗号分隔；留空不限" value="${teenEscape((limits.allowed_windows || []).join(', '))}">
        </div>
        <div class="teen-row">
            <label style="min-width:130px;">解锁窗口时长:</label>
            <select id="teen-window2" class="form-control" style="width:120px;">
                ${[5, 15, 30, 60].map(m => `<option value="${m}" ${st.window_minutes === m ? 'selected' : ''}>${m} 分钟</option>`).join('')}
            </select>
        </div>
        <div class="teen-hint">修改随窗口右下角「保存」按钮一并生效。</div>
    </div>
    <div class="teen-section">
        <h4>修改家长密码</h4>
        <div class="teen-row">
            <label style="min-width:110px;">原密码:</label>
            <input type="password" id="teen-old-pw" class="form-control" style="width:180px;" autocomplete="current-password">
        </div>
        <div class="teen-row">
            <label style="min-width:110px;">新密码（≥6位）:</label>
            <input type="password" id="teen-new-pw" class="form-control" style="width:180px;" autocomplete="new-password">
        </div>
        <div class="teen-row">
            <button class="btn btn-small" id="teen-change-pw-btn">修改密码</button>
        </div>
    </div>
    <div class="teen-section">
        <h4>用量报表</h4>
        <div id="teen-report" class="teen-hint">加载中...</div>
    </div>
    <div class="teen-section">
        <h4>审计日志（最近 100 条）</h4>
        <div id="teen-audit" class="teen-audit-list"><div>加载中...</div></div>
    </div>`;
}

async function renderTeenPanel() {
    const root = document.getElementById('teen-panel');
    if (!root) return;
    const st = await loadTeenStatus();
    if (!st.enabled) {
        root.innerHTML = teenEnableWizardHtml();
        bindTeenEnable();
        return;
    }
    if (!st.unlocked) {
        root.innerHTML = teenLockedHtml(st);
        const btn = document.getElementById('teen-unlock-btn');
        if (btn) btn.addEventListener('click', teenUnlock);
        const pw = document.getElementById('teen-unlock-pw');
        if (pw) pw.addEventListener('keydown', e => { if (e.key === 'Enter') teenUnlock(); });
        return;
    }
    root.innerHTML = teenUnlockedHtml(st);
    bindTeenUnlocked(st);
    await renderTeenWorkspaceList(st);
    loadTeenReport();
    loadTeenAudit();
}

function bindTeenEnable() {
    const btn = document.getElementById('teen-enable-btn');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        const pw1 = document.getElementById('teen-pw1').value;
        const pw2 = document.getElementById('teen-pw2').value;
        if (pw1.length < 6) { showTeenError('密码长度至少 6 位'); return; }
        if (pw1 !== pw2) { showTeenError('两次输入的密码不一致'); return; }
        const windowMinutes = parseInt(document.getElementById('teen-window').value, 10);
        try {
            const response = await fetch('/api/teen/enable', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: pw1, window_minutes: windowMinutes })
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) { showTeenError(data.detail || '开启失败'); return; }
            showTeenOk('青少年模式已开启');
            await renderTeenPanel();
        } catch (e) {
            showTeenError('网络错误: ' + e.message);
        }
    });
}

async function teenUnlock() {
    const pw = document.getElementById('teen-unlock-pw').value;
    if (!pw) { showTeenError('请输入家长密码'); return; }
    try {
        const response = await fetch('/api/teen/unlock', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: pw })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) { showTeenError(data.detail || '解锁失败'); return; }
        showTeenOk('已解锁，窗口结束时自动回锁');
        await renderTeenPanel();
    } catch (e) {
        showTeenError('网络错误: ' + e.message);
    }
}

function bindTeenUnlocked(st) {
    const lockBtn = document.getElementById('teen-lock-btn');
    if (lockBtn) lockBtn.addEventListener('click', async () => {
        await fetch('/api/teen/lock', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        await renderTeenPanel();
    });

    const disableBtn = document.getElementById('teen-disable-btn');
    if (disableBtn) disableBtn.addEventListener('click', async () => {
        const pw = prompt('请输入家长密码以关闭青少年模式：');
        if (pw === null) return;
        const response = await fetch('/api/teen/disable', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: pw })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) { showTeenError(data.detail || '关闭失败'); return; }
        showTeenOk('青少年模式已关闭');
        await renderTeenPanel();
        await loadTeenStatus();
    });

    const pwBtn = document.getElementById('teen-change-pw-btn');
    if (pwBtn) pwBtn.addEventListener('click', async () => {
        const oldPw = document.getElementById('teen-old-pw').value;
        const newPw = document.getElementById('teen-new-pw').value;
        if (newPw.length < 6) { showTeenError('新密码长度至少 6 位'); return; }
        const response = await fetch('/api/teen/password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_password: oldPw, new_password: newPw })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) { showTeenError(data.detail || '修改失败'); return; }
        showTeenOk('密码已修改');
        document.getElementById('teen-old-pw').value = '';
        document.getElementById('teen-new-pw').value = '';
    });

    const cd = document.getElementById('teen-countdown');
    if (cd) {
        const render = () => {
            const mins = teenUnlockRemainingMinutes();
            cd.textContent = mins > 0 ? `（剩余 ${mins} 分钟）` : '';
            if (mins <= 0) renderTeenPanel();
        };
        render();
        if (teenCountdownTimer) clearInterval(teenCountdownTimer);
        teenCountdownTimer = setInterval(render, 30000);
    }
}

async function renderTeenWorkspaceList(st) {
    const container = document.getElementById('teen-ws-list');
    if (!container) return;
    teenWorkspaceLoadFailed = false;
    const allowed = (st.policy || {}).allowed_workspaces || [];
    try {
        const response = await fetch('/api/workspaces');
        const data = await response.json();
        const workspaces = sortWorkspacesByName((data.workspaces || []).filter(ws => !ws.system));
        if (!workspaces.length) {
            container.innerHTML = '<span class="teen-hint">暂无其他工作区</span>';
            return;
        }
        container.innerHTML = workspaces.map(ws =>
            `<label><input type="checkbox" data-teen-ws="${teenEscape(ws.uuid)}" ${allowed.includes(ws.uuid) ? 'checked' : ''}> ${teenEscape(ws.name)}</label>`
        ).join('');
    } catch (e) {
        teenWorkspaceLoadFailed = true;
        container.innerHTML = '<span class="teen-hint">工作区列表加载失败</span>';
    }
}

// 收集青少年策略（主保存按钮调用）；面板不在解锁视图（未开启/已回锁）时返回 null
function collectTeenPolicyPayload() {
    if (!document.querySelector('[data-teen-policy]')) return null;
    const policy = {};
    document.querySelectorAll('[data-teen-policy]').forEach(cb => {
        policy[cb.dataset.teenPolicy] = cb.checked;
    });
    // 三态键（deny/allow）由 checkbox 布尔映射
    policy.mcp_tools = policy.mcp_tools ? 'deny' : 'allow';
    policy.browser_upload = policy.browser_upload ? 'deny' : 'allow';
    policy.cron = policy.cron ? 'deny' : 'allow';
    policy.allowed_workspaces = Array.from(document.querySelectorAll('[data-teen-ws]:checked'))
        .map(cb => cb.dataset.teenWs);
    const windowsText = document.getElementById('teen-allowed-windows').value.trim();
    const limits = {
        daily_minutes: Math.max(0, parseInt(document.getElementById('teen-daily-minutes').value, 10) || 0),
        allowed_windows: windowsText ? windowsText.split(/[,，]/).map(s => s.trim()).filter(Boolean) : [],
    };
    const windowMinutes = parseInt(document.getElementById('teen-window2').value, 10);
    return { policy, limits, window_minutes: windowMinutes };
}

async function loadTeenReport() {
    const el = document.getElementById('teen-report');
    if (!el) return;
    try {
        const response = await fetch('/api/teen/report?days=7');
        if (!response.ok) { el.textContent = '报表加载失败'; return; }
        const data = await response.json();
        const today = data.today || {};
        const denied = today.denied || {};
        const deniedText = Object.keys(denied).length
            ? Object.entries(denied).map(([k, v]) => `${k}×${v}`).join('，')
            : '无';
        const daily = (data.daily || []).map(d => `${d.date}（拦截 ${d.denies} 次）`).join('；');
        el.innerHTML = `今日使用 <b>${(today.minutes || 0).toFixed(1)}</b> 分钟 / <b>${today.turns || 0}</b> 个回合；`
            + `今日拦截：${teenEscape(deniedText)}`
            + (daily ? `<br>近 7 天拦截：${teenEscape(daily)}` : '');
    } catch (e) {
        el.textContent = '报表加载失败';
    }
}

async function loadTeenAudit() {
    const el = document.getElementById('teen-audit');
    if (!el) return;
    try {
        const response = await fetch('/api/teen/audit?limit=100');
        if (!response.ok) { el.innerHTML = '<div>审计加载失败</div>'; return; }
        const data = await response.json();
        const entries = data.audit || [];
        if (!entries.length) {
            el.innerHTML = '<div>暂无审计记录</div>';
            return;
        }
        el.innerHTML = entries.slice().reverse().map(entry => {
            const parts = [entry.ts, entry.event];
            if (entry.action) parts.push(entry.action);
            if (entry.target) parts.push(String(entry.target).slice(0, 60));
            if (entry.source) parts.push('@' + entry.source);
            return `<div>${teenEscape(parts.join(' | '))}</div>`;
        }).join('');
    } catch (e) {
        el.innerHTML = '<div>审计加载失败</div>';
    }
}

function showTeenError(msg) {
    const status = document.getElementById('settings-status');
    if (status) { status.textContent = '✗ ' + msg; status.className = 'settings-status error'; }
    // 保存失败只在弹窗底部状态栏显示容易被漏看，同时弹 toast（utils.js）
    if (typeof showToast === 'function') showToast(msg, 3500);
}

function showTeenOk(msg) {
    const status = document.getElementById('settings-status');
    if (status) { status.textContent = '✓ ' + msg; status.className = 'settings-status success'; }
}

// 切到「青少年模式」页签时按当前状态渲染面板
(function initTeenSettings() {
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('.settings-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                if (tab.dataset.tab === 'teen') renderTeenPanel();
            });
        });
        // 启动即拉取状态（徽章 + 灰显依据）。仅当模式开启时才启动轮询；
        // loadTeenStatus() 会在检测到关闭后自行停止，避免空转请求。
        loadTeenStatus();
    });
})();
