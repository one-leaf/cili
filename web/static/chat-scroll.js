// ── 聊天滚动贴底状态：翻看历史时不被流式更新拽回底部 ──
// 由 chat/sidebar/sse-client/agent-cards 共用；在 utils.js 之后加载。

// 贴底状态由用户滚动位置决定：离底部超过阈值即视为「正在翻看历史」，
// 之后的追加/流式更新一律不再把视图拽回底部（此前各调用点都是无条件
// 滚到底，工具气泡延迟创建、流式帧刷新都会打断用户阅读）。
// 用户自己滚回底部附近即自动恢复贴底；显式动作（打开会话、发送消息）传
// force=true 强制回底。
let _chatPinnedToBottom = true;
let _chatPinBound = false;

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

function _bindChatPinTracking() {
    if (_chatPinBound) return;
    const el = document.getElementById('chat-messages');
    if (!el) return;
    _chatPinBound = true;
    el.addEventListener('scroll', () => {
        _chatPinnedToBottom = _chatLastBlockVisible(el);
    }, { passive: true });
}

function isChatPinnedToBottom() {
    _bindChatPinTracking();
    return _chatPinnedToBottom;
}

function setChatPinnedToBottom(pinned) {
    _chatPinnedToBottom = !!pinned;
}

function scrollChatIfNearBottom(force = false) {
    const el = document.getElementById('chat-messages');
    if (!el) return;
    _bindChatPinTracking();
    if (force) _chatPinnedToBottom = true;
    if (!_chatPinnedToBottom) return;
    el.scrollTop = el.scrollHeight;
}
