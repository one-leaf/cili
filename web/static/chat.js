/** 占位符是否待用户应答（新键优先，旧会话回退 completed===false）。 */
function _isPendingPlaceholder(meta) {
    if (!meta) return false;
    if (meta.ask_user_completed !== undefined) return meta.ask_user_completed === false;
    return meta.completed === false;
}

// ── chat.js ── 聊天相关逻辑
// 依赖 app.js 全局变量: currentWorkspace, currentSession, isSending, pendingAttachments,
//   chatMessages, chatInput, composerBtn
// 依赖 utils.js 函数: renderMarkdownWithLinks, escapeHtml, showToast, savePosition
// 依赖 app.js 函数: loadSession, loadSessions

// 在飞回合的输出容器：sessionId → 该回合所有输出的专属容器。
// 本回合的气泡全部渲染进容器，而容器只在「正在查看该会话」时挂在消息区。
// 切走会话时 renderMessages 会清空消息区，容器随之脱离文档但**内容不丢**；
// 切回时 renderMessages 把它重新挂回末尾 —— 于是切走之前与切走期间的输出
// 都在，不必缓存事件再重放。
const activeTurnContainers = new Map();

// 把当前会话在飞回合的输出容器挂到消息区末尾（幂等：已是末尾则原样）。
// 由 renderMessages 在重建消息区之后调用 —— 这就是「切回会话即看到本回合
// 完整输出」的实现点。
function _reattachTurnContainer() {
    if (!currentSession) return;
    const container = activeTurnContainers.get(currentSession.session_id);
    if (container) {
        chatMessages.appendChild(container);
    }
}

// ── 工具输出实时流式显示 ──
// 后端 /stream/{tool_use_id} 端点支持增量读取，_run_bash 边执行边写入；
// 工具实时输出由全局事件流（sse-client.js）推送 tool_output 事件驱动，不再轮询。
// 工具输出显示为独立的消息气泡（类似思考块），执行完毕后自动消失
// 延迟5秒显示：快速执行的工具不会产生气泡闪烁
// （有实时输出事件时 sse-client 会跳过延迟立即建气泡）

const toolStreamTimers = {};  // { tool_use_id: { delayTimer, pre, offset, div } }
const _pendingAgentSpawnCalls = new Set();

// 旧版工作流占位 tool_use（id = task-{8位hex}，用于生成 worker 卡）不渲染成
// 「[调用工具: agent]」气泡。新格式的步骤调用 id 是 task-{run_id}-{index}，
// 必须正常显示，因此只匹配旧占位的精确形态。
function _isHiddenLegacyPlaceholder(block) {
    if (!block || block.kind !== 'tool_call') return false;
    if (block._meta && block._meta.hidden === true) return true;
    return block.name === 'agent' && typeof block.id === 'string'
        && /^task-[0-9a-f]{8}$/.test(block.id);
}
const TOOL_STREAMING_DELAY = 5000; // 5秒后才显示实时输出

function startToolStreaming(toolUseId, toolName, sessionId) {
    if (!currentWorkspace || !currentSession) return;
    if (toolStreamTimers[toolUseId]) return;

    // 先记录 entry；5 秒后若无 tool_output 事件，由延迟定时器创建气泡。
    // 记下所属会话：气泡只在仍查看该会话时创建，否则会塞进别的会话界面。
    toolStreamTimers[toolUseId] = {
        delayTimer: null, pre: null, offset: 0, div: null,
        sessionId: sessionId || currentSession.session_id,
    };
    toolStreamTimers[toolUseId].delayTimer = setTimeout(() => {
        const entry = toolStreamTimers[toolUseId];
        if (!entry) return;
        entry.delayTimer = null;
        ensureMasterToolBubble(toolUseId, toolName);
    }, TOOL_STREAMING_DELAY);
}

function stopToolStreaming(toolUseId) {
    const entry = toolStreamTimers[toolUseId];
    if (entry) {
        // 清除延迟定时器（如果5秒内完成，气泡还没创建）
        if (entry.delayTimer) clearTimeout(entry.delayTimer);
        // 删除独立的输出气泡（如果已经创建）
        if (entry.div && entry.div.parentNode) {
            entry.div.remove();
        }
        delete toolStreamTimers[toolUseId];
    }
}

// 清理所有流式定时器（会话切换时调用）
function clearAllToolStreaming() {
    for (const id of Object.keys(toolStreamTimers)) {
        stopToolStreaming(id);
    }
    _pendingAgentSpawnCalls.clear();
}

// 输入框底部左侧：附件 chip（预览选区 + Ctrl+V 上传文件）
function renderComposerAttachments() {
    _syncEditorChipWorkspace();
    const area = document.getElementById('input-attachments');
    if (!area) return;
    area.innerHTML = '';
    const items = [];

    const ctx = (window.FileTree && typeof window.FileTree.getEditorContext === 'function')
        ? window.FileTree.getEditorContext()
        : null;
    _editorContextChip.ctx = ctx;
    if (ctx && !(_editorContextChip.suppressedKey && _editorContextChip.suppressedKey === _editorContextKey(ctx))) {
        const sel = ctx.selection;
        items.push({
            kind: 'editor',
            label: sel ? `@${ctx.path}:${sel.startLine}-${sel.endLine}` : `@${ctx.path}`,
            title: sel ? `${ctx.path}:${sel.startLine}-${sel.endLine}` : ctx.path,
        });
    }
    pendingAttachments.forEach((att, idx) => {
        if (ctx && att.path === ctx.path) return;
        items.push({ kind: 'file', idx, label: `@${att.name}`, title: att.path });
    });

    const wrap = area.parentElement;
    if (items.length === 0) {
        area.hidden = true;
        if (wrap) wrap.classList.remove('has-attachments');
        return;
    }
    area.hidden = false;
    if (wrap) wrap.classList.add('has-attachments');
    items.forEach(item => {
        const chip = document.createElement('span');
        chip.className = 'input-chip';
        const text = document.createElement('span');
        text.className = 'input-chip-text';
        text.textContent = item.label;
        text.title = item.title;
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'input-chip-remove';
        remove.title = '移除';
        remove.textContent = '×';
        if (item.kind === 'editor') {
            remove.addEventListener('click', () => {
                _suppressEditorContext(_editorContextKey(_editorContextChip.ctx));
                renderComposerAttachments();
                updateComposerButtons();
            });
        } else {
            remove.addEventListener('click', () => {
                pendingAttachments.splice(item.idx, 1);
                renderComposerAttachments();
                updateComposerButtons();
            });
        }
        chip.appendChild(text);
        chip.appendChild(remove);
        area.appendChild(chip);
    });
}

// ── 编辑器上下文引用 chip（预览选区/打开文件 → 随消息发送）──
let _editorContextChip = { ctx: null, suppressedKey: null, workspaceUuid: null };

const _EDITOR_CTX_SUPPRESS_PREFIX = 'cili_editor_ctx_suppressed:';

function _editorChipWorkspaceUuid() {
    return (typeof currentWorkspace !== 'undefined' && currentWorkspace && currentWorkspace.uuid) || '';
}

function _syncEditorChipWorkspace() {
    const uuid = _editorChipWorkspaceUuid();
    if (_editorContextChip.workspaceUuid === uuid) return;
    _editorContextChip.workspaceUuid = uuid;
    try {
        _editorContextChip.suppressedKey = localStorage.getItem(
            _EDITOR_CTX_SUPPRESS_PREFIX + uuid);
    } catch (e) {
        _editorContextChip.suppressedKey = null;
    }
}

function _suppressEditorContext(key) {
    _syncEditorChipWorkspace();
    _editorContextChip.suppressedKey = key || null;
    try {
        const storageKey = _EDITOR_CTX_SUPPRESS_PREFIX + _editorChipWorkspaceUuid();
        if (key) localStorage.setItem(storageKey, key);
        else localStorage.removeItem(storageKey);
    } catch (e) { /* ignore */ }
}

function _editorContextKey(ctx) {
    if (!ctx || !ctx.path) return null;
    const sel = ctx.selection;
    return sel ? `${ctx.path}:${sel.startLine}-${sel.endLine}` : ctx.path;
}

// 发送后引用即被消费：抑制当前编辑器上下文 chip，直到选区/文件再次变化
// （与点 × 移除同一机制；若用户在编辑器里改选区，会作为新引用重新出现）
function suppressEditorContextChip() {
    if (!(window.FileTree && typeof window.FileTree.getEditorContext === 'function')) return;
    const ctx = window.FileTree.getEditorContext();
    if (ctx) _suppressEditorContext(_editorContextKey(ctx));
}

function getActiveEditorContext() {
    if (!(window.FileTree && typeof window.FileTree.getEditorContext === 'function')) return null;
    _syncEditorChipWorkspace();
    const ctx = window.FileTree.getEditorContext();
    if (!ctx) return null;
    if (_editorContextChip.suppressedKey && _editorContextChip.suppressedKey === _editorContextKey(ctx)) {
        return null;
    }
    return ctx;
}

chatInput.addEventListener('focus', renderComposerAttachments);
window.addEventListener('cili:editor-context-changed', renderComposerAttachments);

// Normalize message content into blocks
function normalizeContent(content) {
    const blocks = [];
    if (typeof content === 'string') {
        blocks.push({ kind: 'text', text: content });
    } else if (Array.isArray(content)) {
        content.forEach(block => {
            if (block.type === 'text') {
                blocks.push({ kind: 'text', text: block.text || '', _meta: block._meta || null });
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
// 出站文件卡片（send_user_file 落下的 file_card 消息）：连续出现 ≥3 条时折叠成
// 一个附件块——单个文件仍按逐条卡片显示，成批产物不刷屏。
const FILE_CARD_FOLD_THRESHOLD = 3;

function _fileCardGroups(messages) {
    const groups = new Map();  // 消息下标 → { head, cards }
    let i = 0;
    while (i < messages.length) {
        if (!messages[i]._meta?.file_card) { i++; continue; }
        let j = i;
        const cards = [];
        while (j < messages.length && messages[j]._meta?.file_card) {
            cards.push(messages[j]._meta.file_card);
            j++;
        }
        if (cards.length >= FILE_CARD_FOLD_THRESHOLD) {
            for (let k = i; k < j; k++) groups.set(k, { head: k === i, cards });
        }
        i = j;
    }
    return groups;
}

function _renderFileCardGroup(cards) {
    const lines = [`📎 附件（${cards.length} 个）`, ''];
    cards.forEach(card => {
        const note = card.note ? ` —— ${card.note}` : '';
        lines.push(`- [${card.name}](${card.path})${note}`);
    });
    addMessage('assistant', lines.join('\n'));
}

// 兼容旧消息：从 [Attachment]/[Editor context] 文本中恢复附件信息（新消息走 _meta.attachment）
function _attachmentFromLegacyText(text) {
    const s = String(text || '');
    let m = s.match(/^\[Attachment\] `([^`]+)`/);
    if (m) return { path: m[1], selection: null };
    m = s.match(/^\[Editor context\]\nThe user selected lines (\d+)-(\d+) from `([^`]+)`:/);
    if (m) return { path: m[3], selection: { startLine: Number(m[1]), endLine: Number(m[2]) } };
    m = s.match(/^\[Editor context\]\nThe user has file `([^`]+)` open/);
    if (m) return { path: m[1], selection: null };
    return null;
}

function _renderUserAttachmentChips(container, attachments) {
    if (!container || !attachments || attachments.length === 0) return;
    const existing = container.querySelector('.user-attachments');
    if (existing) existing.remove();
    const row = document.createElement('div');
    row.className = 'user-attachments';
    attachments.forEach(att => {
        const chip = document.createElement('span');
        chip.className = 'user-attachment';
        const path = att.path || '';
        const sel = att.selection;
        let label = `@${path}`;
        if (sel && sel.startLine) {
            label = `@${path}:${sel.startLine}-${sel.endLine}`;
        } else if (/^[A-Za-z]:[\\/]/.test(path) || path.startsWith('/')) {
            // 工作区外绝对路径：chip 只显示文件名，完整路径放 title
            label = `@${path.split(/[\\/]/).filter(Boolean).pop() || path}`;
        }
        chip.textContent = label;
        chip.title = path;
        row.appendChild(chip);
    });
    // 附件 chip 在正文下方（与消息块顺序一致：正文在前、附件在后）
    container.appendChild(row);
}

// 最近一次渲染的会话（workspace:session）：与本次相同说明是同一会话的重建
// （回合落盘/撤销后刷新），应保留用户阅读位置；不同则是切换会话，回到最新。
let _renderedChatSessionKey = null;

function renderMessages(messages) {
    const sessionKey = currentSession
        ? sessionKeyOf(currentWorkspace ? currentWorkspace.uuid : '', currentSession.session_id)
        : null;
    const sameSession = sessionKey !== null && sessionKey === _renderedChatSessionKey;
    // 重建会清空 DOM，先把阅读位置与贴底状态记下来
    const prevScrollTop = chatMessages.scrollTop;
    const wasPinned = isChatPinnedToBottom();
    _renderedChatSessionKey = sessionKey;

    chatMessages.innerHTML = '';
    // 全局事件流那条路的渲染器持有 DOM 引用，清空后必须重置
    if (typeof resetMasterStreamBlocks === 'function') {
        resetMasterStreamBlocks();
    }
    if (!messages || messages.length === 0) {
        chatMessages.innerHTML = newSessionWelcomeHtml();
        // 空会话也要挂回在飞容器（否则新会话第一条消息切回后看不到输出）。
        // 注意 innerHTML 赋值会清掉已挂的子节点，必须在之后调用。
        _reattachTurnContainer();
        return;
    }

    // 出站文件卡片连续 ≥3 条时折叠（映射：消息下标 → 组）
    const fileGroups = _fileCardGroups(messages);

    // 子代理卡依附于 tool_use：预扫 tool_result 的 _meta，按 tool_use_id 关联。
    const callMeta = new Map();
    messages.forEach((msg) => {
        const bs = normalizeContent(msg.content);
        bs.forEach((b) => {
            if (b.kind === 'tool_result' && b.tool_use_id && b._meta && b._meta.exec_id) {
                callMeta.set(b.tool_use_id, b._meta);
            }
        });
    });
    const anchoredCalls = new Set();

    messages.forEach((msg, idx) => {
        const role = msg.role;
        if (role === 'system') return;
        // 后台 agent 完成通知：LLM 上下文需要但前端不显示
        if (msg._meta?.background_notification) return;

        // send_user_file 的出站文件卡片：成批时整组只渲染一次
        const fileGroup = fileGroups.get(idx);
        if (fileGroup) {
            if (fileGroup.head) _renderFileCardGroup(fileGroup.cards);
            return;
        }

        const msgId = msg._meta?.id;  // 消息唯一 ID，用于分享链接
        const content = msg.content;
        const blocks = normalizeContent(content);

        // For user messages, separate user content from tool results
        if (role === 'user') {
            const textParts = [];
            const imageParts = [];
            const attachmentParts = [];
            const toolResultBlocks = [];

            blocks.forEach(block => {
                if (block.kind === 'text' && block._meta && block._meta.attachment) {
                    const att = block._meta.attachment;
                    if (att && typeof att === 'object' && att.path) attachmentParts.push(att);
                    return;
                }
                if (block.kind === 'text' && block._meta && (block._meta.editor_context || block._meta.file_mention)) return;
                if (block.kind === 'text') {
                    const legacy = _attachmentFromLegacyText(block.text);
                    if (legacy) { attachmentParts.push(legacy); return; }
                }
                if (block.kind === 'text' && block.text) textParts.push(block.text);
                else if (block.kind === 'image') imageParts.push(block);
                else if (block.kind === 'tool_result') toolResultBlocks.push(block);
            });

            // Only create user bubble if there's actual user content
            const combinedText = textParts.join('\n');
            if (combinedText || imageParts.length > 0 || attachmentParts.length > 0) {
                const div = addMessage('user', combinedText, msgId, msg._meta?.created_at);
                const contentDiv = div.querySelector('.message-content');
                if (imageParts.length > 0) {
                    const imgContainer = document.createElement('div');
                    imgContainer.className = 'user-images';
                    imageParts.forEach(img => {
                        const imgEl = document.createElement('img');
                        imgEl.src = `data:${img.media_type};base64,${img.data}`;
                        imgEl.alt = '用户图片';
                        imgContainer.appendChild(imgEl);
                    });
                    contentDiv.appendChild(imgContainer);
                }
                if (attachmentParts.length > 0) {
                    _renderUserAttachmentChips(contentDiv, attachmentParts);
                }
            }

            // Render tool results as separate tool bubbles
            toolResultBlocks.forEach(block => {
                // 子代理结果：渲染为可折叠的卡片
                if (block._meta && block._meta.exec_id) {
                    // 已依附在 tool_use 位置的卡片：不再在 tool_result 位置重复
                    if (anchoredCalls.has(block.tool_use_id)) return;
                    const execId = block._meta.exec_id;
                    const isCompleted = block._meta.completed === true;
                    const saMsg = {
                        exec_id: execId,
                        task_summary: block._meta.task_summary || '',
                        status: block._meta.status || (isCompleted ? 'completed' : 'running'),
                        phase: block._meta.phase || '',
                        stats: block._meta.stats || null,
                        background: block._meta.background || false,
                        workflow_step: block._meta.workflow_step,
                        step_index: block._meta.step_index,
                        step_name: block._meta.step_name,
                        executor: block._meta.executor,
                        duration_ms: block._meta.duration_ms,
                    };
                    // 尝试从 tool_use 块获取任务摘要（在前面的消息中）
                    // 简单处理：用 exec_id 加载详情
                    renderAgentRef(saMsg, idx, msgId);
                    return;
                }
                // ask_user 等待中：跳过渲染
                if (block._meta && _isPendingPlaceholder(block._meta)) return;
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
                thinkDiv.innerHTML = renderMarkdownWithLinks(block.text);
                contentDiv.appendChild(thinkDiv);
            } else if (block.kind === 'tool_call') {
                if (_isHiddenLegacyPlaceholder(block)) return;
                const input = block.input || {};
                const agentSpawn = block.name === 'agent'
                    && (input.action || 'spawn') === 'spawn';
                const workflowCall = block.name === 'workflow_agent';
                if (agentSpawn || workflowCall) {
                    // 卡片直接依附在 tool_use 上：完成态用 tool_result meta，
                    // 运行态由 mountRunning 补挂；这里不再生成调用工具气泡。
                    const meta = callMeta.get(block.id);
                    const stepNo = Number(input.step) + 1;
                    const seed = (meta && meta.exec_id)
                        ? {
                            ...meta,
                            exec_id: meta.exec_id,
                            task_summary: meta.task_summary
                                || (workflowCall ? `第 ${stepNo} 步` : (input.task || '')),
                            status: meta.status || (meta.completed ? 'completed' : 'running'),
                            workflow_step: workflowCall ? true : !!meta.workflow_step,
                            step_index: meta.step_index || (workflowCall ? stepNo : 0),
                            step_name: meta.step_name || '',
                            executor: meta.executor || '',
                            duration_ms: meta.duration_ms || 0,
                            background: !!meta.background,
                        }
                        : {
                            // 没有 tool_result（失败/中断/仍在启动）：也先挂一张折叠卡，
                            // 运行中的卡随后由 mountRunning/SSE 绑定 exec_id。
                            exec_id: block.id,
                            pending: true,
                            task_summary: workflowCall ? '（等待执行）' : (input.task || '子代理任务'),
                            status: 'running',
                            workflow_step: workflowCall,
                            step_index: workflowCall ? stepNo : 0,
                            step_name: workflowCall ? '（等待执行）' : '',
                            executor: '',
                            background: false,
                        };
                    renderAgentRef(seed, idx, msgId);
                    anchoredCalls.add(block.id);
                    return;
                }
                const div = addMessage('assistant', '', msgId, msg._meta?.created_at);
                div.classList.add('tool');
                const contentDiv = div.querySelector('.message-content');
                if (block.name === 'ask_user' && (!block._meta || !block._meta.answered)) {
                    // Render interactive question/approval card only if unanswered
                    if (isApprovalInput(block.input)) {
                        renderApprovalCard(contentDiv, block.input, block.id);
                    } else {
                        renderAskUserQuestions(contentDiv, block.input, block.id);
                    }
                } else if (block.name === 'ask_user' && block._meta?.answered
                           && isApprovalInput(block.input)) {
                    // 已结算的审批卡：紧凑展示，不再渲染 JSON 参数
                    const settled = document.createElement('div');
                    settled.className = 'approval-settled';
                    const outcomeText = {
                        approved: '已允许（本次会话）',
                        remembered: '已允许并记住',
                        denied: '已拒绝',
                        expired: '已超时未响应（已按拒绝处理）',
                        aborted: '已终止任务',
                    }[block._meta.answered_outcome] || '已处理';
                    settled.textContent = `🛡️ 审批${outcomeText}`;
                    contentDiv.appendChild(settled);
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
                if (block._meta && _isPendingPlaceholder(block._meta)) return;
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

    // 在飞回合的输出容器重新挂回末尾：切回本会话时立刻可见，
    // 不必等下一帧（也覆盖「切回后本回合已不再产生新输出」的情况）
    _reattachTurnContainer();

    if (sameSession) {
        // 同一会话的重建（回合落盘后刷新、撤销后重载）：用户翻历史时不打断，
        // 回到原位置；本来就贴底则继续贴底。
        chatMessages.scrollTop = wasPinned ? chatMessages.scrollHeight : prevScrollTop;
        setChatPinnedToBottom(wasPinned);
    } else {
        // 打开/切回会话属于显式动作：强制回底并恢复贴底
        scrollChatIfNearBottom(true);
    }

    // Render math formulas with MathJax
    if (window.MathJax && window.MathJax.typesetPromise) {
        MathJax.typesetPromise([chatMessages]).catch((err) => console.error('MathJax error:', err));
    }
}

// 渲染子代理引用（可折叠卡片）——委托 sse-client.js 卡片状态机
function renderAgentRef(msg, idx, msgId) {
    const model = AgentCardStore.mountFromHistory(msg);
    if (model && msgId && model.cardEl) model.cardEl.dataset.msgId = msgId;
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
        // 后台 agent 完成通知：LLM 上下文需要但前端不显示
        if (msg._meta?.background_notification) return;

        const msgId = msg._meta?.id;
        const content = msg.content;
        const blocks = normalizeContent(content);

        if (role === 'user') {
            const textParts = [];
            const imageParts = [];
            const attachmentParts = [];
            const toolResultBlocks = [];

            blocks.forEach(block => {
                if (block.kind === 'text' && block._meta && block._meta.attachment) {
                    const att = block._meta.attachment;
                    if (att && typeof att === 'object' && att.path) attachmentParts.push(att);
                    return;
                }
                if (block.kind === 'text' && block._meta && (block._meta.editor_context || block._meta.file_mention)) return;
                if (block.kind === 'text') {
                    const legacy = _attachmentFromLegacyText(block.text);
                    if (legacy) { attachmentParts.push(legacy); return; }
                }
                if (block.kind === 'text' && block.text) textParts.push(block.text);
                else if (block.kind === 'image') imageParts.push(block);
                else if (block.kind === 'tool_result') toolResultBlocks.push(block);
            });

            const combinedText = textParts.join('\n');
            if (combinedText || imageParts.length > 0 || attachmentParts.length > 0) {
                const div = addMessage('user', combinedText, msgId, msg._meta?.created_at, null);
                const contentDiv = div.querySelector('.message-content');
                if (imageParts.length > 0) {
                    const imgContainer = document.createElement('div');
                    imgContainer.className = 'user-images';
                    imageParts.forEach(img => {
                        const imgEl = document.createElement('img');
                        imgEl.src = `data:${img.media_type};base64,${img.data}`;
                        imgEl.alt = '用户图片';
                        imgContainer.appendChild(imgEl);
                    });
                    contentDiv.appendChild(imgContainer);
                }
                if (attachmentParts.length > 0) {
                    _renderUserAttachmentChips(contentDiv, attachmentParts);
                }
                fragment.appendChild(div);
            }

            toolResultBlocks.forEach(block => {
                if (block._meta && block._meta.exec_id) {
                    // 已依附在 tool_use 位置的卡片：不再在 tool_result 位置重复
                    if (anchoredCalls.has(block.tool_use_id)) return;
                    const execId = block._meta.exec_id;
                    const isCompleted = block._meta.completed === true;
                    const saMsg = {
                        exec_id: execId,
                        task_summary: block._meta.task_summary || '',
                        status: block._meta.status || (isCompleted ? 'completed' : 'running'),
                        phase: block._meta.phase || '',
                        stats: block._meta.stats || null,
                        background: block._meta.background || false,
                        workflow_step: block._meta.workflow_step,
                        step_index: block._meta.step_index,
                        step_name: block._meta.step_name,
                        executor: block._meta.executor,
                        duration_ms: block._meta.duration_ms,
                    };
                    AgentCardStore.mountFromHistory(saMsg, fragment);
                    return;
                }
                if (block._meta && _isPendingPlaceholder(block._meta)) return;
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
                thinkDiv.innerHTML = renderMarkdownWithLinks(block.text);
                contentDiv.appendChild(thinkDiv);
                fragment.appendChild(div);
            } else if (block.kind === 'tool_call') {
                if (_isHiddenLegacyPlaceholder(block)) return;
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
                if (block._meta && _isPendingPlaceholder(block._meta)) return;
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

    const sessionId = currentSession.session_id;
    const epoch = sessionLoadEpoch;
    isLoadingMore = true;
    try {
        const newOffset = currentSessionLoadedOffset;
        const response = await fetch(`/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}?limit=50&offset=${newOffset}`);
        const data = await response.json();
        if (epoch !== sessionLoadEpoch || !currentSession || currentSession.session_id !== sessionId) return;

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
    // 清单在消息区顶部，插入/增高会把内容整体下推：贴底时跟随到底
    scrollChatIfNearBottom();
}

// ── 单回合流式渲染器（sendMessage 与 ask-user/answers 共用） ──
// 两个入口的 SSE 事件分派曾各写一份并已漂移；统一在这里维护。
function createTurnStreamRenderer({ sessionId, turnContainer, addTurnMessage, scrollIfViewing,
                                   onUserMessage = null, onQueued = null }) {
    let assistantDiv = null;
    let assistantContent = '';
    let thinkDiv = null;
    let thinkContent = '';
    let assistantRenderQueued = false;
    let thinkRenderQueued = false;

    // 本流是否属于「用户正在查看的会话」。
    // 回合流在用户切走后仍在消费（服务端要继续跑完并落盘），此时只能写
    // 自己的 turnContainer（已脱离文档，切回时挂回），任何全局 UI ——
    // 任务清单、子代理卡片、队列条、滚动位置 —— 都不能被这条流改动，
    // 否则旧会话的内容会显示到新会话里。
    const isCurrentSession = () =>
        !!currentSession && currentSession.session_id === sessionId;

    function scheduleAssistantRender() {
        if (assistantRenderQueued) return;
        assistantRenderQueued = true;
        requestAnimationFrame(() => {
            assistantRenderQueued = false;
            if (!assistantDiv) return;
            assistantDiv.dataset.rawContent = assistantContent;
            assistantDiv.querySelector('.message-content').innerHTML = renderMarkdownWithLinks(assistantContent);
            scrollIfViewing();
        });
    }

    function scheduleThinkRender() {
        if (thinkRenderQueued || !thinkDiv) return;
        thinkRenderQueued = true;
        requestAnimationFrame(() => {
            thinkRenderQueued = false;
            if (!thinkDiv) return;
            thinkDiv.innerHTML = renderMarkdownWithLinks(thinkContent);
            scrollIfViewing();
        });
    }

    function finalizeThinkBlock() {
        if (thinkDiv) {
            // 收尾前先同步补画一次：rAF 节流可能还没执行，而正文/工具事件与
            // 最后一批思考增量常在同一次 SSE 读取里到达，直接清空会丢掉这截尾巴。
            thinkDiv.innerHTML = renderMarkdownWithLinks(thinkContent);
            const thinkTitle = thinkDiv.parentElement.querySelector('.think-title');
            if (thinkTitle) thinkTitle.textContent = '💭 思考完成';
            thinkDiv = null;
            thinkContent = '';
        }
    }

    function finalizeAssistantBlock() {
        // 与思考块同理：切到工具调用/工具结果前，把节流中的最后一段正文补画出来，
        // 否则 assistantDiv 置 null 后，排队中的 rAF 会因引用为空而放弃渲染。
        if (assistantDiv) {
            assistantDiv.dataset.rawContent = assistantContent;
            assistantDiv.querySelector('.message-content').innerHTML = renderMarkdownWithLinks(assistantContent);
        }
        assistantDiv = null;
        assistantContent = '';
    }

    function handleEvent(event) {
        if (event.type === 'thinking') {
            if (!thinkDiv) {
                const div = addTurnMessage('assistant', '');
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
            scheduleThinkRender();
        } else if (event.type === 'text') {
            finalizeThinkBlock();
            assistantContent += event.content;
            if (!assistantDiv) {
                assistantDiv = addTurnMessage('assistant', assistantContent);
            } else {
                scheduleAssistantRender();
            }
        } else if (event.type === 'tool_use') {
            finalizeThinkBlock();
            // workflow_agent / agent spawn 由子代理卡展示，不显示调用工具气泡
            if (event.tool === 'workflow_agent') {
                finalizeAssistantBlock();
                return;
            }
            if (event.tool === 'agent') {
                const input = event.input || {};
                const action = input.action || 'spawn';
                if (action === 'spawn') {
                    _pendingAgentSpawnCalls.add(event.tool_use_id);
                    finalizeAssistantBlock();
                    return;
                }
            }
            const div = addTurnMessage('assistant', '');
            div.classList.add('tool');
            const contentDiv = div.querySelector('.message-content');
            if (event.tool === 'ask_user') {
                if (isApprovalInput(event.input)) {
                    renderApprovalCard(contentDiv, event.input, event.tool_use_id);
                } else {
                    renderAskUserQuestions(contentDiv, event.input, event.tool_use_id);
                }
            } else {
                const toolTitle = document.createElement('div');
                toolTitle.className = 'tool-title';
                toolTitle.textContent = `[调用工具: ${event.tool}]`;
                contentDiv.appendChild(toolTitle);
                const pre = document.createElement('pre');
                pre.textContent = JSON.stringify(event.input, null, 2);
                contentDiv.appendChild(pre);
                if (event.tool === 'bash' || event.tool === 'python') {
                    startToolStreaming(event.tool_use_id, event.tool, sessionId);
                }
            }
            finalizeAssistantBlock();
        } else if (event.type === 'tool_result') {
            stopToolStreaming(event.tool_use_id);
            if (event.tool === 'agent' && _pendingAgentSpawnCalls.has(event.tool_use_id)) {
                _pendingAgentSpawnCalls.delete(event.tool_use_id);
                return;
            }
            if (event.tool === 'ask_user') {
                // 限定在本回合容器内查找：全局查找会命中别的会话里的同名卡片
                const askCard = turnContainer.querySelector(`.ask-user-card[data-tool-use-id="${event.tool_use_id}"]`);
                if (askCard) {
                    const btn = askCard.querySelector('.ask-submit');
                    if (btn && !btn.classList.contains('ask-submitted')) {
                        btn.textContent = '✓ 已提交';
                        btn.classList.add('ask-submitted');
                        btn.disabled = true;
                    }
                    askCard.querySelectorAll('.ask-option').forEach(el => el.style.pointerEvents = 'none');
                    askCard.querySelectorAll('input').forEach(el => el.disabled = true);
                    settleAskUserCard(askCard);
                    updateComposerButtons();
                }
                return;
            }
            finalizeThinkBlock();
            const div = addTurnMessage('assistant', '');
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
            finalizeAssistantBlock();
        } else if (event.type === 'session_start') {
            // 卡片挂在全局消息区：仅在仍查看本会话时创建。
            // 切走期间跑完/在跑的子代理，切回时由 history 渲染与 mountRunning 补齐。
            if (isCurrentSession()) renderAgentRunStart(event.exec_id, event.task_summary);
        } else if (event.type === 'session_complete') {
            if (isCurrentSession()) renderAgentRunComplete(event.exec_id, event.stats, event.status);
        } else if (event.type === 'todo_update') {
            // 任务清单渲染在全局消息区顶部：旧会话的帧不得写进新会话
            if (isCurrentSession()) renderTodoList(event.todos);
        } else if (event.type === 'file') {
            const fileNote = event.note ? ` —— ${escapeHtml(event.note)}` : '';
            addTurnMessage('assistant', `📎 [${escapeHtml(event.name)}](${event.path})${fileNote}`);
        } else if (event.type === 'retry_clear') {
            if (assistantDiv) {
                assistantDiv.remove();
                assistantDiv = null;
            }
            assistantContent = '';
            finalizeThinkBlock();
        } else if (event.type === 'user_message') {
            // 回调会改全局队列条状态：切走后不得作用到新会话
            if (onUserMessage && isCurrentSession()) onUserMessage(event);
        } else if (event.type === 'queued') {
            if (onQueued && isCurrentSession()) onQueued(event);
        } else if (event.type === 'error') {
            addTurnMessage('assistant', `错误: ${escapeHtml(event.content)}`);
        }
    }

    function flush() {
        if (assistantDiv) {
            assistantDiv.dataset.rawContent = assistantContent;
            assistantDiv.querySelector('.message-content').innerHTML = renderMarkdownWithLinks(assistantContent);
        }
        if (thinkDiv) {
            thinkDiv.innerHTML = renderMarkdownWithLinks(thinkContent);
            const thinkTitle = thinkDiv.parentElement.querySelector('.think-title');
            if (thinkTitle) thinkTitle.textContent = '💭 思考完成';
        }
        if (window.MathJax && window.MathJax.typesetPromise) {
            MathJax.typesetPromise([turnContainer]).catch(() => {});
        }
    }

    return { handleEvent, flush };
}

// 子代理（agent run）开始执行时立即渲染占位卡片（POST SSE 推送）
function renderAgentRunStart(execId, taskSummary) {
    AgentCardStore.onStart({ exec_id: execId, task_summary: taskSummary, status: 'running' });
}

// 子代理（agent run）完成时更新卡片状态（POST SSE 不带 stats 时保留已有快照）
function renderAgentRunComplete(execId, stats, status) {
    // status 由后端终态透传；缺失时回落 completed（旧后端兼容）
    AgentCardStore.onComplete({ exec_id: execId, status: status || 'completed', stats });
}

// 是否存在待答的 ask_user / 审批卡（推荐输入按钮据此置灰）。
// 数据属性在卡片渲染时设置，提交/工具结果/停止时清除——历史加载渲染的卡片同样计数。
async function sendMessage() {
    // 先收尾语音转写，再读取输入框内容（未定稿的临时转写会并入正文）
    if (window.CiliVoice) window.CiliVoice.stop();
    const message = chatInput.value.trim();
    const editorContext = getActiveEditorContext();
    const attachments = [];
    const attachmentPaths = new Set();
    if (editorContext) {
        attachments.push({ path: editorContext.path, selection: editorContext.selection });
        attachmentPaths.add(editorContext.path);
    }
    for (const att of pendingAttachments) {
        if (attachmentPaths.has(att.path)) continue;
        attachmentPaths.add(att.path);
        attachments.push({ path: att.path, selection: null, media_type: att.media_type });
    }
    if (!message && attachments.length === 0) return;

    if (!currentWorkspace || !currentSession) {
        alert('请先选择工作区并创建会话');
        return;
    }

    // 他人会话：发送前明确提示并 fork 到自己的副本，不静默改写别人的会话。
    if (currentSession.owner_mine === false) {
        const owner = currentSession.owner_user_label
            || currentSession.owner_node_name
            || currentSession.owner_node_id
            || '其他节点';
        if (!confirm(`这是 ${owner} 的会话，将创建副本后继续。确定吗？`)) return;
        try {
            const resp = await fetch(
                `/api/workspaces/${currentWorkspace.uuid}/sessions/${currentSession.session_id}/fork`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ next_steps: '', label: '他人会话副本' }),
                }
            );
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || !data.session_id) {
                showToast(data.detail || '创建副本失败');
                return;
            }
            await loadSession(data.session_id);
            if (!currentSession) return;
        } catch (e) {
            showToast('创建副本失败: ' + (e.message || e));
            return;
        }
    }

    console.log('Sending message to session:', currentSession.session_id);
    const sessionId = currentSession.session_id;
    // 发送（含排队）即收起推荐并作废在途请求（设计 §2.3）
    if (typeof hideSuggestBar === 'function') hideSuggestBar({ invalidate: true });

    const clientId = makeClientId();
    const attachmentsToSend = attachments.length > 0 ? attachments : null;

    // 运行中：入队，不创建回合容器、不置 isSending（设计 §5.7.2）
    if (sessionRunning) {
        try {
            const resp = await fetch(
                `/api/workspaces/${currentWorkspace.uuid}/sessions/${sessionId}/queue`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        content: message,
                        client_id: clientId,
                        ...(attachmentsToSend ? { attachments: attachmentsToSend } : {}),
                    }),
                }
            );
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || !data.success) {
                showToast(data.detail || '排队失败，请稍后重试');
                return;
            }
            // 请求期间已切走：消息已入旧会话的队，清输入框 / 队列条都不能动新会话
            if (!currentSession || currentSession.session_id !== sessionId) return;
            chatInput.value = '';
            pendingAttachments = [];
            suppressEditorContextChip();
            renderComposerAttachments();
            upsertQueuedItem(data.item);
            updateComposerButtons();
        } catch (error) {
            console.error('Failed to enqueue message:', error);
            showToast('排队失败: ' + error.message);
        }
        return;
    }

    // 本页已有前台流时不再开第二条（会话空闲时 isSending 才会为真）
    if (isSending) return;

    // Clear input（按钮配色由 updateComposerButtons 统一决定）
    chatInput.value = '';
    pendingAttachments = [];
    suppressEditorContextChip();
    renderComposerAttachments();
    isSending = true;
    foregroundStreamSessionId = sessionId;
    sessionRunning = true;
    updateComposerButtons();

    // 本回合的助手侧输出全部渲染进专属容器（用户消息仍在消息区直接显示）。
    // 容器随会话可见性挂载/脱离：切走后内容留着，切回时 renderMessages 挂回。
    //
    // ⚠️ 容器必须**在用户消息之后**才挂进消息区。DOM 顺序即显示顺序，若先挂容器
    // 再 append 用户消息，本回合的流式输出就会渲染在用户刚发的那条消息**上方**。
    // （renderMessages 重建时是先渲染历史、最后 _reattachTurnContainer 挂容器，
    //   所以这里保持「用户消息 → 容器」的顺序才与重建结果一致。）
    const turnContainer = document.createElement('div');
    turnContainer.className = 'turn-stream';
    const addTurnMessage = (role, content) =>
        addMessage(role, content, undefined, undefined, turnContainer);
    const scrollIfViewing = () => {
        if (currentSession && currentSession.session_id === sessionId) {
            scrollChatIfNearBottom();
        }
    };

    // 清掉新会话的欢迎语：它是 app.js 用 innerHTML 直接设的，不在消息数组里，
    // 因此 renderMessages 不会覆盖它；不清就会一直停在用户第一条消息上方。
    chatMessages.querySelector('.welcome-message')?.remove();

    // Add user message to UI
    const userDiv = addMessage('user', message);
    if (attachmentsToSend) {
        _renderUserAttachmentChips(userDiv.querySelector('.message-content'), attachmentsToSend);
    }

    // 用户消息挂好后再挂助手容器（DOM 顺序 = 显示顺序）
    chatMessages.appendChild(turnContainer);
    activeTurnContainers.set(sessionId, turnContainer);

    // 主动发送是显式动作：无论此前是否在翻历史，都回底并恢复贴底
    scrollChatIfNearBottom(true);

    // 服务端确认投递（user_message 帧）时用它校正合并后的气泡内容
    const optimisticUserDiv = userDiv;

    // Create placeholder for assistant response
    const renderer = createTurnStreamRenderer({
        sessionId, turnContainer, addTurnMessage, scrollIfViewing,
        onUserMessage: (event) => {
            handleDeliveredBatch(event);
            if (optimisticUserDiv && event.content) {
                const contentDiv = optimisticUserDiv.querySelector('.message-content');
                if (contentDiv) {
                    contentDiv.innerHTML = renderMarkdownWithLinks(event.content);
                    if (attachmentsToSend) {
                        _renderUserAttachmentChips(contentDiv, attachmentsToSend);
                    }
                }
                optimisticUserDiv.dataset.rawContent = event.content;
            }
        },
        onQueued: (event) => {
            if (optimisticUserDiv) optimisticUserDiv.remove();
            if (event.item) upsertQueuedItem(event.item);
            showToast('消息已排队，当前回合结束后自动插入');
        },
    });

    try {
        const requestBody = { content: message, client_id: clientId };
        if (attachmentsToSend) {
            requestBody.attachments = attachmentsToSend;
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

                    // 丢弃不属于本流的帧
                    if (event.session_id && event.session_id !== sessionId) continue;

                    renderer.handleEvent(event);
                } catch (e) {
                    console.error('Failed to parse SSE event:', e, dataStr);
                }
            }
        }

        // 回合已结束并落盘：先 flush 节流中的渲染，再注销容器。
        renderer.flush();
        // 注销容器：否则重拉会把「已落盘的完整回合」和容器里那份流式渲染各显示一遍。
        activeTurnContainers.delete(sessionId);

        // Refresh session to get persisted state（仅当仍停留在该会话时，避免切走后被拉回）
        if (currentSession && currentSession.session_id === sessionId) {
            await loadSession(sessionId);
        }

    } catch (error) {
        console.error('Failed to send message:', error);
        if (currentSession && currentSession.session_id === sessionId) {
            addMessage('assistant', '发送消息失败: ' + error.message);
        }
    } finally {
        // 异常路径下也要注销容器，避免它一直挂在消息区
        activeTurnContainers.delete(sessionId);
        clearTimeout(window._stopPendingTimer);
        // 前台流已结束：无论是否还停留在该会话，本页都不再持有请求级流
        if (foregroundStreamSessionId === sessionId) {
            foregroundStreamSessionId = null;
        }
        // 仅当仍在查看本会话时才重置发送/停止状态，
        // 避免旧会话流结束时覆盖新会话的运行状态
        if (currentSession && currentSession.session_id === sessionId) {
            isSending = false;
            isStopping = false;
            // 回合可能还有排队批次在后台继续：状态以 /status 为准
            await refreshSessionStatus(sessionId);
            updateComposerButtons();
            renderQueueBar();
            // 回合结束不打断阅读：仅当用户还贴着底部时跟随
            scrollChatIfNearBottom();
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

// 消息气泡操作图标：复制 / 朗读 / 引用 / 分享 / 撤销（Material Design 图标，Apache 2.0）
const ACTION_ICONS = {
    copy: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M16 1H4a2 2 0 0 0-2 2v14h2V3h12V1zm3 4H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2zm0 16H8V7h11v14z"/></svg>',
    speak: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>',
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

// 批量渲染时合并滚动：每追加一个气泡都同步写 scrollTop 会触发一次布局，
// 50 条历史消息就是 50 次。统一在下一帧滚一次。
let scrollToBottomFrame = null;
function scheduleScrollToBottom() {
    if (scrollToBottomFrame !== null) return;
    scrollToBottomFrame = requestAnimationFrame(() => {
        scrollToBottomFrame = null;
        // 用户翻看历史时不再跟随追加（贴底与否由 scrollChatIfNearBottom 判断）
        scrollChatIfNearBottom();
    });
}

// Add message to UI
// container: 可选，指定添加到哪个容器。默认为 chatMessages。传 null 则不添加。
function addMessage(role, content, msgId, createdAt, container = chatMessages) {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${role}`;

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    if (content) {
        contentDiv.innerHTML = renderMarkdownWithLinks(content);

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
        // 朗读（仅助手消息；浏览器不支持 speechSynthesis 时不渲染）
        if (role === 'assistant' && content && window.CiliTTS) {
            let speakBtn = null;
            speakBtn = makeMessageAction('朗读', 'speak', () => window.CiliTTS.toggle(messageDiv, speakBtn));
            actionsDiv.appendChild(speakBtn);
        }
        // 引用（仅 用户/助手）
        if (role === 'user' || role === 'assistant') {
            actionsDiv.appendChild(makeMessageAction('引用', 'quote', () => doQuote(messageDiv)));
        }
        // 撤销（仅用户消息；青少年模式硬禁，不渲染）
        const teenOn = window.teenStatus && window.teenStatus.enabled;
        if (role === 'user' && msgId && !teenOn) {
            actionsDiv.appendChild(makeMessageAction('撤销', 'revert', () => doRevert(msgId)));
        }
        // 分享（有消息 ID 时；青少年模式 deny_share 时不渲染）
        const teenShareDenied = teenOn && !window.teenStatus.unlocked &&
            (window.teenStatus.policy || {}).deny_share;
        if (msgId && !teenShareDenied) {
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
        // 只在追加到「正在显示」的消息区时跟随滚动。容器已脱离文档时（用户切走
        // 去看别的会话）不能动滚动条，否则会把那个会话的视图拽到底部。
        if (container === chatMessages || chatMessages.contains(container)) {
            scheduleScrollToBottom();
        }
    }

    return messageDiv;
}
