// ── 工作区设置面板（基础信息 / 项目提示词 / Git / 教材页签切换）──
// 依赖 app.js: currentWorkspace, showToast, loadSessions 等全局。

async function openWorkspaceSettings() {
    if (!currentWorkspace) return;

    document.getElementById('workspace-settings-modal').style.display = 'flex';
    const statusEl = document.getElementById('workspace-settings-status');
    statusEl.textContent = currentWorkspace.uuid;

    const nameInput = document.getElementById('ws-setting-name');
    const dirInput = document.getElementById('ws-setting-directory');
    const deleteBtn = document.getElementById('workspace-delete-btn');
    const saveBtn = document.getElementById('workspace-settings-save-btn');

    nameInput.value = currentWorkspace.name || '';
    dirInput.value = currentWorkspace.directory || '';
    dirInput.readOnly = true;

    // System workspace: read-only, no delete
    const isSystem = currentWorkspace.system || currentWorkspace.uuid === 'system';
    nameInput.disabled = isSystem;
    dirInput.disabled = isSystem;
    deleteBtn.style.display = isSystem ? 'none' : '';
    saveBtn.style.display = isSystem ? 'none' : '';
    const systemBadge = document.getElementById('ws-system-badge');
    if (systemBadge) systemBadge.hidden = !isSystem;

    // System 工作区不适用项目提示词 / 版本管理 / 教材下载 / MCP / 敏感词
    const systemHiddenTabs = ['ws-migration', 'ws-instructions', 'ws-git', 'ws-textbooks', 'ws-mcp', 'ws-sensitive'];
    document.querySelectorAll('#workspace-settings-modal .settings-tab').forEach(tabBtn => {
        if (systemHiddenTabs.includes(tabBtn.dataset.tab)) {
            tabBtn.style.display = isSystem ? 'none' : '';
        }
    });

    if (isSystem) {
        statusEl.textContent = `${currentWorkspace.uuid}（系统工作区，不可修改）`;
    }

    // 切换到基本设置 tab 并加载项目提示词
    switchWorkspaceSettingsTab('ws-basic');
    loadInstructions();
    loadTemplateList();
    loadGitSettings();
    initWorkspaceMigration();
}


async function saveWorkspaceSettings() {
    if (!currentWorkspace) return;

    const statusEl = document.getElementById('workspace-settings-status');
    statusEl.textContent = '保存中...';
    statusEl.className = 'settings-status';

    const name = document.getElementById('ws-setting-name').value.trim();

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name })
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Failed to save');
        }

        statusEl.textContent = '✓ 保存成功';
        statusEl.className = 'settings-status success';

        // Update current workspace info（目录只能通过数据迁移修改）
        currentWorkspace.name = name;

        // Refresh workspace path display
        updateWorkspacePath();

        // Refresh workspace list to update the dropdown
        await loadWorkspaces();

        // Re-select the current workspace
        workspaceSelect.value = currentWorkspace.uuid;

        setTimeout(() => {
            document.getElementById('workspace-settings-modal').style.display = 'none';
            statusEl.textContent = currentWorkspace.uuid;
            statusEl.className = 'settings-status';
        }, 1000);
    } catch (error) {
        statusEl.textContent = '✗ 保存失败: ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

async function deleteWorkspaceConfig() {
    if (!currentWorkspace) return;

    const wsName = currentWorkspace.name || currentWorkspace.uuid;
    const confirmed = confirm(
        `确定要删除工作区「${wsName}」的配置吗？\n\n` +
        `⚠ 将从工作区列表中移除该配置\n\n` +
        `✓ 工作区文件和会话数据不会被删除\n\n` +
        `此操作不可恢复！`
    );

    if (!confirmed) return;

    const statusEl = document.getElementById('workspace-settings-status');
    statusEl.textContent = '删除中...';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}`, { method: 'DELETE' });

        if (!response.ok) {
            let detail = `服务器错误 (${response.status})`;
            try {
                const err = await response.json();
                detail = err.detail || detail;
            } catch {
                // 响应不是 JSON，使用默认错误信息
            }
            throw new Error(detail);
        }

        statusEl.textContent = '✓ 删除成功';

        // Close modal and reset UI
        setTimeout(() => {
            document.getElementById('workspace-settings-modal').style.display = 'none';
            currentWorkspace = null;
            currentSession = null;
            clearPosition();
            workspaceSelect.value = '';
            sessionsList.innerHTML = '<div class="empty-state">选择一个工作区</div>';
            chatMessages.innerHTML = '<div class="welcome-message"><h2>欢迎使用草履虫</h2><p>请先选择工作区，再新建会话开始</p></div>';
            loadWorkspaces();
        }, 800);
    } catch (error) {
        statusEl.textContent = '✗ 删除失败: ' + error.message;
    }
}

// ─── 工作区设置 Tab 切换 ─────────────────────────────────────────

function switchWorkspaceSettingsTab(tabName) {
    // 切换 tab 按钮状态
    document.querySelectorAll('#workspace-settings-modal .settings-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabName);
    });
    // 切换 tab 内容
    document.querySelectorAll('#workspace-settings-modal .tab-content').forEach(content => {
        content.classList.toggle('active', content.id === `tab-${tabName}`);
    });
    // 切换 footer 按钮
    const footerSaveBtn = document.getElementById('workspace-settings-save-btn');
    const deleteBtn = document.getElementById('workspace-delete-btn');

    if (tabName === 'ws-migration') {
        // 数据迁移 tab：面板内按钮，隐藏底部保存/删除
        footerSaveBtn.style.display = 'none';
        deleteBtn.style.display = 'none';
        initWorkspaceMigration();
    } else if (tabName === 'ws-instructions') {
        // 项目提示词 tab：显示保存提示词按钮（靠右），隐藏删除按钮
        footerSaveBtn.textContent = '保存提示词';
        footerSaveBtn.style.display = '';
        footerSaveBtn.style.marginLeft = 'auto';
        footerSaveBtn.onclick = saveInstructions;
        deleteBtn.style.display = 'none';
    } else if (tabName === 'ws-git') {
        // 版本管理 tab：显示保存远程按钮，隐藏删除按钮
        footerSaveBtn.textContent = '保存远程设置';
        footerSaveBtn.style.display = '';
        footerSaveBtn.style.marginLeft = 'auto';
        footerSaveBtn.onclick = saveGitRemote;
        deleteBtn.style.display = 'none';
    } else if (tabName === 'ws-textbooks') {
        // 教材下载 tab：使用面板内按钮，隐藏底部保存/删除
        footerSaveBtn.style.display = 'none';
        deleteBtn.style.display = 'none';
        initTextbookPanel();
    } else if (tabName === 'ws-cron') {
        // 定时任务 tab：面板内按钮，隐藏底部保存/删除
        footerSaveBtn.style.display = 'none';
        deleteBtn.style.display = 'none';
        if (window.CronSettings) {
            window.CronSettings.init(currentWorkspace?.uuid || '');
        }
    } else if (tabName === 'ws-mcp') {
        // MCP 服务器 tab：增删即时保存，隐藏底部保存/删除
        footerSaveBtn.style.display = 'none';
        deleteBtn.style.display = 'none';
        if (window.McpSettings) {
            window.McpSettings.init(currentWorkspace?.uuid || '');
        }
    } else if (tabName === 'ws-sensitive') {
        // 敏感词 tab：显示保存规则按钮（靠右），隐藏删除按钮
        footerSaveBtn.textContent = '保存规则';
        footerSaveBtn.style.display = '';
        footerSaveBtn.style.marginLeft = 'auto';
        footerSaveBtn.onclick = saveSensitiveWords;
        deleteBtn.style.display = 'none';
        loadSensitiveWords();
    } else {
        // 基本设置 tab：显示 footer 按钮
        footerSaveBtn.textContent = '保存';
        footerSaveBtn.style.display = '';
        footerSaveBtn.style.marginLeft = '';
        footerSaveBtn.onclick = saveWorkspaceSettings;
        const isSystem = currentWorkspace?.system || currentWorkspace?.uuid === 'system';
        deleteBtn.style.display = isSystem ? 'none' : '';
    }
}

// ─── 项目提示词相关 ─────────────────────────────────────────────

async function loadInstructions() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('instructions-status');
    const editor = document.getElementById('instructions-editor');
    const filenameEl = document.getElementById('instructions-filename');

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/instructions`);
        const data = await response.json();

        editor.value = data.content || '';
        filenameEl.textContent = data.filename || 'AGENTS.md';
        if (!data.found) {
            statusEl.textContent = '未找到项目指令文件，将创建 AGENTS.md';
            statusEl.className = 'settings-status info';
        } else {
            statusEl.textContent = '';
        }
    } catch (error) {
        statusEl.textContent = '✗ 加载失败: ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

async function saveInstructions() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('instructions-status');
    const editor = document.getElementById('instructions-editor');
    const filenameEl = document.getElementById('instructions-filename');

    statusEl.textContent = '保存中...';
    statusEl.className = 'settings-status info';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/instructions`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filename: filenameEl.textContent,
                content: editor.value
            })
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Failed to save');
        }

        statusEl.textContent = '✓ 保存成功';
        statusEl.className = 'settings-status success';
    } catch (error) {
        statusEl.textContent = '✗ 保存失败: ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

async function generateInstructions() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('instructions-status');
    const generateBtn = document.getElementById('instructions-generate-btn');

    const confirmed = confirm(
        '将启动 AI 分析项目代码并生成 AGENTS.md。\n\n' +
        '此过程可能需要几分钟，期间会扫描项目结构和代码。\n\n' +
        '是否继续？'
    );
    if (!confirmed) return;

    generateBtn.disabled = true;
    generateBtn.textContent = '生成中...';
    statusEl.textContent = '正在启动 AI 生成...';
    statusEl.className = 'settings-status info';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/instructions/generate`, {
            method: 'POST'
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || 'Failed to start');
        }

        const data = await response.json();
        const taskId = data.task_id;

        // 轮询状态
        pollGenerateStatus(taskId);
    } catch (error) {
        statusEl.textContent = '✗ 启动失败: ' + error.message;
        statusEl.className = 'settings-status error';
        generateBtn.disabled = false;
        generateBtn.textContent = 'AI 生成';
    }
}

let generatePollTimer = null;

function stopGeneratePolling() {
    if (generatePollTimer) {
        clearTimeout(generatePollTimer);
        generatePollTimer = null;
    }
}

async function pollGenerateStatus(taskId) {
    const statusEl = document.getElementById('instructions-status');
    const generateBtn = document.getElementById('instructions-generate-btn');
    const editor = document.getElementById('instructions-editor');
    const filenameEl = document.getElementById('instructions-filename');
    // 轮询期间切换工作区后不能拿旧 taskId 去查新工作区
    const workspaceUuid = currentWorkspace?.uuid;

    const poll = async () => {
        generatePollTimer = null;
        if (!currentWorkspace || currentWorkspace.uuid !== workspaceUuid) return;
        try {
            const response = await fetch(`/api/workspaces/${workspaceUuid}/instructions/generate/${taskId}`);
            const data = await response.json();

            if (data.status === 'running') {
                statusEl.textContent = 'AI 正在分析代码...';
                generatePollTimer = setTimeout(poll, 3000);
            } else if (data.status === 'completed') {
                statusEl.textContent = '✓ 生成完成';
                statusEl.className = 'settings-status success';
                generateBtn.disabled = false;
                generateBtn.textContent = 'AI 生成';
                // 重新加载内容
                filenameEl.textContent = 'AGENTS.md';
                await loadInstructions();
            } else {
                statusEl.textContent = '✗ 生成失败: ' + (data.summary || 'Unknown error');
                statusEl.className = 'settings-status error';
                generateBtn.disabled = false;
                generateBtn.textContent = 'AI 生成';
            }
        } catch (error) {
            statusEl.textContent = '✗ 查询状态失败: ' + error.message;
            statusEl.className = 'settings-status error';
            generateBtn.disabled = false;
            generateBtn.textContent = 'AI 生成';
        }
    };

    stopGeneratePolling();
    poll();
}

async function loadTemplateList() {
    if (!currentWorkspace) return;
    const select = document.getElementById('instructions-template-select');

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/instructions/templates`);
        const data = await response.json();

        select.innerHTML = '<option value="">加载模板...</option>';
        for (const tpl of data.templates) {
            const option = document.createElement('option');
            option.value = tpl.name;
            option.textContent = tpl.name;
            select.appendChild(option);
        }
    } catch (error) {
        console.error('Failed to load templates:', error);
    }
}

async function loadTemplate() {
    if (!currentWorkspace) return;
    const select = document.getElementById('instructions-template-select');
    const templateName = select.value;
    if (!templateName) return;

    const statusEl = document.getElementById('instructions-status');
    const editor = document.getElementById('instructions-editor');

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/instructions/templates/${templateName}`);
        const data = await response.json();

        if (editor.value.trim()) {
            const confirmed = confirm('加载模板将覆盖当前内容，是否继续？');
            if (!confirmed) {
                select.value = '';
                return;
            }
        }

        editor.value = data.content;
        statusEl.textContent = `已加载模板: ${templateName}`;
        statusEl.className = 'settings-status info';
        select.value = '';
    } catch (error) {
        statusEl.textContent = '✗ 加载模板失败: ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

// ─── 工作区 Git 版本管理 ─────────────────────────────────────────

async function loadGitSettings() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('git-settings-status');
    const toggle = document.getElementById('git-enabled-toggle');
    const statusSection = document.getElementById('git-status-section');
    const commitsList = document.getElementById('git-commits-list');
    const remoteSection = document.getElementById('git-remote-section');

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/git/status`);
        const data = await response.json();

        toggle.checked = data.enabled;
        // 显示远程仓库区域
        if (remoteSection) {
            remoteSection.style.display = data.enabled ? '' : 'none';
        }
        if (data.initialized) {
            statusSection.style.display = '';
            renderGitCommits(data.commits);
        } else {
            statusSection.style.display = 'none';
            commitsList.innerHTML = '';
        }
        // 加载远程仓库信息
        const remoteInfo = data.remote_info;
        if (remoteInfo) {
            const urlInput = document.getElementById('git-remote-url');
            const usernameInput = document.getElementById('git-username');
            const tokenInput = document.getElementById('git-token');
            const infoEl = document.getElementById('git-remote-info');
            if (urlInput) urlInput.value = remoteInfo.remote_url || '';
            if (usernameInput) usernameInput.value = remoteInfo.username || '';
            // Token 不回显明文，只在有值时显示提示
            if (tokenInput) tokenInput.value = '';
            if (tokenInput) tokenInput.placeholder = remoteInfo.has_token ? `已配置 (${remoteInfo.token_preview})` : 'Personal Access Token';
            if (infoEl) {
                infoEl.textContent = remoteInfo.remote_url ? '已配置远程仓库，设置定时同步后每 2 小时自动拉取/推送' : '';
            }
        }
        statusEl.textContent = '';
        statusEl.className = 'settings-status';
    } catch (error) {
        statusEl.textContent = '✗ 加载 Git 设置失败: ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

async function toggleGitEnabled() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('git-settings-status');
    const toggle = document.getElementById('git-enabled-toggle');
    const enabled = toggle.checked;

    statusEl.textContent = enabled ? '正在启用...' : '正在关闭...';
    statusEl.className = 'settings-status info';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/git/settings`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ git_enabled: enabled })
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || '操作失败');
        }

        const data = await response.json();
        statusEl.textContent = enabled ? '✓ Git 版本管理已启用' : '✓ Git 版本管理已关闭';
        statusEl.className = 'settings-status success';

        // 刷新状态
        await loadGitSettings();
    } catch (error) {
        toggle.checked = !enabled;  // 回滚
        statusEl.textContent = '✗ ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

async function saveGitRemote() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('git-settings-status');
    const urlInput = document.getElementById('git-remote-url');
    const usernameInput = document.getElementById('git-username');
    const tokenInput = document.getElementById('git-token');

    statusEl.textContent = '保存中...';
    statusEl.className = 'settings-status info';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/git/settings`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                git_enabled: document.getElementById('git-enabled-toggle').checked,
                git_remote_url: urlInput.value.trim(),
                git_username: usernameInput.value.trim(),
                git_token: tokenInput.value.trim()
            })
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || '保存失败');
        }

        statusEl.textContent = '✓ 远程仓库设置已保存';
        statusEl.className = 'settings-status success';
        // 清空 token 输入框
        tokenInput.value = '';
        await loadGitSettings();
    } catch (error) {
        statusEl.textContent = '✗ ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

async function gitPull() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('git-settings-status');
    const pullBtn = document.getElementById('git-pull-btn');

    pullBtn.disabled = true;
    pullBtn.textContent = '拉取中...';
    statusEl.textContent = '正在拉取...';
    statusEl.className = 'settings-status info';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/git/pull`, { method: 'POST' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '拉取失败');
        statusEl.textContent = '✓ ' + data.message;
        statusEl.className = 'settings-status success';
        await loadGitSettings();
    } catch (error) {
        statusEl.textContent = '✗ ' + error.message;
        statusEl.className = 'settings-status error';
    } finally {
        pullBtn.disabled = false;
        pullBtn.textContent = '拉取';
    }
}

async function gitPush() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('git-settings-status');
    const pushBtn = document.getElementById('git-push-btn');

    pushBtn.disabled = true;
    pushBtn.textContent = '推送中...';
    statusEl.textContent = '正在推送...';
    statusEl.className = 'settings-status info';

    try {
        // 先静默同步远程地址到 git config
        const urlInput = document.getElementById('git-remote-url');
        const usernameInput = document.getElementById('git-username');
        const tokenInput = document.getElementById('git-token');
        await fetch(`/api/workspaces/${currentWorkspace.uuid}/git/settings`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                git_enabled: document.getElementById('git-enabled-toggle').checked,
                git_remote_url: urlInput.value.trim(),
                git_username: usernameInput.value.trim(),
                git_token: tokenInput.value.trim()
            })
        });
        // 推送
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/git/push`, { method: 'POST' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '推送失败');
        statusEl.textContent = '✓ ' + data.message;
        statusEl.className = 'settings-status success';
        tokenInput.value = '';
    } catch (error) {
        statusEl.textContent = '✗ ' + error.message;
        statusEl.className = 'settings-status error';
    } finally {
        pushBtn.disabled = false;
        pushBtn.textContent = '推送';
    }
}

function renderGitCommits(commits) {
    const list = document.getElementById('git-commits-list');
    if (!commits || commits.length === 0) {
        list.innerHTML = '<div class="git-commit-empty">暂无提交记录</div>';
        return;
    }
    list.innerHTML = commits.map(c => `
        <div class="git-commit-item">
            <span class="git-commit-hash">${c.hash}</span>
            <span class="git-commit-date">${c.date}</span>
            <span class="git-commit-subject">${escapeHtml(c.subject)}</span>
        </div>
    `).join('');
}

// 绑定事件
document.getElementById('git-enabled-toggle')?.addEventListener('change', toggleGitEnabled);
document.getElementById('git-refresh-btn')?.addEventListener('click', loadGitSettings);
document.getElementById('git-pull-btn')?.addEventListener('click', gitPull);
document.getElementById('git-push-btn')?.addEventListener('click', gitPush);

// ─── 工作区数据迁移 ─────────────────────────────────────────────

let workspaceMigrationPollTimer = null;

function stopWorkspaceMigrationPolling() {
    if (workspaceMigrationPollTimer) {
        clearTimeout(workspaceMigrationPollTimer);
        workspaceMigrationPollTimer = null;
    }
}

function formatMigrationBytes(value) {
    const num = Number(value) || 0;
    if (num <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let current = num;
    let index = 0;
    while (current >= 1024 && index < units.length - 1) {
        current /= 1024;
        index += 1;
    }
    return `${current.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function migrationPhaseText(phase) {
    const labels = {
        preparing: '准备中',
        copying: '复制中',
        verifying: '校验中',
        switching: '切换中',
        switched: '更新索引',
        cleaning: '清理源目录'
    };
    return labels[phase] || '迁移中';
}

async function initWorkspaceMigration() {
    if (!currentWorkspace) return;
    stopWorkspaceMigrationPolling();

    const uuid = currentWorkspace.uuid;
    document.getElementById('ws-migration-current').textContent = currentWorkspace.directory || '';

    const targetInput = document.getElementById('ws-migration-target');
    if (targetInput.dataset.uuid !== uuid) {
        targetInput.value = '';
        targetInput.dataset.uuid = uuid;
    }
    targetInput.disabled = false;

    const preflightEl = document.getElementById('ws-migration-preflight');
    preflightEl.textContent = '';
    preflightEl.className = 'settings-status';

    const statusEl = document.getElementById('ws-migration-status');
    statusEl.textContent = '';
    statusEl.className = 'settings-status';

    document.getElementById('ws-migration-start-btn').disabled = false;
    document.getElementById('ws-migration-cancel-btn').style.display = 'none';

    await loadWorkspaceMigrationStatus();
}

async function loadWorkspaceMigrationStatus() {
    stopWorkspaceMigrationPolling();
    if (!currentWorkspace) return;

    const uuid = currentWorkspace.uuid;
    let data;
    try {
        const response = await fetch(`/api/workspaces/${uuid}/migration`);
        data = await response.json();
    } catch {
        return;
    }
    if (!currentWorkspace || currentWorkspace.uuid !== uuid) return;
    if (!data || !data.status) return;

    const statusEl = document.getElementById('ws-migration-status');
    const startBtn = document.getElementById('ws-migration-start-btn');
    const cancelBtn = document.getElementById('ws-migration-cancel-btn');
    const targetInput = document.getElementById('ws-migration-target');

    if (data.active) {
        targetInput.disabled = true;
        startBtn.disabled = true;
        cancelBtn.style.display = ['preparing', 'copying', 'verifying'].includes(data.phase) ? '' : 'none';
        const done = data.files_done || 0;
        const total = data.files_total || 0;
        const pct = total ? Math.floor(done * 100 / total) : 0;
        statusEl.textContent = `${migrationPhaseText(data.phase)} ${done}/${total} 个文件（${pct}%）`;
        statusEl.className = 'settings-status info';
        workspaceMigrationPollTimer = setTimeout(loadWorkspaceMigrationStatus, 1000);
        return;
    }

    targetInput.disabled = false;
    startBtn.disabled = false;
    cancelBtn.style.display = 'none';

    if (data.status === 'done') {
        statusEl.textContent = '✓ 迁移完成';
        statusEl.className = 'settings-status success';
        await refreshAfterWorkspaceMigration(data);
    } else if (data.status === 'cancelled') {
        statusEl.textContent = '已取消（源目录未移动）';
        statusEl.className = 'settings-status info';
    } else {
        statusEl.textContent = '✗ 迁移失败：' + (data.error || '未知错误');
        statusEl.className = 'settings-status error';
    }
}

async function refreshAfterWorkspaceMigration(data) {
    if (!currentWorkspace || !data.dst) return;
    if (currentWorkspace.directory === data.dst) return;

    currentWorkspace.directory = data.dst;
    document.getElementById('ws-migration-current').textContent = data.dst;
    updateWorkspacePath();

    await loadWorkspaces();
    if (workspaceSelect) workspaceSelect.value = currentWorkspace.uuid;
    if (window.FileTree) FileTree.setWorkspace(currentWorkspace);
}

async function preflightWorkspaceMigration() {
    if (!currentWorkspace) return;
    const targetInput = document.getElementById('ws-migration-target');
    const targetPath = targetInput.value.trim();
    const preflightEl = document.getElementById('ws-migration-preflight');
    const startBtn = document.getElementById('ws-migration-start-btn');

    if (!targetPath) {
        preflightEl.textContent = '请填写完整目标路径';
        preflightEl.className = 'settings-status error';
        startBtn.disabled = true;
        return;
    }

    preflightEl.textContent = '预检中...';
    preflightEl.className = 'settings-status info';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/migration/preflight`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_path: targetPath })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '预检失败');

        if (data.blockers && data.blockers.length) {
            preflightEl.textContent = '无法迁移：' + data.blockers.join('；');
            preflightEl.className = 'settings-status error';
            startBtn.disabled = true;
            return;
        }

        const space = data.same_volume
            ? '同盘移动（不额外占用空间）'
            : `跨盘复制，需要约 ${formatMigrationBytes(data.required_bytes)}`;
        const warnings = (data.warnings || []).length
            ? `；注意：${data.warnings.join('；')}`
            : '';
        preflightEl.textContent =
            `源 ${formatMigrationBytes(data.total_bytes)} / ${data.file_count} 个文件；` +
            `${space}；目标盘可用 ${formatMigrationBytes(data.dest_free_bytes)}${warnings}`;
        preflightEl.className = 'settings-status success';
        startBtn.disabled = false;
    } catch (error) {
        preflightEl.textContent = '✗ ' + error.message;
        preflightEl.className = 'settings-status error';
        startBtn.disabled = true;
    }
}

async function startWorkspaceMigration() {
    if (!currentWorkspace) return;
    const targetPath = document.getElementById('ws-migration-target').value.trim();
    if (!targetPath) return;

    const confirmed = confirm(
        `确定把工作区「${currentWorkspace.name || currentWorkspace.uuid}」迁移到：\n\n` +
        `${targetPath}\n\n` +
        `迁移期间该工作区会短暂冻结，其他工作区不受影响。`
    );
    if (!confirmed) return;

    const statusEl = document.getElementById('ws-migration-status');
    statusEl.textContent = '正在启动迁移...';
    statusEl.className = 'settings-status info';

    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/migration`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_path: targetPath })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '启动失败');
        await loadWorkspaceMigrationStatus();
    } catch (error) {
        statusEl.textContent = '✗ ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

async function cancelWorkspaceMigration() {
    if (!currentWorkspace) return;
    const statusEl = document.getElementById('ws-migration-status');
    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/migration`, {
            method: 'DELETE'
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || '取消失败');
        statusEl.textContent = '正在取消...';
        statusEl.className = 'settings-status info';
    } catch (error) {
        statusEl.textContent = '✗ ' + error.message;
        statusEl.className = 'settings-status error';
    }
}

document.getElementById('ws-migration-preflight-btn')?.addEventListener('click', preflightWorkspaceMigration);
document.getElementById('ws-migration-start-btn')?.addEventListener('click', startWorkspaceMigration);
document.getElementById('ws-migration-cancel-btn')?.addEventListener('click', cancelWorkspaceMigration);
document.getElementById('ws-migration-target')?.addEventListener('input', () => {
    document.getElementById('ws-migration-start-btn').disabled = true;
});
document.getElementById('ws-migration-browse-btn')?.addEventListener('click', () => {
    const input = document.getElementById('ws-migration-target');
    openDirectoryBrowser(input.value.trim(), (selectedPath) => {
        input.value = selectedPath;
        preflightWorkspaceMigration();
    });
});
document.querySelectorAll('[data-modal="workspace-settings-modal"]').forEach(el => {
    el.addEventListener('click', stopWorkspaceMigrationPolling);
});
