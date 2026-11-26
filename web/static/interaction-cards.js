// ── 提问卡与审批卡渲染/提交（共享 ask-user/answers 管道）──
// 依赖 chat.js 的渲染容器与 sse/发送工具；hasPendingAskUser 亦供 app.js 使用。

function hasPendingAskUser() {
    return !!document.querySelector('.ask-user-card[data-ask-pending="1"]');
}

function settleAskUserCard(card) {
    if (card) card.removeAttribute('data-ask-pending');
}

// 渲染 AskUser 问题卡片
function renderAskUserQuestions(container, input, toolUseId) {
    const questions = input.questions || [];
    if (!questions.length) return;

    const card = document.createElement('div');
    card.className = 'ask-user-card';
    card.dataset.askPending = '1';
    if (toolUseId) card.dataset.toolUseId = toolUseId;

    const title = document.createElement('div');
    title.className = 'ask-title';
    title.textContent = '💬 Agent 需要你的输入';
    card.appendChild(title);

    // 每个问题一个区块
    const answers = {};  // question text → answer
    const questionBlocks = [];

    questions.forEach((q, idx) => {
        const qBlock = document.createElement('div');
        qBlock.className = 'ask-question';

        const header = document.createElement('span');
        header.className = 'ask-header';
        header.textContent = q.header || `问题 ${idx + 1}`;
        qBlock.appendChild(header);

        const qText = document.createElement('div');
        qText.className = 'ask-question-text';
        qText.textContent = q.question;
        qBlock.appendChild(qText);

        const optionsDiv = document.createElement('div');
        optionsDiv.className = 'ask-options';

        const multiSelect = q.multi_select || false;
        let selectedOptions = new Set();

        (q.options || []).forEach((opt, optIdx) => {
            const optEl = document.createElement('div');
            optEl.className = 'ask-option';
            optEl.dataset.optIdx = optIdx;

            const label = document.createElement('div');
            label.className = 'ask-option-label';
            label.textContent = opt.label;
            optEl.appendChild(label);

            const desc = document.createElement('div');
            desc.className = 'ask-option-desc';
            desc.textContent = opt.description;
            optEl.appendChild(desc);

            optEl.addEventListener('click', () => {
                if (multiSelect) {
                    if (selectedOptions.has(optIdx)) {
                        selectedOptions.delete(optIdx);
                        optEl.classList.remove('selected');
                    } else {
                        selectedOptions.add(optIdx);
                        optEl.classList.add('selected');
                    }
                } else {
                    // 单选：清除同问题的其他选项
                    optionsDiv.querySelectorAll('.ask-option.selected').forEach(el => el.classList.remove('selected'));
                    selectedOptions.clear();
                    selectedOptions.add(optIdx);
                    optEl.classList.add('selected');
                }
                // 更新答案
                const selectedLabels = [...selectedOptions].map(i => q.options[i].label);
                answers[q.question] = multiSelect ? selectedLabels.join(', ') : (selectedLabels[0] || '');
                updateSubmitBtn();
            });

            optionsDiv.appendChild(optEl);
        });

        // "Other" 自由输入
        const otherDiv = document.createElement('div');
        otherDiv.className = 'ask-option ask-other';
        otherDiv.innerHTML = '<div class="ask-option-label">其他</div>';
        const otherInput = document.createElement('input');
        otherInput.type = 'text';
        otherInput.className = 'ask-other-input';
        otherInput.placeholder = '输入自定义答案...';
        otherDiv.appendChild(otherInput);

        otherInput.addEventListener('focus', () => {
            // 清除选项选择
            optionsDiv.querySelectorAll('.ask-option.selected').forEach(el => el.classList.remove('selected'));
            selectedOptions.clear();
            otherDiv.classList.add('selected');
        });
        otherInput.addEventListener('input', () => {
            answers[q.question] = otherInput.value;
            updateSubmitBtn();
        });
        otherInput.addEventListener('blur', () => {
            if (!otherInput.value) {
                otherDiv.classList.remove('selected');
            }
        });

        qBlock.appendChild(optionsDiv);
        qBlock.appendChild(otherDiv);
        card.appendChild(qBlock);
        questionBlocks.push({ qBlock, otherInput });
    });

    // 提交按钮
    const submitBtn = document.createElement('button');
    submitBtn.className = 'btn btn-primary ask-submit';
    submitBtn.textContent = '提交';
    submitBtn.disabled = true;

    function updateSubmitBtn() {
        // 所有问题都有答案时启用
        const allAnswered = questions.every(q => {
            const a = answers[q.question];
            return a && a.trim() !== '';
        });
        submitBtn.disabled = !allAnswered;
    }

    function formatAnswers() {
        const parts = [];
        questions.forEach((q, idx) => {
            const answer = answers[q.question] || '';
            const otherInput = questionBlocks[idx]?.otherInput;
            const display = answer || (otherInput?.value || '');
            parts.push(`${q.question} ${display}`);
        });
        return parts.join('\n');
    }

    submitBtn.addEventListener('click', async () => {
        if (isSending) return;
        const answer = formatAnswers();
        if (!answer.trim()) return;

        // 捕获提交时刻的会话：本流的帧要按它过滤，期间用户可能切走
        const sessionId = currentSession.session_id;
        submitBtn.textContent = '提交中...';
        submitBtn.disabled = true;
        // 提交答案同样会跑一个前台回合：置位两个标志，让事件流那边的
        // 同名渲染器让位（否则本回合正文会被请求级流与全局流各渲染一遍）
        isSending = true;
        foregroundStreamSessionId = sessionId;
        sessionRunning = true;
        updateComposerButtons();

        try {
            // 调用 ask-user/answers API
            const response = await fetch(
                `/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}/ask-user/answers`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ tool_use_id: toolUseId, answer })
                }
            );

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${await response.text()}`);
            }

            // 标记卡片为已提交
            submitBtn.textContent = '✓ 已提交';
            submitBtn.classList.add('ask-submitted');
            settleAskUserCard(card);
            card.querySelectorAll('.ask-option').forEach(el => el.style.pointerEvents = 'none');
            card.querySelectorAll('input').forEach(el => el.disabled = true);
            updateComposerButtons();

            // 处理 SSE 流
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            // 与 sendMessage 同样的专属容器：切走后内容留着，切回时挂回
            const turnContainer = document.createElement('div');
            turnContainer.className = 'turn-stream';
            chatMessages.appendChild(turnContainer);
            activeTurnContainers.set(sessionId, turnContainer);
            const addTurnMessage = (role, content) =>
                addMessage(role, content, undefined, undefined, turnContainer);
            const scrollIfViewing = () => {
                if (currentSession && currentSession.session_id === sessionId) {
                    scrollChatIfNearBottom();
                }
            };

            const renderer = createTurnStreamRenderer({
                sessionId, turnContainer, addTurnMessage, scrollIfViewing,
            });

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';

                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    const dataStr = line.slice(6).trim();
                    if (!dataStr || dataStr === '[DONE]') continue;

                    try {
                        const event = JSON.parse(dataStr);

                        // 丢弃不属于本流的帧
                        if (event.session_id && event.session_id !== sessionId) continue;

                        renderer.handleEvent(event);
                    } catch (e) {
                        console.error('Failed to parse SSE event:', e, dataStr);
                    }
                }
            }

            // 回合已结束并落盘：先 flush 节流中的渲染，再注销容器
            renderer.flush();
            activeTurnContainers.delete(sessionId);

            // Refresh session to get persisted state（仅当仍停留在该会话时，
            // 避免切走后把用户拉回旧会话）
            if (currentSession && currentSession.session_id === sessionId) {
                await loadSession(sessionId);
            }

        } catch (error) {
            console.error('Failed to submit answer:', error);
            submitBtn.textContent = '提交失败';
            submitBtn.disabled = false;
            if (currentSession && currentSession.session_id === sessionId) {
                addMessage('assistant', '提交答案失败: ' + error.message);
            }
        } finally {
            // 异常路径下也要注销容器，避免它一直挂在消息区
            activeTurnContainers.delete(sessionId);
            // 只清理属于本会话的标志：期间可能已切走（此时 isSending 由切会话
            // 时的 loadSession 按新会话 /status 重置）或已在别的会话发起新请求
            if (currentSession && currentSession.session_id === sessionId) {
                isSending = false;
                isStopping = false;
                updateComposerButtons();
            }
            if (foregroundStreamSessionId === sessionId) {
                foregroundStreamSessionId = null;
            }
        }
    });

    card.appendChild(submitBtn);
    container.appendChild(card);
    // 提问卡是阻塞交互：强制回底，别让用户错过要回答的问题。
    // 卡片若挂在已脱离文档的旧会话容器里（用户已切走），不能动当前视图。
    if (chatMessages.contains(container)) scrollChatIfNearBottom(true);
    // 新卡片出现即刷新推荐按钮门控（历史加载/实时推送共用本函数）
    if (typeof updateComposerButtons === 'function') updateComposerButtons();
}

// ── 审批卡（与提问卡分开渲染，共享 ask-user/answers 管道） ──

function isApprovalInput(input) {
    const q = input && input.questions && input.questions[0];
    return !!(q && q.intent && q.intent.kind === 'approval');
}

// 倒计时：单个全局定时器，扫描带 data-deadline 的卡片
setInterval(() => {
    document.querySelectorAll('.approval-card[data-deadline]').forEach(card => {
        const deadline = parseInt(card.dataset.deadline, 10) || 0;
        const el = card.querySelector('.approval-countdown');
        if (!el) return;
        if (!deadline) { el.textContent = ''; return; }
        const remain = deadline * 1000 - Date.now();
        if (remain <= 0) {
            el.textContent = '已超时，等待结算…';
            el.classList.add('expired');
            card.querySelectorAll('button').forEach(b => { b.disabled = true; });
        } else if (!card.classList.contains('submitted')) {
            const sec = Math.ceil(remain / 1000);
            el.textContent = sec >= 60
                ? `剩余 ${Math.floor(sec / 60)} 分 ${sec % 60} 秒`
                : `剩余 ${sec} 秒`;
        }
    });
}, 1000);

function renderApprovalCard(container, input, toolUseId) {
    const q = (input.questions || [])[0] || {};
    const intent = q.intent || {};
    const card = document.createElement('div');
    card.className = 'approval-card ask-user-card';
    card.dataset.askPending = '1';
    card.dataset.deadline = String(intent.deadline_ts || 0);
    if (toolUseId) card.dataset.toolUseId = toolUseId;

    const title = document.createElement('div');
    title.className = 'ask-title';
    title.textContent = '🛡️ 命令审批';
    card.appendChild(title);

    const body = document.createElement('div');
    body.className = 'approval-body';
    body.innerHTML = renderMarkdownWithLinks(q.question || '');
    card.appendChild(body);

    const countdown = document.createElement('div');
    countdown.className = 'approval-countdown';
    card.appendChild(countdown);

    const actions = document.createElement('div');
    actions.className = 'approval-actions';

    const options = [
        {decision: 'approve', label: intent.approve || '允许本次会话', desc: '本会话内相同操作不再询问', cls: 'btn-primary'},
        {decision: 'remember', label: intent.always || '一直允许（此类操作）', desc: '写入工作区规则，重启后仍生效', cls: 'btn-secondary'},
        {decision: 'deny', label: intent.reject || '拒绝', desc: '仅拦截本次，Agent 可换方案', cls: 'btn-secondary'},
        {decision: 'abort', label: intent.abort || '终止任务', desc: '结束当前任务，不再自动继续', cls: 'btn-danger'},
    ].filter(opt => opt.decision !== 'remember' || intent.remember !== false);
    options.forEach(opt => {
        const btn = document.createElement('button');
        btn.className = `btn ${opt.cls} approval-btn`;
        btn.dataset.decision = opt.decision;

        const label = document.createElement('span');
        label.className = 'approval-btn-label';
        label.textContent = opt.label;
        btn.appendChild(label);

        const desc = document.createElement('span');
        desc.className = 'approval-btn-desc';
        desc.textContent = opt.desc;
        btn.appendChild(desc);

        btn.addEventListener('click', () => {
            if (card.classList.contains('submitted') || isSending) return;
            if (opt.decision === 'abort' && !confirm('终止当前任务？本回合将结束，不再自动继续。')) return;
            submitApprovalDecision(toolUseId, opt.decision, card);
        });
        actions.appendChild(btn);
    });
    card.appendChild(actions);

    container.appendChild(card);
    // 审批卡是阻塞交互：强制回底，别让用户错过待批操作。
    // 卡片若挂在已脱离文档的旧会话容器里（用户已切走），不能动当前视图。
    if (chatMessages.contains(container)) scrollChatIfNearBottom(true);
    if (typeof updateComposerButtons === 'function') updateComposerButtons();
}

async function submitApprovalDecision(toolUseId, decision, card) {
    if (!currentSession) return;
    const sessionId = currentSession.session_id;
    card.classList.add('submitted');
    card.querySelectorAll('button').forEach(b => { b.disabled = true; });
    const countdown = card.querySelector('.approval-countdown');
    if (countdown && !countdown.classList.contains('expired')) {
        countdown.textContent = '已提交…';
    }

    isSending = true;
    foregroundStreamSessionId = sessionId;
    sessionRunning = true;
    updateComposerButtons();

    try {
        const response = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}/ask-user/answers`,
            {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({tool_use_id: toolUseId, decision}),
            }
        );
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${await response.text()}`);
        }
        if (decision === 'abort') {
            countdown.textContent = '已终止任务';
            card.classList.add('aborted');
        }

        // 与提问卡提交共用同一套 SSE 消费方式
        const turnContainer = document.createElement('div');
        turnContainer.className = 'turn-stream';
        chatMessages.appendChild(turnContainer);
        activeTurnContainers.set(sessionId, turnContainer);
        const addTurnMessage = (role, content) =>
            addMessage(role, content, undefined, undefined, turnContainer);
        const scrollIfViewing = () => {
            if (currentSession && currentSession.session_id === sessionId) {
                scrollChatIfNearBottom();
            }
        };
        const renderer = createTurnStreamRenderer({
            sessionId, turnContainer, addTurnMessage, scrollIfViewing,
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, {stream: true});
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const dataStr = line.slice(6).trim();
                if (!dataStr || dataStr === '[DONE]') continue;
                try {
                    const event = JSON.parse(dataStr);
                    if (event.session_id && event.session_id !== sessionId) continue;
                    renderer.handleEvent(event);
                } catch (e) {
                    console.error('Failed to parse approval SSE event:', e, dataStr);
                }
            }
        }
        renderer.flush();
        activeTurnContainers.delete(sessionId);
        if (currentSession && currentSession.session_id === sessionId) {
            await loadSession(sessionId);
        }
    } catch (error) {
        console.error('Failed to submit approval decision:', error);
        countdown.textContent = '提交失败，请重试';
        card.classList.remove('submitted');
        card.querySelectorAll('button').forEach(b => { b.disabled = false; });
        if (currentSession && currentSession.session_id === sessionId) {
            addMessage('assistant', '提交审批失败: ' + error.message);
        }
    } finally {
        activeTurnContainers.delete(sessionId);
        if (currentSession && currentSession.session_id === sessionId) {
            isSending = false;
            isStopping = false;
            updateComposerButtons();
        }
        if (foregroundStreamSessionId === sessionId) {
            foregroundStreamSessionId = null;
        }
    }
}
