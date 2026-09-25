// ── chat.js ── 聊天相关逻辑
// 依赖 app.js 全局变量: currentWorkspace, currentSession, isSending, pendingImages,
//   chatMessages, chatInput, sendBtn
// 依赖 app.js 函数: renderMarkdown, escapeHtml, showToast, loadSession, loadSessions, savePosition

// ── 工具输出实时流式显示 ──
// 后端 /stream/{tool_use_id} 端点支持增量读取，_run_bash 边执行边写入；
// 工具实时输出由全局事件流（sse-client.js）推送 tool_output 事件驱动，不再轮询。
// 工具输出显示为独立的消息气泡（类似思考块），执行完毕后自动消失
// 延迟5秒显示：快速执行的工具不会产生气泡闪烁
// （有实时输出事件时 sse-client 会跳过延迟立即建气泡）

const _toolStreamTimers = {};  // { tool_use_id: { delayTimer, pre, offset, div } }
const TOOL_STREAMING_DELAY = 5000; // 5秒后才显示实时输出

function startToolStreaming(toolUseId, toolName) {
    if (!currentWorkspace || !currentSession) return;
    if (_toolStreamTimers[toolUseId]) return;

    // 先记录 entry；5 秒后若无 tool_output 事件，由延迟定时器创建气泡
    _toolStreamTimers[toolUseId] = { delayTimer: null, timer: null, pre: null, offset: 0, div: null };
    _toolStreamTimers[toolUseId].delayTimer = setTimeout(() => {
        const entry = _toolStreamTimers[toolUseId];
        if (!entry) return;
        entry.delayTimer = null;
        ensureMasterToolBubble(toolUseId, toolName);
    }, TOOL_STREAMING_DELAY);
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
    // 一并清理 worker 子代理卡片状态（sse-client.js，修复切会话泄漏）
    if (typeof clearAllAgentStreaming === 'function') {
        clearAllAgentStreaming();
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

    if (!messages || messages.length === 0) {
        chatMessages.innerHTML = '<div class="welcome-message"><h2>开始新对话</h2><p>输入消息开始使用</p></div>';
        return;
    }

    messages.forEach((msg, idx) => {
        const role = msg.role;
        if (role === 'system') return;

        const msgId = msg._meta?.id;  // 消息唯一 ID，用于分享链接
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
                const div = addMessage('user', combinedText, msgId, msg._meta?.created_at);
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
                // 子代理结果：渲染为可折叠的卡片
                if (block._meta && block._meta.exec_id) {
                    const execId = block._meta.exec_id;
                    const isCompleted = block._meta.completed === true;
                    const saMsg = {
                        exec_id: execId,
                        task_summary: block._meta.task_summary || '',
                        status: isCompleted ? 'completed' : 'running',
                        iterations: block._meta.iterations || 0,
                        message_count: block._meta.message_count || 0,
                        tool_call_count: block._meta.tool_call_count || 0,
                    };
                    // 尝试从 tool_use 块获取任务摘要（在前面的消息中）
                    // 简单处理：用 exec_id 加载详情
                    renderAgentRef(saMsg, idx, msgId);
                    return;
                }
                // ask_user 等待中：跳过渲染
                if (block._meta && block._meta.completed === false) return;
                const text = typeof block.content === 'string' ? block.content : JSON.stringify(block.content, null, 2);
                const div = addMessage('assistant', '', msgId, msg._meta?.created_at);
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
                addMessage(role, block.text, msgId, msg._meta?.created_at);
            } else if (block.kind === 'image') {
                // Image blocks in non-user messages (shouldn't normally happen)
                // Render as an assistant message with the image
                const div = addMessage('assistant', '', msgId, msg._meta?.created_at);
                const contentDiv = div.querySelector('.message-content');
                const imgEl = document.createElement('img');
                imgEl.src = `data:${block.media_type};base64,${block.data}`;
                imgEl.alt = '图片';
                contentDiv.appendChild(imgEl);
            } else if (block.kind === 'thinking' && block.text) {
                // Render thinking block
                const div = addMessage('assistant', '', msgId, msg._meta?.created_at);
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
                const div = addMessage('assistant', '', msgId, msg._meta?.created_at);
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
                const div = addMessage('assistant', '', msgId, msg._meta?.created_at);
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

// 渲染子代理引用（可折叠卡片）——委托 sse-client.js 卡片状态机
function renderAgentRef(msg, idx, msgId) {
    agentCardForMessage(msg, msgId);
}

// ── 历史消息分页加载 ──
// 将历史消息插入到聊天区域顶部，保持滚动位置
function prependMessages(messages) {
    if (!messages || messages.length === 0) return;

    // 记录当前滚动位置
    const oldScrollHeight = chatMessages.scrollHeight;
    const oldScrollTop = chatMessages.scrollTop;

    // 找到第一个可见消息元素（用于在其前面插入）
    const firstVisibleChild = chatMessages.firstElementChild;

    // 创建临时容器来渲染新消息
    const fragment = document.createDocumentFragment();

    messages.forEach((msg, idx) => {
        const role = msg.role;
        if (role === 'system') return;

        const msgId = msg._meta?.id;
        const content = msg.content;
        const blocks = normalizeContent(content);

        if (role === 'user') {
            const textParts = [];
            const imageParts = [];
            const toolResultBlocks = [];

            blocks.forEach(block => {
                if (block.kind === 'text' && block.text) textParts.push(block.text);
                else if (block.kind === 'image') imageParts.push(block);
                else if (block.kind === 'tool_result') toolResultBlocks.push(block);
            });

            const combinedText = textParts.join('\n');
            if (combinedText || imageParts.length > 0) {
                const div = addMessage('user', combinedText, msgId, msg._meta?.created_at, null);
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
                fragment.appendChild(div);
            }

            toolResultBlocks.forEach(block => {
                if (block._meta && block._meta.exec_id) {
                    const execId = block._meta.exec_id;
                    const isCompleted = block._meta.completed === true;
                    const saMsg = {
                        exec_id: execId,
                        task_summary: block._meta.task_summary || '',
                        status: isCompleted ? 'completed' : 'running',
                        iterations: block._meta.iterations || 0,
                        message_count: block._meta.message_count || 0,
                        tool_call_count: block._meta.tool_call_count || 0,
                    };
                    renderAgentRef(saMsg, idx, msgId);
                    return;
                }
                if (block._meta && block._meta.completed === false) return;
                const text = typeof block.content === 'string' ? block.content : JSON.stringify(block.content, null, 2);
                const div = addMessage('assistant', '', msgId, msg._meta?.created_at, null);
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
                fragment.appendChild(div);
            });

            return;
        }

        blocks.forEach(block => {
            if (block.kind === 'text' && block.text) {
                const div = addMessage(role, block.text, msgId, msg._meta?.created_at, null);
                fragment.appendChild(div);
            } else if (block.kind === 'thinking' && block.text) {
                const div = addMessage('assistant', '', msgId, msg._meta?.created_at, null);
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
                fragment.appendChild(div);
            } else if (block.kind === 'tool_call') {
                const div = addMessage('assistant', '', msgId, msg._meta?.created_at, null);
                div.classList.add('tool');
                const contentDiv = div.querySelector('.message-content');
                const pre = document.createElement('pre');
                pre.textContent = JSON.stringify(block.input, null, 2);
                const toolTitle = document.createElement('div');
                toolTitle.className = 'tool-title';
                toolTitle.textContent = `[调用工具: ${block.name}]`;
                contentDiv.appendChild(toolTitle);
                contentDiv.appendChild(pre);
                fragment.appendChild(div);
            } else if (block.kind === 'tool_result') {
                if (block._meta && block._meta.completed === false) return;
                const text = typeof block.content === 'string' ? block.content : JSON.stringify(block.content, null, 2);
                const div = addMessage('assistant', '', msgId, msg._meta?.created_at, null);
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
                fragment.appendChild(div);
            }
        });
    });

    // 插入到顶部
    if (firstVisibleChild) {
        chatMessages.insertBefore(fragment, firstVisibleChild);
    } else {
        chatMessages.appendChild(fragment);
    }

    // 恢复滚动位置（保持用户看到的内容不变）
    const newScrollHeight = chatMessages.scrollHeight;
    chatMessages.scrollTop = oldScrollTop + (newScrollHeight - oldScrollHeight);

    // 渲染数学公式
    if (window.MathJax && window.MathJax.typesetPromise) {
        MathJax.typesetPromise([chatMessages]).catch((err) => console.error('MathJax error:', err));
    }
}

// 加载更多历史消息
async function loadMoreMessages() {
    if (!currentWorkspace || !currentSession || isLoadingMore || !currentSessionHasMore) return;

    isLoadingMore = true;
    try {
        const newOffset = currentSessionLoadedOffset;
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${currentSession.session_id}?limit=50&offset=${newOffset}`);
        const data = await response.json();

        if (data.messages && data.messages.length > 0) {
            prependMessages(data.messages);
            currentSessionLoadedOffset += data.messages.length;
            currentSessionHasMore = data.has_more;
        } else {
            currentSessionHasMore = false;
        }
    } catch (error) {
        console.error('Failed to load more messages:', error);
    } finally {
        isLoadingMore = false;
    }
}

// 滚动事件监听 - 检测用户滚动到顶部时加载更多消息
const LOAD_MORE_THRESHOLD = 100; // 距离顶部多少像素时触发加载
chatMessages.addEventListener('scroll', () => {
    if (chatMessages.scrollTop <= LOAD_MORE_THRESHOLD && currentSessionHasMore && !isLoadingMore) {
        loadMoreMessages();
    }
});

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

// 子代理开始执行时立即渲染占位卡片（SSE 推送，无需等待完成）
// 委托 sse-client.js 幂等 ensureAgentCard（POST SSE 与事件流双源共用）
function renderAgentStart(execId, taskSummary) {
    ensureAgentCard(execId, taskSummary, { status: 'running' });
}

// 子代理完成时更新卡片状态——委托 sse-client.js 幂等 markAgentComplete
function renderAgentComplete(execId) {
    markAgentComplete(execId, 'completed');
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
                        } else if (event.type === 'agent_start') {
                            renderAgentStart(event.exec_id, event.task_summary);
                        } else if (event.type === 'agent_complete') {
                            renderAgentComplete(event.exec_id);
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
    const sessionId = currentSession.session_id;

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
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}/messages`,
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
                    } else if (event.type === 'agent_start') {
                        // 子代理开始执行，立即渲染占位卡片
                        renderAgentStart(event.exec_id, event.task_summary);
                    } else if (event.type === 'agent_complete') {
                        // 子代理完成，更新卡片状态
                        renderAgentComplete(event.exec_id);
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

        // Refresh session to get persisted state（仅当仍停留在该会话时，避免切走后被拉回）
        if (currentSession && currentSession.session_id === sessionId) {
            await loadSession(sessionId);
        }

    } catch (error) {
        console.error('Failed to send message:', error);
        addMessage('assistant', '发送消息失败: ' + error.message);
    } finally {
        clearTimeout(window._stopPendingTimer);
        // 仅当仍在查看本会话时才重置发送/停止状态，
        // 避免旧会话流结束时覆盖新会话的运行状态
        if (currentSession && currentSession.session_id === sessionId) {
            isSending = false;
            sendBtn.disabled = false;
            sendBtn.textContent = '发送';
            sendBtn.classList.remove('btn-danger');
            sendBtn.classList.add('btn-primary');
            chatMessages.scrollTop = chatMessages.scrollHeight;
        }
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

function doShare(msgId) {
    if (!currentWorkspace || !currentSession) return;
    const messageDiv = document.querySelector(`[data-msg-id="${msgId}"]`);
    if (!messageDiv) return;

    let ids;
    if (messageDiv.classList.contains('user')) {
        // user 消息：从该消息开始到结束
        const allMessages = document.querySelectorAll('.message[data-msg-id]');
        let startCollecting = false;
        const idSet = new Set();
        for (const div of allMessages) {
            if (div === messageDiv) startCollecting = true;
            if (startCollecting && div.dataset.msgId) {
                idSet.add(div.dataset.msgId);
            }
        }
        ids = Array.from(idSet).join(',');
    } else {
        // 其他消息：只分享自己
        ids = msgId;
    }

    const url = `${window.location.origin}/s/${currentWorkspace.uuid}/${currentSession.session_id}/${ids}`;
    window.open(url, '_blank');
}

async function doRevert(msgId) {
    if (!currentWorkspace || !currentSession) return;
    if (!confirm('确定要撤销到此消息吗？该消息及其后面的所有消息将被删除。')) return;

    try {
        const resp = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${currentSession.session_id}/revert`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ msg_id: msgId })
            }
        );

        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }

        const result = await resp.json();
        showToast(`已撤销 ${result.deleted_count} 条消息`);

        // 重新加载会话
        await loadSession(currentSession.session_id);
    } catch (err) {
        console.error('Failed to revert:', err);
        showToast('撤销失败: ' + err.message);
    }
}

// 消息气泡操作图标：复制 / 引用 / 分享 / 撤销（Material Design 图标，Apache 2.0）
const ACTION_ICONS = {
    copy: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M16 1H4a2 2 0 0 0-2 2v14h2V3h12V1zm3 4H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2zm0 16H8V7h11v14z"/></svg>',
    quote: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 17h3l2-4V7H5v6h3zm8 0h3l2-4V7h-6v6h3z"/></svg>',
    share: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M18 16.08c-.76 0-1.44.3-1.96.77L8.91 12.7c.05-.23.09-.46.09-.7s-.04-.47-.09-.7l7.05-4.11c.54.5 1.25.81 2.04.81 1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3c0 .24.04.47.09.7L8.04 9.81C7.5 9.31 6.79 9 6 9c-1.66 0-3 1.34-3 3s1.34 3 3 3c.79 0 1.5-.31 2.04-.81l7.12 4.16c-.05.21-.08.43-.08.65 0 1.61 1.31 2.92 2.92 2.92 1.61 0 2.92-1.31 2.92-2.92s-1.31-2.92-2.92-2.92z"/></svg>',
    revert: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12.5 8c-2.65 0-5.05.99-6.9 2.6L2 7v9h9l-3.62-3.62c1.39-1.16 3.16-1.88 5.12-1.88 3.54 0 6.55 2.31 7.6 5.5l2.37-.78C21.08 11.03 17.15 8 12.5 8z"/></svg>',
};

function makeMessageAction(title, iconKey, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'msg-action-btn';
    btn.title = title;
    btn.setAttribute('aria-label', title);
    btn.innerHTML = ACTION_ICONS[iconKey] || '';
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        onClick();
    });
    return btn;
}

// 把 "YYYY-MM-DD HH:MM:SS" 格式化为相对时间：
//   一周内 → 星期X HH:MM
//   一周外 → X月D日 HH:MM
function formatMessageTime(isoStr) {
    if (!isoStr) return '';
    // 兼容两种格式："2026-09-14 12:30:00" 与 "2026-09-14T12:30:00"
    const d = new Date(isoStr.replace(' ', 'T'));
    if (isNaN(d.getTime())) return '';
    const now = new Date();
    const diffDays = Math.floor((now - d) / (24 * 3600 * 1000));
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const time = hh + ':' + mm;
    if (diffDays < 7) {
        const weekdays = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];
        return weekdays[d.getDay()] + ' ' + time;
    }
    return (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + time;
}

// Add message to UI
// container: 可选，指定添加到哪个容器。默认为 chatMessages。传 null 则不添加。
function addMessage(role, content, msgId, createdAt, container = chatMessages) {
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
    }

    messageDiv.appendChild(contentDiv);

    // 有内容或有消息 ID 时添加菜单按钮、时间、data-msg-id
    if (content || msgId || createdAt) {
        if (msgId) {
            messageDiv.dataset.msgId = msgId;
        }
        if (content) {
            messageDiv.dataset.rawContent = content;
        }

        // 底部操作行：按钮 + 时间（所有角色气泡都显示）
        const actionsDiv = document.createElement('div');
        actionsDiv.className = 'message-actions';

        // 复制（仅 用户/助手 且有内容）
        if ((role === 'user' || role === 'assistant') && content) {
            actionsDiv.appendChild(makeMessageAction('复制', 'copy', () => doCopy(messageDiv)));
        }
        // 引用（仅 用户/助手）
        if (role === 'user' || role === 'assistant') {
            actionsDiv.appendChild(makeMessageAction('引用', 'quote', () => doQuote(messageDiv)));
        }
        // 撤销（仅用户消息）
        if (role === 'user' && msgId) {
            actionsDiv.appendChild(makeMessageAction('撤销', 'revert', () => doRevert(msgId)));
        }
        // 分享（有消息 ID 时）
        if (msgId) {
            actionsDiv.appendChild(makeMessageAction('分享', 'share', () => doShare(msgId)));
        }

        // 时间标签（紧跟按钮右侧）
        if (createdAt) {
            const timeSpan = document.createElement('span');
            timeSpan.className = 'message-time';
            timeSpan.textContent = formatMessageTime(createdAt);
            timeSpan.title = createdAt;
            actionsDiv.appendChild(timeSpan);
        }

        messageDiv.appendChild(actionsDiv);
    }

    if (container) {
        container.appendChild(messageDiv);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    return messageDiv;
}
