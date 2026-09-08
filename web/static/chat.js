// ── chat.js ── 聊天相关逻辑
// 依赖 app.js 全局变量: currentWorkspace, currentSession, isSending, pendingImages,
//   chatMessages, chatInput, sendBtn, _displayMsgIdx
// 依赖 app.js 函数: renderMarkdown, escapeHtml, showToast, loadSession, loadSessions, savePosition

let _displayMsgIdx = 0;  // 可显示消息的索引计数器（用于分享链接）

// ── 工具输出实时流式显示 ──
// 后端 /stream/{tool_use_id} 端点支持增量读取，_run_bash 边执行边写入
// 前端在收到 tool_use SSE 事件后启动轮询，收到 tool_result 后停止
// 工具输出显示为独立的消息气泡（类似思考块），执行完毕后自动消失
// 延迟5秒显示：快速执行的工具不会产生气泡闪烁

const _toolStreamTimers = {};  // { tool_use_id: { timer, pre, offset } }
const TOOL_STREAMING_DELAY = 5000; // 5秒后才显示实时输出

function startToolStreaming(toolUseId, toolName) {
    if (!currentWorkspace || !currentSession) return;

    // 先设置延迟定时器，5秒后才真正开始轮询和显示
    const delayTimer = setTimeout(() => {
        // 5秒后还没完成，开始创建气泡并轮询
        let offset = 0;

        // 创建独立的消息气泡（类似思考块）
        const div = document.createElement('div');
        div.className = 'message assistant tool-streaming-bubble';
        div.dataset.toolUseId = toolUseId;

        const contentDiv = document.createElement('div');
        contentDiv.className = 'message-content';

        const title = document.createElement('div');
        title.className = 'streaming-title';
        title.textContent = `⚡ ${toolName} 执行中...`;
        contentDiv.appendChild(title);

        const pre = document.createElement('pre');
        pre.className = 'tool-streaming-output';
        pre.textContent = '';
        contentDiv.appendChild(pre);

        div.appendChild(contentDiv);
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;

        // 更新 entry，标记气泡已创建
        const entry = _toolStreamTimers[toolUseId];
        if (entry) {
            entry.div = div;
            entry.pre = pre;
            entry.offset = offset;
            entry.timer = setInterval(async () => {
                try {
                    const url = `/api/workspaces/${currentWorkspace.uuid}/sessions/${currentSession.session_id}/stream/${toolUseId}?offset=${entry.offset}`;
                    const resp = await fetch(url);
                    if (!resp.ok) return;
                    const data = await resp.json();

                    if (data.content) {
                        if (entry.offset === 0) entry.pre.textContent = '';
                        entry.pre.textContent += data.content;
                        entry.offset = data.offset;
                        chatMessages.scrollTop = chatMessages.scrollHeight;
                    }
                } catch (e) {
                    // 轮询失败静默忽略，tool_result SSE 会兜底显示
                }
            }, 200);
        }
    }, TOOL_STREAMING_DELAY);

    // 先记录 entry，delayTimer 触发前 div/timer 为空
    _toolStreamTimers[toolUseId] = { delayTimer, timer: null, pre: null, offset: 0, div: null };
}

function stopToolStreaming(toolUseId) {
    const entry = _toolStreamTimers[toolUseId];
    if (entry) {
        // 清除延迟定时器（如果5秒内完成，气泡还没创建）
        if (entry.delayTimer) clearTimeout(entry.delayTimer);
        // 清除轮询定时器（如果已经启动）
        if (entry.timer) clearInterval(entry.timer);
        // 删除独立的输出气泡（如果已经创建）
        if (entry.div && entry.div.parentNode) {
            entry.div.remove();
        }
        delete _toolStreamTimers[toolUseId];
    }
}

// 清理所有流式定时器（会话切换时调用）
function clearAllToolStreaming() {
    for (const id of Object.keys(_toolStreamTimers)) {
        stopToolStreaming(id);
    }
}

// Render image preview thumbnails in the input area
function renderImagePreviews() {
    const area = document.getElementById('image-preview-area');
    if (!area) return;
    area.innerHTML = '';
    if (pendingImages.length === 0) {
        area.classList.remove('visible');
        return;
    }
    area.classList.add('visible');
    pendingImages.forEach((img, idx) => {
        const wrapper = document.createElement('div');
        wrapper.className = 'image-preview-thumb';

        const imgEl = document.createElement('img');
        imgEl.src = img.preview_url;
        imgEl.alt = '待发送图片';
        wrapper.appendChild(imgEl);

        const removeBtn = document.createElement('button');
        removeBtn.className = 'image-preview-remove';
        removeBtn.textContent = '×';
        removeBtn.title = '移除图片';
        removeBtn.onclick = () => {
            pendingImages.splice(idx, 1);
            renderImagePreviews();
        };
        wrapper.appendChild(removeBtn);
        area.appendChild(wrapper);
    });
}

// 辅助函数：提取文本内容
function extractTextContent(content) {
    if (typeof content === 'string') {
        return content;
    }
    if (Array.isArray(content)) {
        return content
            .filter(block => block.type === 'text')
            .map(block => block.text || '')
            .join('\n');
    }
    return '';
}

// Normalize message content into blocks
function normalizeContent(content) {
    const blocks = [];
    if (typeof content === 'string') {
        blocks.push({ kind: 'text', text: content });
    } else if (Array.isArray(content)) {
        content.forEach(block => {
            if (block.type === 'text') {
                blocks.push({ kind: 'text', text: block.text || '' });
            } else if (block.type === 'image') {
                const source = block.source || {};
                blocks.push({ kind: 'image', data: source.data || '', media_type: source.media_type || 'image/png' });
            } else if (block.type === 'thinking' || block.type === 'reasoning') {
                blocks.push({ kind: 'thinking', text: block.thinking || block.text || '' });
            } else if (block.type === 'tool_use' || block.type === 'tool_call') {
                // Handle both old format (input dict) and new format (arguments JSON string)
                let input = block.input;
                if (!input && block.arguments) {
                    try {
                        input = JSON.parse(block.arguments);
                    } catch (e) {
                        input = { _raw: block.arguments };
                    }
                }
                blocks.push({ kind: 'tool_call', name: block.name, input: input, id: block.id, _meta: block._meta || null });
            } else if (block.type === 'tool_result') {
                blocks.push({ kind: 'tool_result', content: block.content, is_error: block.is_error || false, _meta: block._meta || null, tool_use_id: block.tool_use_id || block.tool_call_id });
            }
        });
    }
    return blocks;
}

// Render messages
function renderMessages(messages) {
    chatMessages.innerHTML = '';
    _displayMsgIdx = 0;  // 重置消息索引

    if (!messages || messages.length === 0) {
        chatMessages.innerHTML = '<div class="welcome-message"><h2>开始新对话</h2><p>输入消息开始使用</p></div>';
        return;
    }

    messages.forEach((msg, idx) => {
        const role = msg.role;
        if (role === 'system') return;

        const content = msg.content;
        const blocks = normalizeContent(content);

        // For user messages, separate user content from tool results
        if (role === 'user') {
            const textParts = [];
            const imageParts = [];
            const toolResultBlocks = [];

            blocks.forEach(block => {
                if (block.kind === 'text' && block.text) textParts.push(block.text);
                else if (block.kind === 'image') imageParts.push(block);
                else if (block.kind === 'tool_result') toolResultBlocks.push(block);
            });

            // Only create user bubble if there's actual user content
            const combinedText = textParts.join('\n');
            if (combinedText || imageParts.length > 0) {
                const div = addMessage('user', combinedText);
                if (imageParts.length > 0) {
                    const contentDiv = div.querySelector('.message-content');
                    const imgContainer = document.createElement('div');
                    imgContainer.className = 'user-images';
                    imageParts.forEach(img => {
                        const imgEl = document.createElement('img');
                        imgEl.src = `data:${img.media_type};base64,${img.data}`;
                        imgEl.alt = '用户图片';
                        imgContainer.appendChild(imgEl);
                    });
                    contentDiv.insertBefore(imgContainer, contentDiv.firstChild);
                }
            }

            // Render tool results as separate tool bubbles
            toolResultBlocks.forEach(block => {
                // SubAgent 结果：渲染为可折叠的 SubAgent 卡片
                if (block._meta && block._meta.exec_id) {
                    const execId = block._meta.exec_id;
                    const isCompleted = block._meta.completed === true;
                    const msg = {
                        exec_id: execId,
                        task_summary: '',
                        status: isCompleted ? 'completed' : 'running',
                        iterations: block._meta.iterations || 0,
                        message_count: block._meta.message_count || 0,
                    };
                    // 尝试从 tool_use 块获取任务摘要（在前面的消息中）
                    // 简单处理：用 exec_id 加载详情
                    renderSubagentRef(msg, idx);
                    return;
                }
                // ask_user 等待中：跳过渲染
                if (block._meta && block._meta.completed === false) return;
                const text = typeof block.content === 'string' ? block.content : JSON.stringify(block.content, null, 2);
                const div = addMessage('assistant', '');
                div.classList.add('tool');
                if (block.is_error) {
                    div.classList.add('tool-error');
                } else {
                    div.classList.add('tool-result');
                }
                const contentDiv = div.querySelector('.message-content');
                const resultTitle = document.createElement('div');
                resultTitle.className = 'tool-title';
                resultTitle.textContent = '[工具结果]';
                contentDiv.appendChild(resultTitle);
                const pre = document.createElement('pre');
                pre.textContent = text.length > 2000 ? text.substring(0, 2000) + '\n... (内容过长已截断)' : text;
                contentDiv.appendChild(pre);
            });

            return;
        }

        blocks.forEach(block => {
            if (block.kind === 'text' && block.text) {
                addMessage(role, block.text);
            } else if (block.kind === 'image') {
                // Image blocks in non-user messages (shouldn't normally happen)
                // Render as an assistant message with the image
                const div = addMessage('assistant', '');
                const contentDiv = div.querySelector('.message-content');
                const imgEl = document.createElement('img');
                imgEl.src = `data:${block.media_type};base64,${block.data}`;
                imgEl.alt = '图片';
                contentDiv.appendChild(imgEl);
            } else if (block.kind === 'thinking' && block.text) {
                // Render thinking block
                const div = addMessage('assistant', '');
                div.classList.add('thinking');
                const contentDiv = div.querySelector('.message-content');
                const thinkTitle = document.createElement('div');
                thinkTitle.className = 'think-title';
                thinkTitle.textContent = '💭 思考过程';
                contentDiv.appendChild(thinkTitle);
                const thinkDiv = document.createElement('div');
                thinkDiv.className = 'think-content';
                thinkDiv.innerHTML = renderMarkdown(block.text);
                contentDiv.appendChild(thinkDiv);
            } else if (block.kind === 'tool_call') {
                const div = addMessage('assistant', '');
                div.classList.add('tool');
                const contentDiv = div.querySelector('.message-content');
                if (block.name === 'ask_user' && (!block._meta || !block._meta.answered)) {
                    // Render interactive question card only if unanswered
                    renderAskUserQuestions(contentDiv, block.input, block.id);
                } else {
                    const pre = document.createElement('pre');
                    pre.textContent = JSON.stringify(block.input, null, 2);
                    const toolTitle = document.createElement('div');
                    toolTitle.className = 'tool-title';
                    toolTitle.textContent = `[调用工具: ${block.name}]`;
                    contentDiv.appendChild(toolTitle);
                    contentDiv.appendChild(pre);
                }
            } else if (block.kind === 'tool_result') {
                // Skip placeholder tool_result for ask_user
                if (block._meta && block._meta.completed === false) return;
                const text = typeof block.content === 'string' ? block.content : JSON.stringify(block.content, null, 2);
                const div = addMessage('assistant', '');
                div.classList.add('tool');
                if (block.is_error) {
                    div.classList.add('tool-error');
                } else {
                    div.classList.add('tool-result');
                }
                const contentDiv = div.querySelector('.message-content');
                const resultTitle = document.createElement('div');
                resultTitle.className = 'tool-title';
                resultTitle.textContent = '[工具结果]';
                contentDiv.appendChild(resultTitle);
                const pre = document.createElement('pre');
                pre.textContent = text;
                contentDiv.appendChild(pre);
            }
        });
    });

    chatMessages.scrollTop = chatMessages.scrollHeight;

    // Render math formulas with MathJax
    if (window.MathJax && window.MathJax.typesetPromise) {
        MathJax.typesetPromise([chatMessages]).catch((err) => console.error('MathJax error:', err));
    }
}

// 渲染SubAgent 引用（可折叠卡片）
function renderSubagentRef(msg, idx) {
    const statusIcons = {
        'completed': '✅',
        'error': '❌',
        'failed': '❌',
        'timeout': '⏱️',
        'running': '🔄',
        'stopped': '⏹️'
    };
    const icon = statusIcons[msg.status] || '📋';

    const card = document.createElement('div');
    card.className = 'message assistant subagent-card';
    card.dataset.execId = msg.exec_id;

    const header = document.createElement('div');
    header.className = 'subagent-header';
    header.innerHTML = `
        <span class="sa-icon">${icon}</span>
        <span class="sa-title">SubAgent 执行</span>
        <span class="sa-task" title="${escapeHtml(msg.task_summary)}">${escapeHtml(msg.task_summary.substring(0, 60))}${msg.task_summary.length > 60 ? '...' : ''}</span>
        <span class="sa-meta">${msg.iterations || 0} 轮 · ${msg.message_count || 0} 条消息</span>
        <span class="sa-toggle">▶</span>
    `;

    const detail = document.createElement('div');
    detail.className = 'subagent-detail';
    detail.style.display = 'none';

    card.appendChild(header);
    card.appendChild(detail);
    chatMessages.appendChild(card);

    // 点击展开/折叠
    header.addEventListener('click', () => {
        if (detail.style.display === 'none') {
            detail.style.display = 'block';
            header.querySelector('.sa-toggle').textContent = '▼';
            // 运行中时每次都重新加载，完成后缓存
            const shouldReload = !detail.dataset.loaded || msg.status === 'running';
            if (shouldReload) {
                loadExecutionDetail(msg.exec_id, detail, header, msg);
            }
        } else {
            detail.style.display = 'none';
            header.querySelector('.sa-toggle').textContent = '▶';
            // 折叠时停止定时器并重置状态
            if (msg._refreshTimer) {
                clearInterval(msg._refreshTimer);
                msg._refreshTimer = null;
            }
            // 重置加载状态，再次展开时当作全新加载
            detail.dataset.loaded = 'false';
            detail.dataset.renderedCount = '0';
        }
    });
}

// 渲染任务清单（Todo List）
// TodoWrite UI 渲染逻辑
// 显示在聊天区域顶部，实时更新
function renderTodoList(todos) {
    if (!todos || !Array.isArray(todos) || todos.length === 0) {
        const existing = document.getElementById('todo-list');
        if (existing) existing.remove();
        return;
    }

    // 计算统计
    const total = todos.length;
    const completed = todos.filter(t => t.status === 'completed').length;
    const inProgress = todos.filter(t => t.status === 'in_progress').length;

    // 查找或创建 todo 容器
    let container = document.getElementById('todo-list');
    if (!container) {
        container = document.createElement('div');
        container.id = 'todo-list';
        container.className = 'todo-list-container';
        // 插入到聊天区域顶部
        chatMessages.insertBefore(container, chatMessages.firstChild);
    }

    // 构建 HTML
    const progress = total > 0 ? Math.round((completed / total) * 100) : 0;

    let html = `
        <div class="todo-header">
            <span class="todo-title">📋 任务清单</span>
            <span class="todo-progress">${completed}/${total} 完成</span>
        </div>
        <div class="todo-progress-bar">
            <div class="todo-progress-fill" style="width: ${progress}%"></div>
        </div>
        <ul class="todo-items">
    `;

    todos.forEach(todo => {
        const statusClass = `todo-${todo.status.replace('_', '-')}`;
        const statusIcon = todo.status === 'completed' ? '✓' :
                          todo.status === 'in_progress' ? '◉' : '○';
        const displayText = todo.content;
        html += `
            <li class="todo-item ${statusClass}">
                <span class="todo-status">${statusIcon}</span>
                <span class="todo-text">${escapeHtml(displayText)}</span>
            </li>
        `;
    });

    html += '</ul>';
    container.innerHTML = html;
}

// SubAgent 开始执行时立即渲染占位卡片（SSE 推送，无需等待完成）
function renderSubagentStart(execId, taskSummary) {
    const card = document.createElement('div');
    card.className = 'message assistant subagent-card';
    card.dataset.execId = execId;

    const header = document.createElement('div');
    header.className = 'subagent-header';
    header.innerHTML = `
        <span class="sa-icon">🔄</span>
        <span class="sa-title">SubAgent 执行中</span>
        <span class="sa-task" title="${escapeHtml(taskSummary)}">${escapeHtml(taskSummary.substring(0, 60))}${taskSummary.length > 60 ? '...' : ''}</span>
        <span class="sa-meta">0 轮 · 0 条消息</span>
        <span class="sa-toggle">▶</span>
    `;

    const detail = document.createElement('div');
    detail.className = 'subagent-detail';
    detail.style.display = 'none';

    card.appendChild(header);
    card.appendChild(detail);
    chatMessages.appendChild(card);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    // msg 对象用于 loadExecutionDetail 更新卡片状态
    const msg = { exec_id: execId, task_summary: taskSummary, status: 'running' };

    // 点击展开/折叠（运行中时每次都重新加载）
    header.addEventListener('click', () => {
        if (detail.style.display === 'none') {
            detail.style.display = 'block';
            header.querySelector('.sa-toggle').textContent = '▼';
            loadExecutionDetail(execId, detail, header, msg);
        } else {
            detail.style.display = 'none';
            header.querySelector('.sa-toggle').textContent = '▶';
            // 折叠时停止定时器并重置状态
            if (msg._refreshTimer) {
                clearInterval(msg._refreshTimer);
                msg._refreshTimer = null;
            }
            // 重置加载状态，再次展开时当作全新加载
            detail.dataset.loaded = 'false';
            detail.dataset.renderedCount = '0';
        }
    });
}

// SubAgent 完成时更新卡片状态
function renderSubagentComplete(execId) {
    const card = chatMessages.querySelector(`.subagent-card[data-exec-id="${execId}"]`);
    if (!card) return;
    const header = card.querySelector('.subagent-header');
    if (!header) return;
    // 更新图标和标题
    header.querySelector('.sa-icon').textContent = '✅';
    header.querySelector('.sa-title').textContent = 'SubAgent 已完成';
    // 触发一次展开加载以获取最新数据
    const detail = card.querySelector('.subagent-detail');
    if (detail && detail.style.display === 'block') {
        // 已展开，重新加载内容
        const msg = { exec_id: execId, status: 'completed' };
        loadExecutionDetail(execId, detail, header, msg);
    }
}

// 渲染 AskUser 问题卡片
function renderAskUserQuestions(container, input, toolUseId) {
    const questions = input.questions || [];
    if (!questions.length) return;

    const card = document.createElement('div');
    card.className = 'ask-user-card';
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

        submitBtn.textContent = '提交中...';
        submitBtn.disabled = true;

        try {
            // 调用 answer-ask-user API
            const response = await fetch(
                `/api/workspaces/${currentWorkspace.uuid}/sessions/${currentSession.session_id}/answer-ask-user`,
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
            card.querySelectorAll('.ask-option').forEach(el => el.style.pointerEvents = 'none');
            card.querySelectorAll('input').forEach(el => el.disabled = true);

            // 处理 SSE 流
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            let assistantDiv = null;
            let assistantContent = '';
            let thinkDiv = null;
            let thinkContent = '';

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

                        if (event.type === 'thinking') {
                            if (!thinkDiv) {
                                const div = addMessage('assistant', '');
                                div.classList.add('thinking');
                                const contentDiv = div.querySelector('.message-content');
                                const thinkTitle = document.createElement('div');
                                thinkTitle.className = 'think-title';
                                thinkTitle.textContent = '💭 思考中...';
                                contentDiv.appendChild(thinkTitle);
                                thinkDiv = document.createElement('div');
                                thinkDiv.className = 'think-content';
                                contentDiv.appendChild(thinkDiv);
                                thinkContent = '';
                            }
                            thinkContent += event.content;
                            thinkDiv.innerHTML = renderMarkdown(thinkContent);
                            chatMessages.scrollTop = chatMessages.scrollHeight;
                        } else if (event.type === 'text') {
                            if (thinkDiv) {
                                thinkDiv = null;
                                thinkContent = '';
                            }
                            assistantContent += event.content;
                            if (!assistantDiv) {
                                assistantDiv = addMessage('assistant', assistantContent);
                            } else {
                                assistantDiv.dataset.rawContent = assistantContent;
                                assistantDiv.querySelector('.message-content').innerHTML = renderMarkdown(assistantContent);
                                if (window.MathJax && window.MathJax.typesetPromise) {
                                    MathJax.typesetPromise([assistantDiv]).catch((err) => {});
                                }
                            }
                        } else if (event.type === 'tool_use') {
                            if (thinkDiv) {
                                thinkDiv = null;
                                thinkContent = '';
                            }
                            const div = addMessage('assistant', '');
                            div.classList.add('tool');
                            const contentDiv = div.querySelector('.message-content');
                            if (event.tool === 'ask_user') {
                                renderAskUserQuestions(contentDiv, event.input, event.tool_use_id);
                            } else {
                                const toolTitle = document.createElement('div');
                                toolTitle.className = 'tool-title';
                                toolTitle.textContent = `[调用工具: ${event.tool}]`;
                                contentDiv.appendChild(toolTitle);
                                const pre = document.createElement('pre');
                                pre.textContent = JSON.stringify(event.input, null, 2);
                                contentDiv.appendChild(pre);
                                // 对 bash/python 工具启动实时输出流式显示（独立气泡）
                                if (event.tool === 'bash' || event.tool === 'python') {
                                    startToolStreaming(event.tool_use_id, event.tool);
                                }
                            }
                            assistantDiv = null;
                            assistantContent = '';
                        } else if (event.type === 'tool_result') {
                            // 停止该工具调用的实时输出轮询
                            stopToolStreaming(event.tool_use_id);
                            if (event.tool === 'ask_user') {
                                const askCard = document.querySelector(`.ask-user-card[data-tool-use-id="${event.tool_use_id}"]`);
                                if (askCard) {
                                    const btn = askCard.querySelector('.ask-submit');
                                    if (btn && !btn.classList.contains('ask-submitted')) {
                                        btn.textContent = '✓ 已提交';
                                        btn.classList.add('ask-submitted');
                                        btn.disabled = true;
                                    }
                                    askCard.querySelectorAll('.ask-option').forEach(el => el.style.pointerEvents = 'none');
                                    askCard.querySelectorAll('input').forEach(el => el.disabled = true);
                                }
                                continue;
                            }
                            if (thinkDiv) {
                                thinkDiv = null;
                                thinkContent = '';
                            }
                            const div = addMessage('assistant', '');
                            div.classList.add('tool');
                            if (event.is_error) {
                                div.classList.add('tool-error');
                            } else {
                                div.classList.add('tool-result');
                            }
                            const contentDiv = div.querySelector('.message-content');
                            const resultTitle = document.createElement('div');
                            resultTitle.className = 'tool-title';
                            resultTitle.textContent = '[工具结果]';
                            contentDiv.appendChild(resultTitle);
                            const text = typeof event.content === 'string' ? event.content : JSON.stringify(event.content, null, 2);
                            const pre = document.createElement('pre');
                            pre.textContent = text;
                            contentDiv.appendChild(pre);
                            assistantDiv = null;
                            assistantContent = '';
                        } else if (event.type === 'subagent_start') {
                            renderSubagentStart(event.exec_id, event.task_summary);
                        } else if (event.type === 'subagent_complete') {
                            renderSubagentComplete(event.exec_id);
                        } else if (event.type === 'todo_update') {
                            renderTodoList(event.todos);
                        } else if (event.type === 'retry_clear') {
                            if (assistantDiv) {
                                assistantDiv.remove();
                                assistantDiv = null;
                            }
                            assistantContent = '';
                            if (thinkDiv) {
                                thinkDiv = null;
                                thinkContent = '';
                            }
                        } else if (event.type === 'error') {
                            addMessage('assistant', `错误: ${escapeHtml(event.content)}`);
                        } else if (event.type === 'done') {
                            // Stream complete
                        }
                    } catch (e) {
                        console.error('Failed to parse SSE event:', e, dataStr);
                    }
                }
            }

            // Refresh session to get persisted state
            await loadSession(currentSession.session_id);

        } catch (error) {
            console.error('Failed to submit answer:', error);
            submitBtn.textContent = '提交失败';
            submitBtn.disabled = false;
            addMessage('assistant', '提交答案失败: ' + error.message);
        }
    });

    card.appendChild(submitBtn);
    container.appendChild(card);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// 加载执行详情（headerEl 和 msg 用于实时更新卡片状态）
async function loadExecutionDetail(execId, container, headerEl, msg) {
    // 如果正在刷新，先清除旧的定时器
    if (msg._refreshTimer) {
        clearInterval(msg._refreshTimer);
        msg._refreshTimer = null;
    }

    container.innerHTML = '<div class="sa-loading">加载中...</div>';

    // 实际加载函数
    const doLoad = async () => {
        try {
            const workspaceUuid = currentWorkspace?.uuid;
            const sessionId = currentSession.session_id;
            const url = `/api/workspaces/${workspaceUuid}/sessions/${sessionId}/executions/${execId}`;
            const resp = await fetch(url);

            if (!resp.ok) {
                container.innerHTML = '<div class="sa-error">加载失败</div>';
                return false;
            }

            const data = await resp.json();
            const isFirstLoad = container.dataset.loaded !== 'true';
            container.dataset.loaded = 'true';

            // 更新卡片 header 中的状态信息
            if (headerEl && msg) {
                // 更新 status（从运行中变为已完成等）
                if (data.metadata && data.metadata.status) {
                    msg.status = data.metadata.status;
                    const statusIcons = {
                        'completed': '✅',
                        'error': '❌',
                        'failed': '❌',
                        'timeout': '⏱️',
                        'running': '🔄',
                        'stopped': '⏹️'
                    };
                    headerEl.querySelector('.sa-icon').textContent = statusIcons[msg.status] || '📋';
                }
                // 更新 iterations 和 message_count
                if (data.metadata) {
                    const iters = data.metadata.iterations || 0;
                    const msgs = data.messages ? data.messages.length : 0;
                    const currentTool = data.metadata.current_tool || '';
                    const toolSuffix = currentTool ? ` · 正在: ${currentTool}` : '';
                    headerEl.querySelector('.sa-meta').textContent = `${iters} 轮 · ${msgs} 条消息${toolSuffix}`;
                }
            }

            // 首次加载时清空容器
            if (isFirstLoad) {
                container.innerHTML = '';
                // summary 内容已在最后一条 assistant 消息中显示，不重复渲染
            }

            // 渲染消息（增量追加）
            const renderedCount = parseInt(container.dataset.renderedCount || '0');
            const messages = data.messages || [];

            if (messages.length > renderedCount) {
                // 获取或创建消息容器
                let msgsDiv = container.querySelector('.sa-messages');
                if (!msgsDiv) {
                    msgsDiv = document.createElement('div');
                    msgsDiv.className = 'sa-messages';
                    container.appendChild(msgsDiv);
                }

                // 只渲染新消息
                const newMessages = messages.slice(renderedCount);
                newMessages.forEach(message => {
                    const blocks = normalizeContent(message.content);
                    blocks.forEach(block => {
                        if (block.kind === 'text' && block.text) {
                            const div = document.createElement('div');
                            div.className = `sa-msg ${message.role}`;
                            div.innerHTML = renderMarkdown(block.text);
                            msgsDiv.appendChild(div);
                        } else if (block.kind === 'tool_call') {
                            const div = document.createElement('div');
                            div.className = 'sa-msg tool-use';
                            div.innerHTML = `<strong>[工具调用: ${block.name}]</strong><pre>${escapeHtml(JSON.stringify(block.input, null, 2))}</pre>`;
                            msgsDiv.appendChild(div);
                        } else if (block.kind === 'tool_result') {
                            const text = typeof block.content === 'string' ? block.content : JSON.stringify(block.content, null, 2);
                            const div = document.createElement('div');
                            div.className = 'sa-msg tool-result';
                            div.innerHTML = `<strong>[工具结果]</strong><pre>${escapeHtml(text.substring(0, 500))}${text.length > 500 ? '...' : ''}</pre>`;
                            msgsDiv.appendChild(div);
                        }
                    });
                });

                // 更新已渲染计数
                container.dataset.renderedCount = messages.length;

                // 自动滚动：只在用户已经在底部附近时才滚动
                const chatMessages = document.getElementById('chat-messages');
                if (chatMessages) {
                    const isNearBottom = chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight < 100;
                    if (isNearBottom) {
                        chatMessages.scrollTop = chatMessages.scrollHeight;
                    }
                }
            }

            // 返回当前状态
            return msg.status === 'running';
        } catch (err) {
            console.error('Failed to load execution detail:', err);
            if (container.dataset.loaded !== 'true') {
                container.innerHTML = '<div class="sa-error">加载失败: ' + escapeHtml(err.message) + '</div>';
            }
            return false;
        }
    };

    // 首次加载
    const isRunning = await doLoad();

    // 如果还在运行，设置定时刷新（每 3 秒）
    if (isRunning) {
        msg._refreshTimer = setInterval(async () => {
            const stillRunning = await doLoad();
            if (!stillRunning) {
                // 已完成，停止刷新
                clearInterval(msg._refreshTimer);
                msg._refreshTimer = null;
            }
        }, 3000);
    }
}

// Stop agent
async function stopAgent() {
    if (!currentWorkspace || !currentSession) return;

    // Disable button immediately after clicking, keep "停止中..." until the SSE stream actually closes
    // (agent truly stopped). sendMessage()'s finally block restores the button.
    sendBtn.disabled = true;
    sendBtn.textContent = '停止中...';

    let success = false;
    try {
        const response = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${currentSession.session_id}/stop`,
            { method: 'POST' }
        );

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const result = await response.json();
        if (result.success) {
            console.log('Agent stop signal sent');
            success = true;
        } else {
            console.warn('Stop failed:', result.message);
        }
    } catch (error) {
        console.error('Failed to stop agent:', error);
    }

    if (!success) {
        // 停止信号未送达（agent 可能已自行停止或网络异常）：恢复为可点击的"停止"按钮，
        // 若 agent 确实已停止，SSE 流马上关闭，finally 会恢复为"发送"
        sendBtn.disabled = false;
        sendBtn.textContent = '停止';
    } else {
        // 保持"停止中..."直到 SSE 流关闭。兜底超时：若流长时间未关闭
        // （如 LLM 重试卡住），恢复"停止"按钮允许重新点击
        clearTimeout(window._stopPendingTimer);
        window._stopPendingTimer = setTimeout(() => {
            if (isSending && sendBtn.textContent === '停止中...') {
                sendBtn.disabled = false;
                sendBtn.textContent = '停止';
            }
        }, 60000);
    }
    // 不在这里恢复按钮状态：isSending 保持 true，阻止停止过程中发送新消息，
    // 直到 SSE 流关闭（agent 真正停止）后由 sendMessage() 的 finally 恢复

    chatInput.disabled = false;

    // 禁用所有 pending 的 ask_user 问题卡片
    document.querySelectorAll('.ask-user-card').forEach(card => {
        const submitBtn = card.querySelector('.ask-submit');
        if (submitBtn && !submitBtn.classList.contains('ask-submitted')) {
            submitBtn.textContent = '已取消';
            submitBtn.disabled = true;
        }
        card.querySelectorAll('.ask-option').forEach(el => el.style.pointerEvents = 'none');
        card.querySelectorAll('input').forEach(el => el.disabled = true);
    });
}

// Send message
async function sendMessage() {
    const message = chatInput.value.trim();
    const hasImages = pendingImages.length > 0;
    if (!message && !hasImages) return;

    if (!currentWorkspace || !currentSession) {
        alert('请先选择工作区并创建会话');
        return;
    }

    // Block sending if agent is running
    if (isSending) {
        return;
    }

    console.log('Sending message to session:', currentSession.session_id);

    // Capture images before clearing
    const imagesToSend = hasImages ? pendingImages.map(img => ({
        data: img.data,
        media_type: img.media_type,
    })) : null;

    // Clear input and change button to stop
    chatInput.value = '';
    pendingImages = [];
    renderImagePreviews();
    isSending = true;
    sendBtn.textContent = '停止';
    sendBtn.classList.remove('btn-primary');
    sendBtn.classList.add('btn-danger');

    // Add user message to UI (with images if any)
    const userDiv = addMessage('user', message);
    if (imagesToSend) {
        const contentDiv = userDiv.querySelector('.message-content');
        const imgContainer = document.createElement('div');
        imgContainer.className = 'user-images';
        imagesToSend.forEach(img => {
            const imgEl = document.createElement('img');
            imgEl.src = `data:${img.media_type};base64,${img.data}`;
            imgEl.alt = '用户图片';
            imgContainer.appendChild(imgEl);
        });
        contentDiv.insertBefore(imgContainer, contentDiv.firstChild);
    }

    // Create placeholder for assistant response
    let assistantDiv = null;
    let assistantContent = '';
    let thinkDiv = null;
    let thinkContent = '';

    // Close the current think block (shared by text, tool_use, tool_result handlers)
    function finalizeThinkBlock() {
        if (thinkDiv) {
            const thinkTitle = thinkDiv.parentElement.querySelector('.think-title');
            if (thinkTitle) {
                thinkTitle.textContent = '💭 思考完成';
            }
            thinkDiv = null;
            thinkContent = '';
        }
    }

    try {
        const requestBody = { content: message };
        if (imagesToSend) {
            requestBody.images = imagesToSend;
        }
        const response = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${currentSession.session_id}/messages`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(requestBody)
            }
        );

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${await response.text()}`);
        }

        // Handle streaming response
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });

            // Process complete lines
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';  // Keep incomplete last line

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;

                const dataStr = line.slice(6).trim();
                if (!dataStr || dataStr === '[DONE]') continue;

                try {
                    const event = JSON.parse(dataStr);

                    if (event.type === 'thinking') {
                        // 创建或更新 think 块
                        if (!thinkDiv) {
                            const div = addMessage('assistant', '');
                            div.classList.add('thinking');
                            const contentDiv = div.querySelector('.message-content');
                            const thinkTitle = document.createElement('div');
                            thinkTitle.className = 'think-title';
                            thinkTitle.textContent = '💭 思考中...';
                            contentDiv.appendChild(thinkTitle);
                            thinkDiv = document.createElement('div');
                            thinkDiv.className = 'think-content';
                            contentDiv.appendChild(thinkDiv);
                            thinkContent = '';
                        }
                        thinkContent += event.content;
                        thinkDiv.innerHTML = renderMarkdown(thinkContent);
                        chatMessages.scrollTop = chatMessages.scrollHeight;
                    } else if (event.type === 'text') {
                        // 如果之前有 think 块，标记完成
                        finalizeThinkBlock();
                        assistantContent += event.content;
                        if (!assistantDiv) {
                            assistantDiv = addMessage('assistant', assistantContent);
                        } else {
                            assistantDiv.dataset.rawContent = assistantContent;
                            assistantDiv.querySelector('.message-content').innerHTML = renderMarkdown(assistantContent);
                            // Render math formulas with MathJax
                            if (window.MathJax && window.MathJax.typesetPromise) {
                                MathJax.typesetPromise([assistantDiv]).catch((err) => {});
                            }
                        }
                    } else if (event.type === 'tool_use') {
                        // 如果之前有 think 块，标记完成
                        finalizeThinkBlock();
                        // 创建新的消息气泡
                        const div = addMessage('assistant', '');
                        div.classList.add('tool');
                        const contentDiv = div.querySelector('.message-content');

                        if (event.tool === 'ask_user') {
                            // 渲染交互式问题卡片
                            renderAskUserQuestions(contentDiv, event.input, event.tool_use_id);
                        } else {
                            const toolTitle = document.createElement('div');
                            toolTitle.className = 'tool-title';
                            toolTitle.textContent = `[调用工具: ${event.tool}]`;
                            contentDiv.appendChild(toolTitle);
                            const pre = document.createElement('pre');
                            pre.textContent = JSON.stringify(event.input, null, 2);
                            contentDiv.appendChild(pre);
                            // 对 bash/python 工具启动实时输出流式显示（独立气泡）
                            if (event.tool === 'bash' || event.tool === 'python') {
                                startToolStreaming(event.tool_use_id, event.tool);
                            }
                        }

                        // 重置 assistantDiv 用于后续文本
                        assistantDiv = null;
                        assistantContent = '';
                    } else if (event.type === 'tool_result') {
                        // 停止该工具调用的实时输出轮询
                        stopToolStreaming(event.tool_use_id);
                        // ask_user 的 tool_result 事件：禁用问题卡片
                        if (event.tool === 'ask_user') {
                            const card = document.querySelector(`.ask-user-card[data-tool-use-id="${event.tool_use_id}"]`);
                            if (card) {
                                const submitBtn = card.querySelector('.ask-submit');
                                if (submitBtn && !submitBtn.classList.contains('ask-submitted')) {
                                    submitBtn.textContent = '✓ 已提交';
                                    submitBtn.classList.add('ask-submitted');
                                    submitBtn.disabled = true;
                                }
                                card.querySelectorAll('.ask-option').forEach(el => el.style.pointerEvents = 'none');
                                card.querySelectorAll('input').forEach(el => el.disabled = true);
                            }
                            continue;
                        }
                        // 如果之前有 think 块，标记完成
                        finalizeThinkBlock();
                        // 创建新的消息气泡
                        const div = addMessage('assistant', '');
                        div.classList.add('tool');
                        if (event.is_error) {
                            div.classList.add('tool-error');
                        } else {
                            div.classList.add('tool-result');
                        }
                        const contentDiv = div.querySelector('.message-content');
                        const resultTitle = document.createElement('div');
                        resultTitle.className = 'tool-title';
                        resultTitle.textContent = '[工具结果]';
                        contentDiv.appendChild(resultTitle);
                        const text = typeof event.content === 'string' ? event.content : JSON.stringify(event.content, null, 2);
                        const pre = document.createElement('pre');
                        pre.textContent = text;
                        contentDiv.appendChild(pre);
                        // 重置 assistantDiv 用于后续文本
                        assistantDiv = null;
                        assistantContent = '';
                    } else if (event.type === 'subagent_start') {
                        // SubAgent 开始执行，立即渲染占位卡片
                        renderSubagentStart(event.exec_id, event.task_summary);
                    } else if (event.type === 'subagent_complete') {
                        // SubAgent 完成，更新卡片状态
                        renderSubagentComplete(event.exec_id);
                    } else if (event.type === 'todo_update') {
                        // Todo 列表更新，渲染任务清单
                        renderTodoList(event.todos);
                    } else if (event.type === 'retry_clear') {
                        // 413 重试：清除已流式输出的文本，防止用户看到重复内容
                        if (assistantDiv) {
                            assistantDiv.remove();
                            assistantDiv = null;
                        }
                        assistantContent = '';
                        finalizeThinkBlock();
                        thinkDiv = null;
                        thinkContent = '';
                    } else if (event.type === 'error') {
                        addMessage('assistant', `错误: ${escapeHtml(event.content)}`);
                    } else if (event.type === 'done') {
                        // Stream complete
                    }
                } catch (e) {
                    console.error('Failed to parse SSE event:', e, dataStr);
                }
            }
        }

        // Refresh session to get persisted state
        await loadSession(currentSession.session_id);

    } catch (error) {
        console.error('Failed to send message:', error);
        addMessage('assistant', '发送消息失败: ' + error.message);
    } finally {
        clearTimeout(window._stopPendingTimer);
        isSending = false;
        sendBtn.disabled = false;
        sendBtn.textContent = '发送';
        sendBtn.classList.remove('btn-danger');
        sendBtn.classList.add('btn-primary');
        chatMessages.scrollTop = chatMessages.scrollHeight;
        // 刷新侧边栏，更新会话标题（preview = 最后一条用户消息）
        await loadSessions();
    }
}

// ── Quote (引用) helpers ──

/**
 * Create a quote button for a message element.
 * Clicking it inserts the message text (truncated to 500 chars) into the
 * input box as a quoted block, e.g.:
 *   [引用AI的消息]
 *   > ...
 * [/引用]
 */
function doQuote(messageDiv) {
    const contentDiv = messageDiv.querySelector('.message-content');
    const raw = contentDiv ? contentDiv.textContent.trim() : '';
    if (!raw) return;

    const text = raw.length > 500 ? raw.substring(0, 500) + '...' : raw;
    const quoteText = text.split('\n').map(line => '> ' + line).join('\n') + '\n\n';

    const start = chatInput.selectionStart ?? chatInput.value.length;
    const end = chatInput.selectionEnd ?? chatInput.value.length;
    chatInput.value = chatInput.value.substring(0, start) + quoteText + chatInput.value.substring(end);
    chatInput.focus();
    const newPos = start + quoteText.length;
    chatInput.setSelectionRange(newPos, newPos);
}

function doCopy(messageDiv) {
    const text = messageDiv.dataset.rawContent || messageDiv.querySelector('.message-content').textContent;
    navigator.clipboard.writeText(text).then(() => {
        showToast('已复制');
    });
}

function doShare(msgIdx) {
    if (!currentWorkspace || !currentSession) return;
    const url = `${window.location.origin}/s/${currentWorkspace.uuid}/${currentSession.session_id}/${msgIdx}`;
    window.open(url, '_blank');
}

function toggleMessageMenu(messageDiv, anchor) {
    // 关闭其他菜单
    document.querySelectorAll('.msg-menu.show').forEach(m => m.remove());

    const menu = document.createElement('div');
    menu.className = 'msg-menu show';

    const items = [];

    // 引用
    items.push({ label: '引用', action: () => doQuote(messageDiv) });

    // 复制（仅助手消息）
    if (messageDiv.classList.contains('assistant')) {
        items.push({ label: '复制', action: () => doCopy(messageDiv) });
    }

    // 分享（有索引时）
    const msgIdx = messageDiv.dataset.msgIdx;
    if (msgIdx !== undefined) {
        items.push({ label: '分享', action: () => doShare(msgIdx) });
    }

    items.forEach(({ label, action }) => {
        const div = document.createElement('div');
        div.className = 'msg-menu-item';
        div.textContent = label;
        div.addEventListener('click', (e) => {
            e.stopPropagation();
            action();
            menu.remove();
        });
        menu.appendChild(div);
    });

    anchor.appendChild(menu);

    // 点击其他地方关闭菜单
    setTimeout(() => {
        document.addEventListener('click', function closeMenu() {
            menu.remove();
            document.removeEventListener('click', closeMenu);
        });
    }, 0);
}

// Add message to UI
function addMessage(role, content) {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${role}`;

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    if (content) {
        contentDiv.innerHTML = renderMarkdown(content);

        // 检查是否包含图片，如果包含则添加特殊样式
        if (contentDiv.querySelector('img')) {
            messageDiv.classList.add('has-image');
        }

        // 分配可显示消息索引
        messageDiv.dataset.msgIdx = _displayMsgIdx;
        _displayMsgIdx++;
    }

    messageDiv.appendChild(contentDiv);

    // 所有消息（有内容时）添加菜单按钮
    if (content) {
        messageDiv.dataset.rawContent = content;

        const triggerBtn = document.createElement('button');
        triggerBtn.className = 'msg-menu-trigger';
        triggerBtn.textContent = '⋯';
        triggerBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleMessageMenu(messageDiv, triggerBtn);
        });

        const actionsDiv = document.createElement('div');
        actionsDiv.className = 'message-actions';
        actionsDiv.appendChild(triggerBtn);
        messageDiv.appendChild(actionsDiv);
    }

    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    return messageDiv;
}
