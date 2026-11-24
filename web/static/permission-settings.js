// ── 权限页签：规则编辑、默认策略与审批配置 ──
// 依赖 settings.js 的弹窗/表单工具。

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
