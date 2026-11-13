// ── 输入区上下文占用指示器（Context Gauge）────────────────────────────
// 设计：workspace/default/cili-context-gauge-plan-2026-11-13.md
// 胶囊按钮显示「当前消息体 token / 模型窗口」占比；点击展开当前会话信息面板。
// 数据口径与自动压缩一致（core/compression.py count_messages_tokens）。
//
// 依赖 app.js 的全局：currentWorkspace / currentSession / sessionRunning / isSending /
// openAnchoredMenu；utils.js 的 escapeHtml / showToast。

const CONTEXT_POLL_MS = 4000;
const CONTEXT_LEVEL_WARN = 0.60;   // 注意
const CONTEXT_LEVEL_DANGER = 0.80; // 自动压缩阈值

let _ctxInitialized = false;
let _ctxSeq = 0;        // 丢弃过期响应
let _ctxReport = null;  // 最近一次成功响应
let _ctxReportKey = ''; // 该响应所属会话
let _ctxPopover = null; // 打开中的面板
let _ctxUnavailable = false;

let _ctxBtn = null;
let _ctxPct = null;
let _ctxBar = null;

function _ctxSessionKey() {
    return currentWorkspace && currentSession
        ? `${currentWorkspace.uuid}:${currentSession.session_id}`
        : '';
}

function _fmtInt(n) {
    return (Number(n) || 0).toLocaleString();
}

function _fmtPct(v) {
    return `${Math.round((Number(v) || 0) * 100)}%`;
}

function _fmtBytes(n) {
    n = Number(n) || 0;
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)} MB`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)} KB`;
    return `${n} B`;
}

function _fmtSpeed(v) {
    return `${(Number(v) || 0).toLocaleString(undefined, { maximumFractionDigits: 2 })} t/s`;
}

// ── 数据获取 ──────────────────────────────────────────────────────────

async function refresh() {
    const key = _ctxSessionKey();
    if (!key) {
        _hideGauge();
        close();
        return;
    }
    const seq = ++_ctxSeq;
    try {
        const resp = await fetch(
            `/api/workspaces/${currentWorkspace.uuid}/sessions/${currentSession.session_id}/context`
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        if (seq !== _ctxSeq || key !== _ctxSessionKey()) return; // 已切会话/已有新请求
        _ctxReport = data;
        _ctxReportKey = key;
        _ctxUnavailable = false;
        _renderGauge(data);
        if (_ctxPopover) _renderPopover(data);
    } catch (e) {
        if (seq !== _ctxSeq || key !== _ctxSessionKey()) return;
        _paintUnavailable(key);
    }
}

function _paintUnavailable(key) {
    _ctxUnavailable = true;
    if (_ctxReportKey !== key) {
        // 从未成功过：显示占位，保留旧会话的数值不串台
        _ctxReport = null;
        _ctxBtn.hidden = false;
        _ctxBtn.removeAttribute('data-level');
        _ctxPct.textContent = '—';
        _ctxBar.style.strokeDashoffset = '100';
        _ctxBtn.title = '上下文获取失败';
        _ctxBtn.setAttribute('aria-label', '上下文获取失败');
        if (_ctxPopover) _renderPopover(null);
    }
}

function _hideGauge() {
    if (!_ctxBtn) return;
    _ctxBtn.hidden = true;
    _ctxBtn.removeAttribute('data-level');
    _ctxPct.textContent = '—';
    _ctxBar.style.strokeDashoffset = '100';
    _ctxBtn.title = '上下文占用';
    _ctxBtn.setAttribute('aria-label', '上下文占用');
}

// ── 胶囊渲染 ──────────────────────────────────────────────────────────

function _renderGauge(data) {
    const ratio = Number(data.ratio) || 0;
    const pct = Math.round(ratio * 100);
    const maxTokens = Number(data.max_context_tokens) || 0;
    const tokens = Number(data.context_tokens) || 0;

    _ctxBtn.hidden = false;
    if (ratio >= CONTEXT_LEVEL_DANGER) {
        _ctxBtn.dataset.level = 'danger';
    } else if (ratio >= CONTEXT_LEVEL_WARN) {
        _ctxBtn.dataset.level = 'warn';
    } else {
        _ctxBtn.removeAttribute('data-level');
    }

    const ringPct = Math.max(0, Math.min(100, pct));
    _ctxBar.style.strokeDashoffset = String(100 - ringPct);
    _ctxPct.textContent = pct < 1 && tokens > 0 ? '<1%' : `${pct}%`;

    const label = maxTokens > 0
        ? `上下文占用 ${pct}%（${_fmtInt(tokens)} / ${_fmtInt(maxTokens)} tokens）`
        : '上下文占用（上下文预算未配置）';
    _ctxBtn.title = label;
    _ctxBtn.setAttribute('aria-label', label);
}

// ── 面板渲染 ──────────────────────────────────────────────────────────

function _row(key, value, extraHtml = '') {
    return `<div class="ctx-row"><span class="ctx-key">${escapeHtml(key)}</span>`
        + `<span class="ctx-val">${value}${extraHtml}</span></div>`;
}

function _renderPopover(data) {
    if (!_ctxPopover) return;
    if (!data) {
        _ctxPopover.innerHTML = '<div class="ctx-loading">加载中…</div>';
        return;
    }

    const ratio = Number(data.ratio) || 0;
    const pct = Math.round(ratio * 100);
    const tokens = Number(data.context_tokens) || 0;
    const maxTokens = Number(data.max_context_tokens) || 0;
    const usage = data.usage || {};
    const sess = data.session || {};
    const model = data.model || {};
    const level = ratio >= CONTEXT_LEVEL_DANGER ? 'danger'
        : ratio >= CONTEXT_LEVEL_WARN ? 'warn' : 'normal';
    const barWidth = Math.max(0, Math.min(100, pct));
    const thresholdPct = Math.round((Number(data.compact_threshold_ratio) || 0.8) * 100);
    const overThreshold = maxTokens > 0 && tokens >= Number(data.compact_threshold_tokens || 0);

    const modelLabel = `${escapeHtml(model.name || '—')}`
        + (model.interface_type ? `（${escapeHtml(model.interface_type)}）` : '')
        + (data.estimated ? '<span class="ctx-tag">存盘估算</span>' : '');

    // 按模型分桶：最多 5 条
    const buckets = Object.entries(usage.models || {}).filter(([, b]) => b && typeof b === 'object');
    const shown = buckets.slice(0, 5).map(([name, b]) => (
        `<div class="ctx-model">${escapeHtml(name)}——${_fmtInt(b.api_calls)} 次，`
        + `输入 ${_fmtInt(b.input_tokens)} / 输出 ${_fmtInt(b.output_tokens)}</div>`
    )).join('');
    const more = buckets.length > 5
        ? `<div class="ctx-model">等 ${buckets.length} 个模型</div>` : '';

    const warning = overThreshold
        ? `<div class="ctx-warning">已达自动压缩阈值（${thresholdPct}%）：下一次模型调用会压缩早期历史</div>`
        : '';

    _ctxPopover.innerHTML = `
        <div class="ctx-head">
            <span class="ctx-title">上下文占用</span>
            <span class="ctx-pct" data-level="${level}">${pct}%</span>
        </div>
        <div class="ctx-bar" data-level="${level}">
            <div class="ctx-bar-fill" style="width:${barWidth}%"></div>
            <div class="ctx-bar-mark" style="left:${thresholdPct}%"></div>
        </div>
        <div class="ctx-bar-meta">
            ${_fmtInt(tokens)} / ${_fmtInt(maxTokens)} tokens · 剩余 ${_fmtInt(data.remaining_tokens)}
        </div>
        ${warning}
        <div class="ctx-section">
            ${_row('模型', modelLabel)}
            ${_row('预算', `${_fmtInt(maxTokens)} tokens`)}
            ${_row('请求体大小', _fmtBytes(data.body_size))}
        </div>
        <div class="ctx-section-title">会话</div>
        <div class="ctx-section">
            ${_row('会话 ID', escapeHtml(data.session_id || ''),
                '<button class="ctx-copy" type="button" data-copy-session '
                + 'title="复制会话 ID" aria-label="复制会话 ID">⧉</button>')}
            ${_row('消息', `${_fmtInt(sess.message_count)} 条 · 用户 ${_fmtInt(sess.user_turn_count)} 轮`)}
            ${_row('创建', escapeHtml(sess.created_at || '未知'))}
            ${_row('更新', escapeHtml(sess.updated_at || '未知'))}
        </div>
        <div class="ctx-section-title">用量</div>
        <div class="ctx-section">
            ${_row('API 调用', `${_fmtInt(usage.api_calls)} 次`)}
            ${_row('输入 / 输出', `${_fmtInt(usage.input_tokens)} / ${_fmtInt(usage.output_tokens)}`)}
            ${_row('缓存读 / 写', `${_fmtInt(usage.cache_read_tokens)} / ${_fmtInt(usage.cache_creation_tokens)}`)}
            ${_row('缓存比', _fmtPct(usage.cache_hit_ratio))}
            ${_row('速度', `预填充 ${_fmtSpeed(usage.prefill_speed)} · 生成 ${_fmtSpeed(usage.generation_speed)}`)}
            ${shown}${more}
        </div>
        <div class="ctx-footnote">占比为消息体估算（含工具输出与图片折算），不含系统提示词与工具定义；与自动压缩阈值同口径。</div>
    `;
}

// ── 面板开关 ──────────────────────────────────────────────────────────

function _isOpen() {
    return !!_ctxPopover;
}

function open() {
    if (_ctxPopover || !_ctxBtn) return;
    _ctxPopover = document.createElement('div');
    _ctxPopover.className = 'context-popover';
    const report = (!_ctxUnavailable && _ctxReportKey === _ctxSessionKey())
        ? _ctxReport : null;
    _renderPopover(report);
    _ctxPopover.addEventListener('click', _onPopoverClick);
    openAnchoredMenu(_ctxPopover, _ctxBtn, true);
    _ctxBtn.setAttribute('aria-expanded', 'true');
    refresh(); // 打开即刷新
}

function close() {
    if (!_ctxPopover) return;
    _ctxPopover.remove();
    _ctxPopover = null;
    if (_ctxBtn) _ctxBtn.setAttribute('aria-expanded', 'false');
}

function _toggle(e) {
    e.stopPropagation();
    if (_ctxPopover) close();
    else open();
}

function _onPopoverClick(e) {
    const copyBtn = e.target.closest('[data-copy-session]');
    if (!copyBtn) return;
    e.stopPropagation();
    _copySessionId();
}

function _onDocumentClick(e) {
    if (!_ctxPopover) return;
    if (_ctxBtn && _ctxBtn.contains(e.target)) return; // 按钮点击由 _toggle 处理
    if (_ctxPopover.contains(e.target)) return;
    close();
}

function _onKeydown(e) {
    if (e.key === 'Escape') close();
}

function _onScroll(e) {
    // 面板内部滚动不关闭；其余页面滚动关闭（与侧栏菜单一致）
    if (_ctxPopover && e.target instanceof Node && _ctxPopover.contains(e.target)) return;
    close();
}

// ── 复制会话 ID ───────────────────────────────────────────────────────

function _copySessionId() {
    const text = (currentSession && currentSession.session_id)
        || (_ctxReport && _ctxReport.session_id) || '';
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
            () => showToast('已复制'),
            () => showToast('复制失败')
        );
        return;
    }
    // 非安全上下文降级（与 file-tree.js 复制路径同一写法）
    const area = document.createElement('textarea');
    area.value = text;
    document.body.appendChild(area);
    area.select();
    try {
        document.execCommand('copy');
        showToast('已复制');
    } catch (e) {
        showToast('复制失败');
    }
    area.remove();
}

// ── 轮询 ──────────────────────────────────────────────────────────────

function _tick() {
    if (document.hidden) return;
    if (!currentSession || !currentWorkspace) return;
    if (sessionRunning || isSending || _ctxPopover) refresh();
}

// ── 初始化 ────────────────────────────────────────────────────────────

function init() {
    if (_ctxInitialized) return;
    _ctxInitialized = true;
    _ctxBtn = document.getElementById('context-gauge-btn');
    if (!_ctxBtn) return;
    _ctxPct = _ctxBtn.querySelector('.context-gauge-pct');
    _ctxBar = _ctxBtn.querySelector('.ring-bar');

    _ctxBtn.addEventListener('click', _toggle);
    document.addEventListener('click', _onDocumentClick);
    document.addEventListener('keydown', _onKeydown);
    window.addEventListener('resize', close);
    window.addEventListener('scroll', _onScroll, true);
    window.addEventListener('cili:turn-complete', () => refresh());
    setInterval(_tick, CONTEXT_POLL_MS);
}

window.ContextGauge = { init, refresh, close, isOpen: _isOpen };
