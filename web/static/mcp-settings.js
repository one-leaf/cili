// ── 工作区 MCP 服务器管理（配置在 {workspace}/.cili/mcp/，每台服务器一个 json）──
// 依赖 settings.js 的 MS_ICONS、utils.js 的 escapeHtml/showToast。
(function () {
    let servers = {};      // name -> server（GET 返回的展示态 + 本次新增的完整态）
    let loadedUuid = '';   // 当前面板绑定的工作区
    let bound = false;
    let pollTimer = null;  // 后台连接期间轮询状态
    let pollAttempts = 0;
    const MAX_POLL_ATTEMPTS = 20;  // 1.5s × 20 ≈ 30s

    function parseKv(text) {
        const result = {};
        (text || '').split(',').forEach(pair => {
            const idx = pair.indexOf('=');
            if (idx > 0) result[pair.slice(0, idx).trim()] = pair.slice(idx + 1).trim();
        });
        return result;
    }

    function statusChip(s) {
        if (s.status === 'connected') return `<span class="chip chip-ok"><i></i>已连接 · ${s.tool_count} 工具</span>`;
        if (s.status === 'connecting') return '<span class="chip chip-ask"><i></i>连接中…</span>';
        if (s.status === 'failed') return '<span class="chip chip-deny"><i></i>连接失败</span>';
        return '<span class="chip chip-ask"><i></i>离线</span>';
    }

    async function load(uuid) {
        loadedUuid = uuid || '';
        const statusEl = document.getElementById('mcp-status');
        if (!loadedUuid) {
            servers = {};
            render();
            return;
        }
        try {
            const response = await fetch('/api/mcp/servers?workspace=' + encodeURIComponent(uuid));
            if (!response.ok) throw new Error('HTTP ' + response.status);
            const data = await response.json();
            if (loadedUuid !== uuid) return;  // 期间已切换工作区
            servers = {};
            for (const [name, s] of Object.entries(data.servers || {})) {
                servers[name] = { ...s, _loaded: true };
            }
            render();
            statusEl.textContent = `共 ${Object.keys(servers).length} 个服务器（配置在 .cili/mcp/）`;
            scheduleStatusPoll(uuid);
        } catch (e) {
            servers = {};
            render();
            statusEl.textContent = '✗ 加载失败: ' + e.message;
        }
    }

    // 后台连接完成后刷新状态卡（GET 只触发预热，不会等握手完成）
    function scheduleStatusPoll(uuid) {
        clearTimeout(pollTimer);
        const pending = Object.values(servers).some(
            s => s.status === 'connecting' || s.status === 'pending'
        );
        if (!pending) { pollAttempts = 0; return; }
        if (pollAttempts >= MAX_POLL_ATTEMPTS) return;
        pollAttempts += 1;
        pollTimer = setTimeout(() => {
            const modal = document.getElementById('workspace-settings-modal');
            if (loadedUuid === uuid && modal && modal.style.display !== 'none') {
                load(uuid);
            }
        }, 1500);
    }

    function render() {
        const container = document.getElementById('mcp-server-list');
        const names = Object.keys(servers);
        if (names.length === 0) {
            container.innerHTML = '<div class="settings-empty" style="margin-bottom: 14px;">该工作区尚未配置 MCP 服务器。</div>';
            return;
        }
        container.innerHTML = names.map(name => {
            const s = servers[name];
            const target = (s.url || '').trim();
            const headersInfo = Object.keys(s.headers_masked || {}).map(k => `${k}=${s.headers_masked[k]}`).join(', ');
            return `
            <div class="settings-card mcp-server-card">
                <div class="settings-card-head">
                    <span class="title">${escapeHtml(name)}</span>
                    ${statusChip(s)}
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
        container.querySelectorAll('.mcp-remove-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                delete servers[btn.dataset.name];
                render();
                await save();
            });
        });
        container.querySelectorAll('.mcp-test-btn').forEach(btn => {
            btn.addEventListener('click', () => testServer(btn.dataset.name, btn));
        });
        container.querySelectorAll('.mcp-expand-btn').forEach(btn => {
            btn.addEventListener('click', () => toggleTools(btn));
        });
    }

    // 已保存服务器：密钥不回传前端，保存时让后端继承原 headers；
    // 本次新增但保存失败的服务器：带上前端内存里的真实 headers
    function collect() {
        const result = {};
        for (const [name, s] of Object.entries(servers)) {
            const entry = {
                url: s.url || '',
                tool_timeout: s.tool_timeout != null ? s.tool_timeout : 30,
                enabled_tools: s.enabled_tools || ['*']
            };
            if (s._loaded) {
                entry._preserve_headers = true;
            } else {
                entry.headers = s.headers || {};
            }
            result[name] = entry;
        }
        return result;
    }

    async function save() {
        const uuid = loadedUuid;
        if (!uuid) return false;
        const statusEl = document.getElementById('mcp-status');
        statusEl.textContent = '保存中...';
        const payload = collect();
        try {
            const response = await fetch('/api/mcp/servers', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ workspace: uuid, servers: payload })
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || 'HTTP ' + response.status);
            showToast('MCP 配置已保存');
            await load(uuid);
            return true;
        } catch (e) {
            statusEl.textContent = '✗ 保存失败: ' + e.message;
            return false;
        }
    }

    function formConfig() {
        return {
            url: document.getElementById('mcp-url').value.trim(),
            headers: parseKv(document.getElementById('mcp-headers').value.trim()),
            tool_timeout: parseInt(document.getElementById('mcp-tool-timeout').value) || 30,
            enabled_tools: (document.getElementById('mcp-enabled-tools').value.trim() || '*')
                .split(',').map(s => s.trim()).filter(Boolean)
        };
    }

    function clearForm() {
        ['mcp-name', 'mcp-url', 'mcp-headers'].forEach(id => {
            document.getElementById(id).value = '';
        });
    }

    async function addServer() {
        const name = document.getElementById('mcp-name').value.trim();
        if (!name) { showToast('请先填写服务器名称'); return; }
        if (!loadedUuid) { showToast('请先选择工作区'); return; }
        if (servers[name]) { showToast('服务器名称已存在：' + name); return; }
        if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]*$/.test(name)) {
            showToast('名称只能包含字母、数字、_ . -，且不能以符号开头');
            return;
        }
        const cfg = formConfig();
        servers[name] = {
            url: cfg.url,
            tool_timeout: cfg.tool_timeout,
            enabled_tools: cfg.enabled_tools,
            headers: cfg.headers,
            headers_masked: Object.fromEntries(Object.entries(cfg.headers || {}).map(([k, v]) =>
                [k, v.length > 8 ? v.slice(0, 4) + '...' + v.slice(-4) : '***'])),
            status: 'pending',
            tool_count: 0
        };
        render();
        const saved = await save();
        if (saved) clearForm();
    }

    function testConfigFor(name) {
        const s = servers[name] || {};
        return {
            url: s.url || '',
            headers: s.headers || {},
            tool_timeout: s.tool_timeout != null ? s.tool_timeout : 30,
            enabled_tools: s.enabled_tools || ['*']
        };
    }

    async function testServer(name, btn) {
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
            let response = await post({ name, workspace: loadedUuid });
            let data = await response.json();
            // 未保存/保存失败的服务器 → 回退用前端内存态配置测试
            if (data && data.status === 'failed' && /未找到已保存/.test(data.error || '')) {
                response = await post({ name, workspace: loadedUuid, config: testConfigFor(name) });
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

    function toggleTools(btn) {
        const row = btn.closest('.mcp-server-card');
        const listEl = row.querySelector('.mcp-tools-list');
        if (listEl.style.display !== 'none') {
            listEl.style.display = 'none';
            btn.textContent = '工具清单';
            return;
        }
        const tools = (servers[btn.dataset.name] || {}).tools || [];
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

    async function testNew() {
        const name = document.getElementById('mcp-name').value.trim();
        const resultEl = document.getElementById('mcp-test-new-result');
        if (!name) { resultEl.textContent = '请先填写服务器名称'; return; }
        resultEl.textContent = '测试中...';
        resultEl.style.color = 'var(--text-secondary)';
        try {
            const response = await fetch('/api/mcp/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, workspace: loadedUuid, config: formConfig() })
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

    async function reload() {
        if (!loadedUuid) return;
        const statusEl = document.getElementById('mcp-status');
        statusEl.textContent = '重连中...';
        try {
            const response = await fetch(
                '/api/mcp/reload?workspace=' + encodeURIComponent(loadedUuid),
                { method: 'POST' }
            );
            if (!response.ok) throw new Error('HTTP ' + response.status);
            const data = await response.json();
            const counts = Object.entries(data.servers || {}).map(([n, s]) =>
                `${n}:${s.status === 'connected' ? '已连接' : s.status}`
            ).join(', ');
            statusEl.textContent = counts || '无服务器';
            showToast('MCP 已重新连接');
            await load(loadedUuid);
        } catch (e) {
            statusEl.textContent = '重连失败: ' + e.message;
        }
    }

    function bindEvents() {
        if (bound) return;
        bound = true;
        document.getElementById('mcp-add-btn')?.addEventListener('click', addServer);
        document.getElementById('mcp-test-new-btn')?.addEventListener('click', testNew);
        document.getElementById('mcp-reload-btn')?.addEventListener('click', reload);
    }

    window.McpSettings = {
        init(uuid) {
            bindEvents();
            load(uuid);
        }
    };
})();
