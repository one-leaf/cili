// ── 聊天滚动贴底状态：翻看历史时不被流式更新拽回底部 ──
// 由 chat/sidebar/sse-client/agent-cards 共用；在 utils.js 之后加载。

// 贴底状态由用户滚动位置决定：离底部超过阈值即视为「正在翻看历史」，
// 之后的追加/流式更新一律不再把视图拽回底部（此前各调用点都是无条件
// 滚到底，工具气泡延迟创建、流式帧刷新都会打断用户阅读）。
// 用户自己滚回底部附近即自动恢复贴底；显式动作（打开会话、发送消息）传
// force=true 强制回底。
let _chatPinnedToBottom = true;
let _chatPinBound = false;
// 程序化滚动的目标位置：scroll 事件异步到达时，用它区分「自己滚的」
// 和「用户滚的」，避免一批气泡瞬间插入后把贴底状态误判成未贴底。
let _chatAutoScrollTarget = null;
let _chatAutoScrollAt = 0;
let _chatResizeObserver = null;

// 取消息区最后一个可视「块」：直接子元素，回合流容器下钻一层
function _lastChatBlock(el) {
    let node = el.lastElementChild;
    while (node) {
        if (node.classList && node.classList.contains('turn-stream')) {
            const inner = node.lastElementChild;
            if (inner) {
                node = inner;
                continue;
            }
        }
        return node;
    }
    return null;
}

// 跟随判定：最后一个块只要还有任意部分落在可视范围内（部分显示也算）
function _chatLastBlockVisible(el) {
    const last = _lastChatBlock(el);
    if (!last) return true;
    const box = el.getBoundingClientRect();
    const rect = last.getBoundingClientRect();
    return rect.bottom > box.top && rect.top < box.bottom;
}

function _consumeAutoScroll(el) {
    if (_chatAutoScrollTarget === null) return false;
    // 超过 300ms 还没到目标，视为用户接管（例如拖滚动条），不再拦截。
    if (Date.now() - _chatAutoScrollAt > 300) {
        _chatAutoScrollTarget = null;
        return false;
    }
    // scroll 事件在设置 scrollTop 之后异步到达；期间可能又插入了新气泡，
    // 所以不能只看「是否已经到目标」，只要目标还在就视为程序化滚动。
    if (el.scrollTop + 1 >= _chatAutoScrollTarget) {
        _chatAutoScrollTarget = null;
    }
    return true;
}

function _bindChatPinTracking() {
    if (_chatPinBound) return;
    const el = document.getElementById('chat-messages');
    if (!el) return;
    _chatPinBound = true;
    el.addEventListener('scroll', () => {
        if (_consumeAutoScroll(el)) return;
        _chatPinnedToBottom = _chatLastBlockVisible(el);
    }, { passive: true });
    // 用户主动滚动时取消程序化标记，让下面的 scroll 事件按真实位置判定
    const clearAuto = () => { _chatAutoScrollTarget = null; };
    el.addEventListener('wheel', clearAuto, { passive: true });
    el.addEventListener('touchmove', clearAuto, { passive: true });
    // 快速批量插入/图片撑高/字体回流都会改 scrollHeight；贴底时跟随，
    // 不依赖每个调用点都记得 scheduleScrollToBottom。
    if (typeof ResizeObserver === 'function') {
        _chatResizeObserver = new ResizeObserver(() => {
            if (_chatPinnedToBottom) scrollChatIfNearBottom();
        });
        _chatResizeObserver.observe(el);
    }
}

function isChatPinnedToBottom() {
    _bindChatPinTracking();
    return _chatPinnedToBottom;
}

function setChatPinnedToBottom(pinned) {
    _chatPinnedToBottom = !!pinned;
    if (!_chatPinnedToBottom) _chatAutoScrollTarget = null;
}

function scrollChatIfNearBottom(force = false) {
    const el = document.getElementById('chat-messages');
    if (!el) return;
    _bindChatPinTracking();
    if (force) _chatPinnedToBottom = true;
    if (!_chatPinnedToBottom) return;
    const target = el.scrollHeight;
    _chatAutoScrollTarget = target;
    _chatAutoScrollAt = Date.now();
    el.scrollTop = target;
}
