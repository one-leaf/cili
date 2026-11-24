// ── Providers / 模型设置页签：预设、模型行、探测/测试、ChatGPT OAuth 登录 ──
// 依赖 settings.js 的 server presets 全局（settingsServerPresets）与表单工具。

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
