// ── 输入区（composer）：按钮状态、推荐输入、模型下拉、附件上传 ──
// 依赖 app.js/chat.js 的会话状态与全局 DOM（chatInput、composerBtn 等）。

// ── 输入区按钮状态（发送 / 排队发送 / 停止）──
// 单点决策：所有分支结束后都调它，不再在 fetch 分支里直接改按钮文案/配色。
function hasDraft() {
    return chatInput.value.trim().length > 0
        || pendingAttachments.length > 0
        || (typeof getActiveEditorContext === 'function' && getActiveEditorContext() !== null);
}

// 右下角单按钮图标（Claude Code 插件式）：发送=纸飞机，停止=方块
const COMPOSER_SEND_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"></path><path d="M22 2l-7 20-4-9-9-4 20-7z"></path></svg>';
const COMPOSER_STOP_ICON = '<svg width="10" height="10" viewBox="0 0 10 10" fill="currentColor"><rect x="1" y="1" width="8" height="8" rx="1.5"></rect></svg>';

function updateComposerButtons() {
    const hasSession = !!currentSession;
    const draft = hasDraft();
    const bgActive = !!(sessionBackground && sessionBackground.active > 0);
    // 红只表示 master；黄只表示后台；有草稿永远是发送。
    let state = 'disabled';
    if (hasSession) {
        if (draft) {
            state = 'send';
        } else if (sessionRunning) {
            state = 'stop';           // 红：停 master
        } else if (bgActive) {
            state = 'stop-bg';        // 黄：停全部后台
        }
    }
    composerBtn.dataset.state = state;
    composerBtn.innerHTML = (state === 'stop' || state === 'stop-bg')
        ? COMPOSER_STOP_ICON : COMPOSER_SEND_ICON;

    let title = '发送';
    if (state === 'stop') {
        title = '停止当前回合';
    } else if (state === 'stop-bg') {
        title = '停止后台任务';
    } else if (state === 'send' && sessionRunning) {
        title = '排队发送';
    }
    composerBtn.title = title;
    composerBtn.setAttribute('aria-label', title);
    composerBtn.disabled = state === 'disabled' || (state === 'stop' && isStopping);
    // 有会话时输入框常开（运行中也能输入）；无会话才禁用
    chatInput.disabled = !hasSession;
    // 模型下拉：无会话或运行中（含前台请求/后台回合）不可切换
    if (!hasSession && modelSelect.dataset.currentRef) {
        modelSelect.dataset.currentRef = '';
        modelSelect.innerHTML = '<option>模型</option>';
    }
    modelSelect.disabled = !hasSession || sessionRunning || isSending || modelSelectBusy;

    // 推荐输入：回合结束（含发送中）、无待答 ask_user/审批卡、未在生成时才可用
    if (suggestBtn) {
        const pendingAsk = typeof hasPendingAskUser === 'function' && hasPendingAskUser();
        suggestBtn.disabled = !hasSession || sessionRunning || isSending
            || suggestLoading || pendingAsk;
        if (!hasSession) {
            suggestBtn.title = '推荐输入';
        } else if (pendingAsk) {
            suggestBtn.title = '请先回答上方的问题';
        } else if (sessionRunning || isSending) {
            suggestBtn.title = '本轮完成后可用';
        } else {
            suggestBtn.title = '推荐输入';
        }
    }
}

// ── 推荐输入（suggest）────────────────────────────────────────────────
// 设计：workspace/default/cili-input-suggest-plan-2026-11-13.md
// 仅在回合结束、无待答 ask_user、未生成中可用；点击气泡只填充不发送。

function hideSuggestBar({ invalidate = false } = {}) {
    if (invalidate) {
        suggestRequestSeq++;  // 丢弃在途/过期响应
        suggestHistory.clear();
        suggestDraftMode = false;
    }
    suggestItems = [];
    suggestLoading = false;
    if (suggestBtn) suggestBtn.classList.remove('suggest-loading');
    if (suggestBar) {
        suggestBar.hidden = true;
        suggestBar.innerHTML = '';
    }
    updateComposerButtons();
}

async function handleSuggest() {
    if (suggestLoading) return;
    if (suggestBar && !suggestBar.hidden) {
        hideSuggestBar();  // 已弹出则收起
        return;
    }
    await requestSuggestions(false);
}

async function requestSuggestions(refresh) {
    if (!currentWorkspace || !currentSession) return;
    // 输入框有草稿：带上它生成「改写/美化」建议；草稿已在最近一批里则视为误触，不执行
    const draft = (chatInput && chatInput.value ? chatInput.value : '').trim();
    if (draft && suggestHistory.has(draft)) return;
    const workspaceUuid = currentWorkspace.uuid;
    const sessionId = currentSession.session_id;
    const seq = ++suggestRequestSeq;
    suggestLoading = true;
    if (suggestBtn) suggestBtn.classList.add('suggest-loading');
    updateComposerButtons();

    try {
        const resp = await fetch(
            `/api/workspaces/${workspaceUuid}/sessions/${sessionId}/suggest`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    refresh: !!refresh,
                    exclude: refresh ? suggestItems.slice() : [],
                    input: draft,
                }),
            }
        );
        // 过期判定：序号变化、会话切换/新建、已开始新回合
        if (seq !== suggestRequestSeq) return;
        if (!currentSession || currentSession.session_id !== sessionId) return;

        if (!resp.ok) {
            let detail = '';
            try {
                detail = (await resp.json()).detail || '';
            } catch (e) { /* 非 JSON 错误体，用兜底文案 */ }
            showToast(detail || (resp.status === 503
                ? '暂时拿不到建议，请稍后再试'
                : '获取建议失败，请稍后再试'));
            return;
        }
        const data = await resp.json();
        const items = Array.isArray(data.suggestions) ? data.suggestions : [];
        if (!items.length) {
            showToast('暂时拿不到建议，请稍后再试');
            return;
        }
        renderSuggestBar(items, { fromDraft: !!draft });
    } catch (error) {
        if (seq === suggestRequestSeq) {
            console.error('获取推荐输入失败:', error);
            showToast('获取建议失败，请稍后再试');
        }
    } finally {
        if (seq === suggestRequestSeq) {
            suggestLoading = false;
            if (suggestBtn) suggestBtn.classList.remove('suggest-loading');
            updateComposerButtons();
        }
    }
}

function renderSuggestBar(items, { fromDraft = false } = {}) {
    if (!suggestBar) return;
    suggestItems = items.slice();
    // 记住整批（收起气泡后仍生效）：再次以同一条草稿请求视为误触
    suggestHistory = new Set(items);
    suggestDraftMode = !!fromDraft;
    suggestBar.innerHTML = '';
    items.forEach((text) => {
        const pill = document.createElement('button');
        pill.type = 'button';
        pill.className = 'suggest-pill';
        pill.textContent = text;  // textContent：防 XSS
        pill.title = text;
        pill.addEventListener('click', () => fillSuggestion(text));
        suggestBar.appendChild(pill);
    });
    const refreshChip = document.createElement('button');
    refreshChip.type = 'button';
    refreshChip.className = 'suggest-pill suggest-refresh';
    refreshChip.textContent = '换一批';
    refreshChip.addEventListener('click', (e) => {
        e.stopPropagation();
        if (!suggestLoading) requestSuggestions(true);
    });
    suggestBar.appendChild(refreshChip);
    suggestBar.hidden = false;
}

function fillSuggestion(text) {
    if (suggestDraftMode) {
        // 改写草稿：直接替换输入框内容
        chatInput.value = text;
    } else {
        const existing = chatInput.value.trim();
        chatInput.value = existing ? existing + ' ' + text : text;
    }
    chatInput.focus();
    hideSuggestBar();
}

// 输入区模型下拉：默认显示当前会话模型，允许切换（会话级覆盖）
async function refreshModelSelect() {
    if (!currentWorkspace || !currentSession) {
        modelSelect.innerHTML = '<option>模型</option>';
        modelSelect.dataset.currentRef = '';
        modelSelect.disabled = true;
        return;
    }
    const workspaceUuid = currentWorkspace.uuid;
    const sessionId = currentSession.session_id;
    modelSelectBusy = true;
    updateComposerButtons();
    try {
        const resp = await fetch(`/api/workspaces/${workspaceUuid}/sessions/${sessionId}/model`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        if (!currentSession || currentSession.session_id !== sessionId) return;
        const opts = (data.available || []).map(m => {
            const ref = m.ref || '';
            const name = m.name || ref;
            return `<option value="${escapeHtml(ref)}" title="${escapeHtml(ref)}">${escapeHtml(name)}</option>`;
        }).join('');
        modelSelect.innerHTML = opts || '<option>（无可用模型）</option>';
        modelSelect.dataset.currentRef = data.current_ref || '';
        modelSelect.value = data.current_ref || '';
        modelSelect.title = data.current_ref || '切换模型';
    } catch (e) {
        if (currentSession && currentSession.session_id === sessionId) {
            console.warn('加载模型列表失败:', e);
            modelSelect.innerHTML = '<option>模型</option>';
            modelSelect.dataset.currentRef = '';
            modelSelect.title = '模型列表加载失败';
        }
    } finally {
        modelSelectBusy = false;
        updateComposerButtons();
    }
}

async function onModelSelectChange() {
    if (!currentWorkspace || !currentSession) return;
    const workspaceUuid = currentWorkspace.uuid;
    const sessionId = currentSession.session_id;
    const ref = modelSelect.value;
    if (!ref || ref === modelSelect.dataset.currentRef) return;
    const previousRef = modelSelect.dataset.currentRef || '';
    modelSelectBusy = true;
    updateComposerButtons();
    try {
        const resp = await fetch(`/api/workspaces/${workspaceUuid}/sessions/${sessionId}/model`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_ref: ref }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
        if (currentSession && currentSession.session_id === sessionId) {
            modelSelect.dataset.currentRef = data.current_ref || ref;
            modelSelect.value = data.current_ref || ref;
            modelSelect.title = data.current_ref || ref;
        }
        showToast(`已切换模型：${data.current_name || ref}`);
        // 窗口上限随模型变化，立即刷新上下文指示器
        if (window.ContextGauge) ContextGauge.refresh();
    } catch (e) {
        console.error('切换模型失败:', e);
        modelSelect.value = previousRef;
        showToast('切换模型失败：' + (e.message || e));
    } finally {
        modelSelectBusy = false;
        updateComposerButtons();
    }
}

// ─── 上传到 uploads/ 并加入输入区附件 chip ────────────────────────────
// Ctrl+V、拖放共用：与文件浏览器「添加文件」同一套 pendingAttachments 流程，
// 文件随消息作为附件发送，不再把路径文本插入对话输入框。
async function uploadFilesAsAttachments(files) {
    let uploadedCount = 0;
    const errors = [];
    for (const file of files) {
        const result = await uploadFiles('uploads', [file]);
        const up = (result.uploaded || [])[0];
        if (up) {
            const rel = `uploads/${up.name}`;
            if (!pendingAttachments.some((a) => a.path === rel)) {
                pendingAttachments.push({
                    path: rel,
                    name: up.name,
                    media_type: file.type || 'application/octet-stream',
                });
            }
            uploadedCount += 1;
        }
        errors.push(...(result.errors || []));
    }
    if (uploadedCount > 0) {
        if (window.FileTree && typeof window.FileTree.refresh === 'function') {
            window.FileTree.refresh();
        }
        showToast(`已上传 ${uploadedCount} 个文件到 uploads`);
        renderComposerAttachments();
    }
    updateComposerButtons();
    return { uploadedCount, errors };
}

// Setup event listeners
