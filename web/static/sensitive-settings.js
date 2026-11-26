// ── 工作区敏感词（上行请求替换，配置在 {workspace}/.cili/sensitive_words.json）──
// 依赖 app.js: currentWorkspace。
// 规则文本格式：每行 "原词 = 替换词"，以 # 开头的行忽略；兼容旧的 "=>"。
// 固定规则（手机号 / 身份证号 / 银行卡号）由后端按标准格式 + 校验位处理。

const SENSITIVE_FIXED_INPUTS = {
    phone: 'sensitive-fixed-phone',
    id_card: 'sensitive-fixed-id',
    bank_card: 'sensitive-fixed-bank',
};

function parseSensitiveRules(text) {
    const rules = [];
    (text || '').split('\n').forEach(line => {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#')) return;
        const arrow = trimmed.indexOf('=>');
        const sep = arrow > 0 ? arrow : trimmed.indexOf('=');
        if (sep <= 0) return;
        const word = trimmed.slice(0, sep).trim();
        const replacement = trimmed.slice(sep + (arrow > 0 ? 2 : 1)).trim();
        if (word) rules.push({ word, replacement });
    });
    return rules;
}

function renderSensitiveRules(rules) {
    const editor = document.getElementById('sensitive-rules-editor');
    if (editor) {
        editor.value = (rules || []).map(r => `${r.word} = ${r.replacement}`).join('\n');
    }
}

async function loadSensitiveWords() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('sensitive-rules-status');
    const enabledEl = document.getElementById('sensitive-enabled');
    if (!statusEl || !enabledEl) return;
    statusEl.textContent = '';
    statusEl.className = 'settings-status';
    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sensitive-words`);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();
        enabledEl.checked = data.enabled === true;
        const fixed = data.fixed || {};
        Object.entries(SENSITIVE_FIXED_INPUTS).forEach(([kind, id]) => {
            const el = document.getElementById(id);
            if (el) el.checked = fixed[kind] === true;
        });
        renderSensitiveRules(data.rules);
    } catch (e) {
        statusEl.textContent = '✗ 加载失败: ' + e.message;
        statusEl.className = 'settings-status error';
    }
}

async function saveSensitiveWords() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('sensitive-rules-status');
    const enabled = document.getElementById('sensitive-enabled').checked;
    const rules = parseSensitiveRules(document.getElementById('sensitive-rules-editor').value);
    const fixed = {};
    Object.entries(SENSITIVE_FIXED_INPUTS).forEach(([kind, id]) => {
        const el = document.getElementById(id);
        fixed[kind] = el ? el.checked : true;
    });
    if (!statusEl) return;
    statusEl.textContent = '保存中...';
    statusEl.className = 'settings-status info';
    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sensitive-words`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled, fixed, rules })
        });
        if (!response.ok) {
            let detail = 'HTTP ' + response.status;
            try {
                const err = await response.json();
                detail = err.detail || detail;
            } catch {
                // 响应不是 JSON，保留状态码
            }
            throw new Error(detail);
        }
        const data = await response.json();
        renderSensitiveRules(data.rules);
        statusEl.textContent = `✓ 已保存（${(data.rules || []).length} 条规则，下一回合生效）`;
        statusEl.className = 'settings-status success';
    } catch (e) {
        statusEl.textContent = '✗ 保存失败: ' + e.message;
        statusEl.className = 'settings-status error';
    }
}
