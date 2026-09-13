// ── memory.js ── 记忆管理（v3）
// 依赖 app.js 全局变量: currentWorkspace
// 依赖 utils.js 函数: escapeHtml, showToast

const MEMORY_TYPE_LABELS = {
    fact: '事实',
    preference: '偏好',
    skill: '技能',
    reference: '参考',
};
const MEMORY_STATUS_LABELS = {
    active: '活跃',
    stale: '待验证',
    archived: '已归档',
};

let memoryEntries = [];
let memorySelectedName = null;

function setupMemoryEvents() {
    const btn = document.getElementById('memory-btn');
    if (btn) btn.addEventListener('click', openMemory);

    document.getElementById('memory-consolidate-btn').addEventListener('click', consolidateMemory);
    document.getElementById('memory-enabled-toggle').addEventListener('change', toggleMemoryEnabled);
    document.getElementById('memory-type-filter').addEventListener('change', renderMemoryList);
    document.getElementById('memory-status-filter').addEventListener('change', renderMemoryList);
    document.getElementById('memory-search').addEventListener('input', renderMemoryList);
    document.getElementById('mem-save-btn').addEventListener('click', saveMemoryEntry);
    document.getElementById('mem-archive-btn').addEventListener('click', () => changeMemoryEntryStatus('archive'));
    document.getElementById('mem-restore-btn').addEventListener('click', () => changeMemoryEntryStatus('restore'));
    document.getElementById('mem-delete-btn').addEventListener('click', () => changeMemoryEntryStatus('delete'));
}

function openMemory() {
    if (!currentWorkspace) {
        showToast('请先选择工作区');
        return;
    }
    document.getElementById('memory-modal').style.display = 'flex';
    memorySelectedName = null;
    loadMemory();
}

async function loadMemory() {
    if (!currentWorkspace) return;
    const logEl = document.getElementById('memory-action-log');
    logEl.textContent = '加载中...';
    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/memory`);
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || `HTTP ${response.status}`);
        }
        const data = await response.json();
        memoryEntries = data.entries || [];
        renderMemoryStats(data);
        renderMemoryList();
        renderMemoryCommits(data.commits || []);
        document.getElementById('memory-enabled-toggle').checked = !!data.enabled;
        const pending = data.pending || 0;
        document.getElementById('memory-pending-label').textContent =
            pending > 0 ? `待整合 ${pending} 条` : '';
        logEl.textContent = '';
        if (memorySelectedName && memoryEntries.some(e => e.name === memorySelectedName)) {
            selectMemoryEntry(memorySelectedName);
        } else {
            resetMemoryDetail();
        }
    } catch (error) {
        console.error('加载记忆失败:', error);
        document.getElementById('memory-action-log').textContent = '✗ ' + error.message;
    }
}

function renderMemoryStats(data) {
    const s = data.stats || {};
    const byType = s.by_type || {};
    const counts = Object.entries(MEMORY_TYPE_LABELS)
        .map(([key, label]) => `${label} ${byType[key] ?? 0}`)
        .join(' · ');
    document.getElementById('memory-stats').innerHTML =
        `<span>共 ${s.total ?? 0} 条</span>` +
        `<span class="memory-stat-sep">·</span><span>${counts}</span>` +
        (s.stale ? `<span class="memory-stat-sep">·</span><span class="memory-stat-stale">待验证 ${s.stale}</span>` : '') +
        (s.archived ? `<span class="memory-stat-sep">·</span><span>已归档 ${s.archived}</span>` : '') +
        `<span class="memory-stat-sep">·</span><span class="memory-stat-index">索引 ${s.index_lines ?? 0} 行 / ${Math.round((s.index_bytes ?? 0) / 1024)}KB</span>`;
}

function filteredMemoryEntries() {
    const type = document.getElementById('memory-type-filter').value;
    const status = document.getElementById('memory-status-filter').value;
    const q = document.getElementById('memory-search').value.trim().toLowerCase();
    return memoryEntries.filter(e => {
        if (type && e.type !== type) return false;
        if (status && (e.status || 'active') !== status) return false;
        if (q) {
            const hay = [e.name, e.title, e.description, (e.tags || []).join(' ')]
                .join(' ').toLowerCase();
            if (!hay.includes(q)) return false;
        }
        return true;
    });
}

function renderMemoryList() {
    const listEl = document.getElementById('memory-list');
    const entries = filteredMemoryEntries();
    if (entries.length === 0) {
        listEl.innerHTML = '<div class="empty-state">没有记忆条目</div>';
        return;
    }
    listEl.innerHTML = '';
    entries.forEach(e => {
        const div = document.createElement('div');
        div.className = 'memory-list-item';
        if (e.name === memorySelectedName) div.classList.add('active');
        const status = e.status || 'active';
        const typeLabel = MEMORY_TYPE_LABELS[e.type] || e.type || '条目';
        const title = e.title || e.name;
        const desc = (e.description || '')
            .replace(/[\r\n]+/g, ' ');
        const staleMark = status === 'stale' ? '<span class="memory-status-tag stale">待验证</span>' : '';
        const archivedMark = status === 'archived' ? '<span class="memory-status-tag archived">已归档</span>' : '';
        div.innerHTML = `
            <div class="memory-list-item-title">
                <span class="memory-type-tag" data-type="${escapeHtml(e.type || '')}">${escapeHtml(typeLabel)}</span>
                <span class="memory-item-name">${escapeHtml(title)}</span>
                ${staleMark}${archivedMark}
            </div>
            ${desc ? `<div class="memory-item-desc">${escapeHtml(desc)}</div>` : ''}
            <div class="memory-item-meta">${escapeHtml(e.name || '')}${(e.tags && e.tags.length ? ' · ' + escapeHtml(e.tags.join(', ')) : '')}</div>
        `;
        div.addEventListener('click', () => selectMemoryEntry(e.name));
        listEl.appendChild(div);
    });
}

async function selectMemoryEntry(name) {
    if (!currentWorkspace) return;
    memorySelectedName = name;
    renderMemoryList();
    const detailEmpty = document.getElementById('memory-detail-empty');
    const detailContent = document.getElementById('memory-detail-content');
    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/memory/entries/${encodeURIComponent(name)}`);
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || `HTTP ${response.status}`);
        }
        const data = await response.json();
        const fm = data.entry || {};
        detailEmpty.style.display = 'none';
        detailContent.style.display = '';
        document.getElementById('mem-name').textContent = fm.name || name;
        document.getElementById('mem-title').value = fm.title || '';
        document.getElementById('mem-description').value = fm.description || '';
        document.getElementById('mem-tags').value = (fm.tags || []).join(', ');
        document.getElementById('mem-content').value = data.body || '';

        const refs = (fm.refs || []).length
            ? `<div class="memory-meta-row">引用: ${escapeHtml(fm.refs.join(', '))}</div>` : '';
        const status = fm.status || 'active';
        document.getElementById('mem-meta').innerHTML = `
            <div class="memory-meta-row">类型: ${escapeHtml(MEMORY_TYPE_LABELS[fm.type] || fm.type || '')} · 来源: ${escapeHtml(fm.source || '')}</div>
            <div class="memory-meta-row">创建: ${escapeHtml(fm.created || '')} · 更新: ${escapeHtml(fm.updated || '')}</div>
            <div class="memory-meta-row">使用 ${escapeHtml(String(fm.usage_count ?? 0))} 次 · 最近使用: ${escapeHtml(fm.last_used || '-')}</div>
            ${refs}
        `;
        const isArchived = status === 'archived';
        document.getElementById('mem-archive-btn').style.display = isArchived ? 'none' : '';
        document.getElementById('mem-restore-btn').style.display = isArchived ? '' : 'none';
    } catch (error) {
        console.error('加载记忆条目失败:', error);
        resetMemoryDetail();
        document.getElementById('memory-action-log').textContent = '✗ ' + error.message;
    }
}

function resetMemoryDetail() {
    memorySelectedName = null;
    document.getElementById('memory-detail-empty').style.display = '';
    document.getElementById('memory-detail-content').style.display = 'none';
}

async function saveMemoryEntry() {
    if (!currentWorkspace || !memorySelectedName) return;
    const payload = {
        title: document.getElementById('mem-title').value.trim(),
        description: document.getElementById('mem-description').value.trim(),
        tags: document.getElementById('mem-tags').value.split(/[,，]/).map(t => t.trim()).filter(Boolean),
        content: document.getElementById('mem-content').value,
    };
    try {
        const response = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/memory/entries/${encodeURIComponent(memorySelectedName)}`,
            {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            }
        );
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || '保存失败');
        }
        showToast('✓ 已保存');
        await loadMemory();
    } catch (error) {
        showToast('✗ ' + error.message);
    }
}

async function changeMemoryEntryStatus(action) {
    if (!currentWorkspace || !memorySelectedName) return;
    if (action === 'delete' && !confirm(`确定永久删除记忆「${memorySelectedName}」？不可恢复。`)) return;

    const logEl = document.getElementById('memory-action-log');
    logEl.textContent = action === 'delete' ? '删除中...' : '处理中...';
    try {
        const response = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/memory/entries/${encodeURIComponent(memorySelectedName)}/${action}`,
            { method: 'POST' }
        );
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || '操作失败');
        }
        const result = await response.json();
        logEl.textContent = result.committed ? `✓ 已${action === 'archive' ? '归档' : action === 'restore' ? '恢复' : '删除'}（已 git 提交）`
                                              : `✓ 已${action === 'archive' ? '归档' : action === 'restore' ? '恢复' : '删除'}`;
        memorySelectedName = null;
        await loadMemory();
    } catch (error) {
        logEl.textContent = '✗ ' + error.message;
    }
}

async function toggleMemoryEnabled() {
    if (!currentWorkspace) return;
    const enabled = document.getElementById('memory-enabled-toggle').checked;
    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/memory/settings`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ memory_enabled: enabled }),
        });
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || '设置失败');
        }
        showToast(enabled ? '✓ 已启用记忆' : '已停用记忆');
        await loadMemory();
    } catch (error) {
        document.getElementById('memory-enabled-toggle').checked = !enabled;
        showToast('✗ ' + error.message);
    }
}

async function consolidateMemory() {
    if (!currentWorkspace) return;
    const btn = document.getElementById('memory-consolidate-btn');
    const logEl = document.getElementById('memory-action-log');
    btn.disabled = true;
    logEl.textContent = '整合中（调用 lite 模型，可能需要几十秒）...';
    try {
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/memory/consolidate`, {
            method: 'POST',
        });
        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || '整合失败');
        }
        const r = await response.json();
        if (r.error) {
            logEl.textContent = '✗ 整合失败: ' + r.error;
        } else {
            logEl.textContent = `✓ 处理 ${r.processed} 条 · 新建 ${r.applied.filter(a => a.op === 'store').length} / 更新 ${r.applied.filter(a => a.op === 'update').length} · 归档 ${r.archived.length}` +
                (r.committed ? ' · 已 git 提交' : '') +
                (r.pending_after > 0 ? ` · 剩余待整合 ${r.pending_after}` : '');
        }
        await loadMemory();
    } catch (error) {
        logEl.textContent = '✗ ' + error.message;
    } finally {
        btn.disabled = false;
    }
}

function renderMemoryCommits(commits) {
    const el = document.getElementById('memory-commits');
    if (!commits || commits.length === 0) {
        el.innerHTML = '<span class="memory-commits-title">暂无 git 记录</span>';
        return;
    }
    const items = commits.map(c =>
        `<span class="memory-commit-item" title="${escapeHtml(c.date)}">${escapeHtml(c.hash)} ${escapeHtml(c.subject)}</span>`
    ).join('');
    el.innerHTML = `<span class="memory-commits-title">最近提交:</span>${items}`;
}

document.addEventListener('DOMContentLoaded', setupMemoryEvents);
