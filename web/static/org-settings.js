// ── 组织托管设置页 ─────────────────────────────────────
// 设备令牌保存在服务端本地 data/cili/org/session.json；
// 本页只通过 /api/org/* 读取状态，永远不接触令牌。
(function () {
    'use strict';

    const state = {
        status: null,
        countdown: 0,
        timer: null,
        loading: false,
    };

    function $(id) { return document.getElementById(id); }

    function esc(value) {
        return String(value == null ? '' : value)
            .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
    }

    async function api(path, options) {
        const opts = Object.assign({ credentials: 'same-origin' }, options || {});
        if (opts.body && typeof opts.body !== 'string') {
            opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
            opts.body = JSON.stringify(opts.body);
        }
        const response = await fetch(path, opts);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            let message = data.detail;
            if (message && typeof message === 'object') {
                message = message.error || message.message || JSON.stringify(message);
            }
            throw new Error(message || `请求失败（${response.status}）`);
        }
        return data;
    }

    function fmt(value) {
        return value ? esc(value) : '—';
    }

    async function refreshWorkspaceIndex() {
        try {
            if (typeof loadWorkspaces === 'function') await loadWorkspaces();
        } catch (error) {
            console.warn('[org] 刷新工作区列表失败', error);
        }
    }

    // 组织操作会改写 setting.json 的 providers/agents；设置弹窗里的模型列表
    // 是打开时渲染的，不重新拉取会残留已删除的组织 provider（或看不到新加的）。
    async function refreshModelConfig() {
        try {
            const response = await fetch('/api/config', { credentials: 'same-origin' });
            if (!response.ok) return;
            const data = await response.json();
            const cfg = data.config || {};
            if (typeof renderProviders === 'function') {
                renderProviders(cfg.providers || {});
            }
            if (typeof restoreAgentSelection === 'function') {
                restoreAgentSelection(cfg.agents || {});
            }
        } catch (error) {
            console.warn('[org] 刷新模型列表失败', error);
        }
    }

    function setStatus(text, isError) {
        const joinedVisible = $('org-joined') && !$('org-joined').hidden;
        const el = joinedVisible ? $('org-action-status') : $('org-join-status');
        if (el) {
            el.textContent = text || '';
            el.classList.toggle('error', !!isError);
        }
    }

    function renderInfo(info) {
        const rows = [];
        const model = info.model || {};
        rows.push(['组织', fmt((info.org || {}).name || (info.org || {}).id)]);
        rows.push(['用户', `${fmt((info.user || {}).name)}（${fmt((info.user || {}).phone_masked)}）`]);
        rows.push(['群组', ((info.user || {}).groups || []).map(esc).join('、') || '—']);
        rows.push(['服务器', fmt(info.server_url)]);
        rows.push(['模型', model.label ? `${esc(model.label)}（${esc(model.protocol)}，可在模型列表选择）` : '未同步']);
        rows.push(['模型键', (model.keys || []).map(esc).join(', ') || '—']);
        rows.push(['配置版本', fmt(info.config_version)]);
        rows.push(['最近同步', fmt(info.last_sync)]);
        const workspaces = info.workspaces || [];
        rows.push(['共享工作区', workspaces.length
            ? workspaces.map(ws => {
                const title = `${esc(ws.name)}（${esc(ws.group_name || '组织')}）`;
                const path = ws.directory ? `<br><span class="muted">${esc(ws.directory)}</span>` : '';
                const error = ws.error ? `<br><span class="org-error">${esc(ws.error)}</span>` : '';
                return title + path + error;
            }).join('<br><br>')
            : '—']);
        if (info.last_error) rows.push(['最近错误', `<span class="org-error">${esc(info.last_error)}</span>`]);
        $('org-joined-info').innerHTML = rows.map(([label, value]) => (
            `<div class="setting-row org-info-row"><label>${esc(label)}</label><div class="control">${value}</div></div>`
        )).join('');
    }

    function render() {
        const info = state.status || {};
        $('org-loading').hidden = true;
        if (!info.joined) {
            $('org-join').hidden = false;
            $('org-joined').hidden = true;
            if (!$('org-server-url').value) $('org-server-url').value = '';
            return;
        }
        $('org-join').hidden = true;
        $('org-joined').hidden = false;
        $('org-joined-title').textContent = (info.org || {}).name || '已加入组织';
        $('org-joined-sub').textContent = info.server_url || '';
        $('org-invalid-note').hidden = !info.session_invalid;
        $('org-sync-btn').disabled = !!info.session_invalid;
        renderInfo(info);
    }

    async function refresh() {
        if (state.loading) return;
        state.loading = true;
        try {
            state.status = await api('/api/org/status');
            render();
        } catch (error) {
            setStatus('读取组织状态失败：' + error.message, true);
        } finally {
            state.loading = false;
        }
    }

    async function discover() {
        const serverUrl = $('org-server-url').value.trim();
        if (!serverUrl) { setStatus('请填写组织服务器地址', true); return; }
        setStatus('连接中...');
        try {
            const data = await api('/api/org/discover', {
                method: 'POST', body: { server_url: serverUrl },
            });
            const orgs = data.orgs || [];
            const select = $('org-select');
            select.innerHTML = orgs.map(org => (
                `<option value="${esc(org.id)}">${esc(org.name)}</option>`
            )).join('');
            if (orgs.length === 1) select.selectedIndex = 0;
            if (!orgs.length) {
                setStatus('该服务器没有可用组织', true);
                return;
            }
            setStatus(`已连接，找到 ${orgs.length} 个组织`);
        } catch (error) {
            setStatus('连接失败：' + error.message, true);
        }
    }

    function startCountdown(seconds) {
        state.countdown = Math.max(0, Number(seconds) || 60);
        if (state.timer) clearInterval(state.timer);
        const button = $('org-send-code-btn');
        const tick = () => {
            if (state.countdown <= 0) {
                clearInterval(state.timer);
                state.timer = null;
                button.disabled = false;
                button.textContent = '获取验证码';
                return;
            }
            button.disabled = true;
            button.textContent = `${state.countdown}s 后重发`;
            state.countdown -= 1;
        };
        tick();
        state.timer = setInterval(tick, 1000);
    }

    async function sendCode() {
        const serverUrl = $('org-server-url').value.trim();
        const orgId = $('org-select').value;
        const phone = $('org-phone').value.trim();
        if (!serverUrl || !orgId || !phone) {
            setStatus('请先填写服务器、选择组织和手机号', true);
            return;
        }
        setStatus('发送中...');
        try {
            const data = await api('/api/org/sms/send', {
                method: 'POST',
                body: { server_url: serverUrl, org_id: orgId, phone },
            });
            setStatus('验证码已发送');
            startCountdown(data.cooldown_seconds || 60);
        } catch (error) {
            setStatus('发送失败：' + error.message, true);
        }
    }

    async function login() {
        const serverUrl = $('org-server-url').value.trim();
        const orgId = $('org-select').value;
        const phone = $('org-phone').value.trim();
        const code = $('org-code').value.trim();
        if (!serverUrl || !orgId || !phone || !code) {
            setStatus('请填写完整的服务器、组织、手机号和验证码', true);
            return;
        }
        setStatus('登录并同步组织配置...');
        try {
            state.status = await api('/api/org/login', {
                method: 'POST',
                body: {
                    server_url: serverUrl,
                    org_id: orgId,
                    phone,
                    code,
                },
            });
            render();
            await refreshWorkspaceIndex();
            await refreshModelConfig();
            if (state.status.session_invalid || state.status.last_error) {
                setStatus(
                    '已登录，但配置同步失败：' + (state.status.last_error || '请稍后重试'),
                    true
                );
            } else {
                setStatus('已加入组织');
                showToast('组织配置已同步');
            }
        } catch (error) {
            setStatus('加入失败：' + error.message, true);
        }
    }

    async function sync() {
        setStatus('同步中...');
        try {
            state.status = await api('/api/org/sync', { method: 'POST' });
            render();
            await refreshWorkspaceIndex();
            await refreshModelConfig();
            setStatus(state.status.last_error ? '同步失败：' + state.status.last_error : '同步完成');
            if (!state.status.last_error) showToast('组织配置已同步');
        } catch (error) {
            setStatus('同步失败：' + error.message, true);
        }
    }

    async function logout() {
        if (!confirm('退出组织？将删除组织下发的模型 provider，并恢复加入前的角色配置；共享工作区文件保留。')) return;
        setStatus('退出中...');
        try {
            state.status = await api('/api/org/logout', { method: 'POST' });
            render();
            await refreshWorkspaceIndex();
            await refreshModelConfig();
            setStatus('已退出组织');
            showToast('已退出组织');
        } catch (error) {
            setStatus('退出失败：' + error.message, true);
        }
    }

    async function relogin() {
        const info = state.status || {};
        $('org-joined').hidden = true;
        $('org-join').hidden = false;
        $('org-server-url').value = info.server_url || '';
        setStatus('请重新验证手机号');
        if (info.server_url) {
            await discover();
            const orgId = (info.org || {}).id || (info.org || {}).slug;
            if (orgId) $('org-select').value = orgId;
        }
    }

    function bind() {
        if (!$('org-join')) return;
        $('org-discover-btn').addEventListener('click', discover);
        $('org-send-code-btn').addEventListener('click', sendCode);
        $('org-login-btn').addEventListener('click', login);
        $('org-sync-btn').addEventListener('click', sync);
        $('org-logout-btn').addEventListener('click', logout);
        $('org-relogin-btn').addEventListener('click', relogin);
        document.querySelectorAll('#settings-modal .settings-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                if (tab.dataset.tab === 'org') refresh();
            });
        });
        refresh();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bind);
    } else {
        bind();
    }
})();
