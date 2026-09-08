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

// 打开全局设置弹窗
async function openSettings() {
    document.getElementById('settings-modal').style.display = 'flex';
    document.getElementById('settings-status').textContent = '加载中...';

    // Reset to RootAgent model tab
    document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('.settings-tab[data-tab="root"]').classList.add('active');
    document.getElementById('tab-root').classList.add('active');

    try {
        const response = await fetch('/api/config');
        const data = await response.json();
        const cfg = data.config || {};
        const modelCfg = cfg.model || {};
        const llmModelCfg = cfg.llm_model || {};

        // ── RootAgent model settings ──
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

        // ── LLM model settings ──
        document.getElementById('llm-api-key').value = '';
        document.getElementById('llm-base-url').value = llmModelCfg.base_url || '';
        document.getElementById('llm-model').value = llmModelCfg.name || '';
        document.getElementById('llm-interface-type').value = llmModelCfg.interface_type || '';
        document.getElementById('llm-max-tokens').value = llmModelCfg.max_tokens || 16384;
        document.getElementById('llm-max-context-tokens').value = llmModelCfg.max_context_tokens || 256000;
        document.getElementById('llm-multimodal').checked = llmModelCfg.multimodal !== false;

        // LLM Temperature slider
        const llmTempVal = llmModelCfg.temperature != null ? llmModelCfg.temperature : 0.2;
        document.getElementById('llm-temperature').value = llmTempVal;
        document.getElementById('llm-temperature-value').textContent = llmTempVal;

        // LLM Reasoning effort
        document.getElementById('llm-reasoning-effort').value = llmModelCfg.reasoning_effort || '';

        // Show LLM masked key
        const llmMaskedHint = document.getElementById('llm-api-key-masked');
        if (llmModelCfg.api_key_masked) {
            llmMaskedHint.textContent = `当前: ${llmModelCfg.api_key_masked} (留空保持不变)`;
        } else if (modelCfg.api_key_masked) {
            llmMaskedHint.textContent = '未单独配置（将使用 RootAgent 模型的 Key）';
        } else {
            llmMaskedHint.textContent = '未配置';
        }

        // ── System parameters ──
        const systemCfg = cfg.system || {};
        document.getElementById('system-pip-mirror').value = systemCfg.pip_mirror || '';
        document.getElementById('system-browser-path').value = systemCfg.browser_path || '';
        document.getElementById('system-search-engine').value = systemCfg.search_engine || 'bing';
        document.getElementById('system-mineru-api-key').value = '';
        document.getElementById('system-allowed-ips').value = (systemCfg.allowed_ips || []).join(', ');
        document.getElementById('system-max-iterations').value = systemCfg.max_iterations || 200;

        // Show MinerU masked key in placeholder
        const mineruInput = document.getElementById('system-mineru-api-key');
        if (systemCfg.mineru_api_key_masked) {
            mineruInput.placeholder = `当前: ${systemCfg.mineru_api_key_masked}（留空保持不变，清空请填写none）`;
        } else {
            mineruInput.placeholder = '留空则使用免费 Agent API（≤10MB/≤20页）';
        }

        document.getElementById('settings-status').textContent = `配置文件: ${data.config_path}`;
    } catch (error) {
        document.getElementById('settings-status').textContent = '加载失败: ' + error.message;
    }
}

// 保存全局设置
async function saveSettings() {
    const statusEl = document.getElementById('settings-status');
    statusEl.textContent = '保存中...';

    const payload = {
        model: {},
        llm_model: {}
    };

    // ── RootAgent model settings ──
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

    // ── LLM model settings ──
    const llmApiKey = document.getElementById('llm-api-key').value.trim();
    if (llmApiKey) payload.llm_model.api_key = llmApiKey;

    const llmBaseUrl = document.getElementById('llm-base-url').value.trim();
    if (llmBaseUrl) payload.llm_model.base_url = llmBaseUrl;

    const llmModel = document.getElementById('llm-model').value.trim();
    if (llmModel) payload.llm_model.name = llmModel;

    const llmInterfaceType = document.getElementById('llm-interface-type').value;
    if (llmInterfaceType) {
        payload.llm_model.interface_type = llmInterfaceType;
    }

    const llmMaxTokens = document.getElementById('llm-max-tokens').value;
    if (llmMaxTokens) payload.llm_model.max_tokens = parseInt(llmMaxTokens);

    const llmMaxContextTokens = document.getElementById('llm-max-context-tokens').value;
    if (llmMaxContextTokens) payload.llm_model.max_context_tokens = parseInt(llmMaxContextTokens);

    payload.llm_model.multimodal = document.getElementById('llm-multimodal').checked;
    payload.llm_model.temperature = parseFloat(document.getElementById('llm-temperature').value);
    payload.llm_model.reasoning_effort = document.getElementById('llm-reasoning-effort').value;

    // If LLM model has no name, clear it (unconfigured)
    if (!payload.llm_model.name) {
        payload.llm_model = { name: '', api_key: '' };  // Signal to backend to remove
    }

    // ── System parameters ──
    const pipMirror = document.getElementById('system-pip-mirror').value.trim();
    const browserPath = document.getElementById('system-browser-path').value.trim();
    const searchEngine = document.getElementById('system-search-engine').value;
    const mineruApiKey = document.getElementById('system-mineru-api-key').value.trim();
    const allowedIpsStr = document.getElementById('system-allowed-ips').value.trim();
    const allowedIps = allowedIpsStr ? allowedIpsStr.split(',').map(ip => ip.trim()).filter(Boolean) : [];
    const maxIterations = document.getElementById('system-max-iterations').value;
    payload.system = {
        pip_mirror: pipMirror,
        browser_path: browserPath,
        search_engine: searchEngine,
        allowed_ips: allowedIps,
        max_iterations: parseInt(maxIterations) || 200
    };
    // Only include mineru_api_key if user typed a new value; empty means "don't change"
    if (mineruApiKey) {
        payload.system.mineru_api_key = mineruApiKey;
    }

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

// 测试 LLM 连接
async function testSettings(which = 'model') {
    const statusEl = document.getElementById('settings-status');
    statusEl.textContent = '测试连接中...';

    const isLlmTab = which === 'llm';

    let payload = {};
    if (isLlmTab) {
        // Test LLM model config
        const llmModel = document.getElementById('llm-model').value.trim();
        const llmInterfaceType = document.getElementById('llm-interface-type').value;
        const llmBaseUrl = document.getElementById('llm-base-url').value.trim();
        const llmApiKey = document.getElementById('llm-api-key').value.trim();

        if (!llmModel) {
            statusEl.textContent = '✗ 请先填写 LLM 模型名称';
            return;
        }

        payload = {
            config: {
                name: llmModel,
                interface_type: llmInterfaceType || 'anthropic',
            }
        };
        if (llmBaseUrl) payload.config.base_url = llmBaseUrl;
        if (llmApiKey) payload.config.api_key = llmApiKey;
    } else {
        // Test Agent model config
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
            const modelLabel = isLlmTab ? 'LLM 模型' : 'Agent 模型';
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
