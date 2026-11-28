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

async function loadNodeIdentity() {
    try {
        const response = await fetch('/api/system/node');
        if (!response.ok) return;
        const data = await response.json();
        const idEl = document.getElementById('system-node-id');
        const nameEl = document.getElementById('system-node-name');
        if (idEl) idEl.value = data.id || '';
        if (nameEl) nameEl.value = data.name || '';
    } catch (e) {
        console.warn('加载节点身份失败', e);
    }
}

async function saveNodeName() {
    const nameEl = document.getElementById('system-node-name');
    if (!nameEl) return;
    try {
        const response = await fetch('/api/system/node', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: nameEl.value })
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        nameEl.value = data.name || '';
        showToast('节点名称已保存');
    } catch (e) {
        showToast(`保存失败：${e.message || e}`);
    }
}

async function regenerateNodeIdentity() {
    if (!confirm('重新生成节点 ID 会影响 cron 归属和后续课件记录分文件。确定继续吗？')) return;
    try {
        const response = await fetch('/api/system/node/regenerate', { method: 'POST' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        await loadNodeIdentity();
        showToast('节点 ID 已重新生成');
    } catch (e) {
        showToast(`重新生成失败：${e.message || e}`);
    }
}

// 加载某个角色（worker/lite）模型配置到表单
// 收集某个角色（worker/lite）模型配置；name 留空 → 信号后端移除、回退继承 Master
async function openSettings() {
    document.getElementById('settings-modal').style.display = 'flex';
    document.getElementById('settings-status').textContent = '加载中...';
    settingsLoading = true;
    settingsLoadError = '';
    const saveBtn = document.getElementById('settings-save-btn');
    if (saveBtn) saveBtn.disabled = true;

    // Reset to models tab
    document.querySelectorAll('#settings-modal .settings-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('#settings-modal .tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('#settings-modal .settings-tab[data-tab="models"]').classList.add('active');
    document.getElementById('tab-models').classList.add('active');

    try {
        const response = await fetch('/api/config');
        const data = await response.json();
        const cfg = data.config || {};
        loadNodeIdentity();

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
    const activeTab = document.querySelector('#settings-modal .settings-tab.active');
    if (activeTab && activeTab.dataset.tab === 'permissions') {
        return savePermissions();
    }
    if (activeTab && activeTab.dataset.tab === 'org') {
        return;  // 组织页有自己的同步/退出动作，避免用旧表单覆盖组织 provider
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
