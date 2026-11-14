/* 「关于会话」：会话信息 + Git 风格时间线 */
(function () {
    'use strict';

    const state = {
        session: null,
        workspaceUuid: '',
        events: [],
        filter: 'all',
        bound: false,
        requestSeq: 0,
    };

    function $(id) {
        return document.getElementById(id);
    }

    function esc(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function formatNumber(value) {
        const number = Number(value || 0);
        return Number.isFinite(number) ? number.toLocaleString('zh-CN') : '0';
    }

    function dateLabel(ts) {
        const date = String(ts || '').slice(0, 10);
        if (!date) return '时间未知';
        const now = new Date();
        const today = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');
        if (date === today) return '今天';
        return date;
    }

    function timeLabel(ts) {
        const text = String(ts || '');
        return text.length >= 19 ? text.slice(11, 19) : text.slice(11);
    }

    function eventDetail(event) {
        const meta = event.meta || {};
        if (event.type === 'compaction') {
            const layer = Number(meta.compaction_layer || 1);
            if (layer === 2 && meta.before_tokens != null) {
                return formatNumber(meta.before_tokens) + ' → ' + formatNumber(meta.after_tokens) +
                    ' tokens · 移除 ' + formatNumber(meta.invalidated) + ' 条';
            }
            if (layer === 3) {
                if (meta.images_demoted != null) return '降级 ' + formatNumber(meta.images_demoted) + ' 张图片';
                if (meta.bytes_saved != null) return '节省 ' + formatNumber(meta.bytes_saved) + ' 字节';
            }
            if (meta.invalidated != null) return '标记 ' + formatNumber(meta.invalidated) + ' 条消息无效';
        }
        return '';
    }

    function render() {
        const list = $('about-timeline');
        if (!list) return;
        const wanted = state.filter === 'all' ? null : new Set(state.filter.split(','));
        const events = state.events.filter((event) => !wanted || wanted.has(event.type));
        if (!events.length) {
            list.innerHTML = '<div class="about-empty">暂无事件</div>';
            return;
        }

        const parts = [];
        let lastDate = '';
        for (const event of events) {
            const date = dateLabel(event.ts);
            if (date !== lastDate) {
                lastDate = date;
                parts.push('<div class="about-date"><span>' + esc(date) + '</span></div>');
            }
            let detail = eventDetail(event);
            if (event.type === 'tool' && event.meta && event.meta.tool_status === 'error') {
                detail = detail ? detail + ' · 执行失败' : '执行失败';
            }
            const toolResult = event.meta && event.meta.tool_result
                ? '<div class="about-snippet about-tool-result">↳ ' + esc(event.meta.tool_result) + '</div>'
                : '';
            const errorClass = event.type === 'tool' && event.meta && event.meta.tool_status === 'error'
                ? ' about-event-error' : '';
            parts.push(
                '<div class="about-event type-' + esc(event.type) + errorClass + '">' +
                    '<div class="about-rail"><span class="about-dot"></span></div>' +
                    '<div class="about-event-body">' +
                        '<div class="about-event-head">' +
                            '<span class="about-time">' + esc(timeLabel(event.ts)) + '</span>' +
                            '<span class="about-type">' + esc(event.title) + '</span>' +
                        '</div>' +
                        (event.snippet ? '<div class="about-snippet">' + esc(event.snippet) + '</div>' : '') +
                        toolResult +
                        (detail ? '<div class="about-detail">' + esc(detail) + '</div>' : '') +
                    '</div>' +
                '</div>'
            );
        }
        list.innerHTML = parts.join('');
        list.scrollTop = list.scrollHeight;  // 最新事件在底部

    }

    function renderInfo(session) {
        $('about-name').textContent = session.name || '(未命名会话)';
        $('about-hidden').hidden = !session.hidden;
        $('about-id').textContent = session.session_id || '';
        const ws = (typeof currentWorkspace !== 'undefined' && currentWorkspace) || null;
        const workspaceName = (ws && ws.name) || session.workspace_uuid || '';
        $('about-meta').textContent = [
            workspaceName ? '工作区：' + workspaceName : '',
            session.created_at ? '创建：' + session.created_at : '',
            session.updated_at ? '更新：' + session.updated_at : '',
        ].filter(Boolean).join(' · ');

        const usage = session.usage || {};
        const usageParts = [
            '消息 ' + formatNumber(session.message_count) + ' 条',
            session.model ? '模型 ' + session.model : '',
            'tokens ' + formatNumber(usage.input_tokens) + ' 输入 / ' + formatNumber(usage.output_tokens) + ' 输出',
            'API 调用 ' + formatNumber(usage.api_calls) + ' 次',
        ].filter(Boolean);
        $('about-usage').textContent = usageParts.join(' · ');
    }

    async function load() {
        const seq = ++state.requestSeq;
        const list = $('about-timeline');
        list.innerHTML = '<div class="about-loading">加载中…</div>';
        try {
            const response = await fetch(
                '/api/workspaces/' + encodeURIComponent(state.workspaceUuid) +
                '/sessions/' + encodeURIComponent(state.session.session_id) + '/timeline'
            );
            const data = await response.json().catch(() => ({}));
            if (seq !== state.requestSeq) return;
            if (!response.ok) throw new Error(data.detail || ('HTTP ' + response.status));
            renderInfo(data.session || {});
            state.events = data.events || [];
            render();
        } catch (error) {
            if (seq !== state.requestSeq) return;
            list.innerHTML = '<div class="about-empty">加载失败：' + esc(error.message) + '</div>';
        }
    }

    function copySessionId() {
        const text = $('about-id').textContent || '';
        if (!text) return;
        const done = () => {
            const btn = $('about-copy-id');
            const old = btn.textContent;
            btn.textContent = '已复制';
            setTimeout(() => { btn.textContent = old; }, 1200);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(fallbackCopy);
        } else {
            fallbackCopy();
        }
        function fallbackCopy() {
            const area = document.createElement('textarea');
            area.value = text;
            document.body.appendChild(area);
            area.select();
            try { document.execCommand('copy'); done(); } catch (e) { /* ignore */ }
            area.remove();
        }
    }

    function bind() {
        if (state.bound) return;
        state.bound = true;
        $('about-copy-id').addEventListener('click', copySessionId);
        $('about-filters').addEventListener('click', (event) => {
            const btn = event.target.closest('[data-about-filter]');
            if (!btn) return;
            state.filter = btn.getAttribute('data-about-filter');
            $('about-filters').querySelectorAll('.about-filter').forEach((item) => {
                item.classList.toggle('active', item === btn);
            });
            render();
        });
    }

    window.SessionAbout = {
        open: function (session) {
            if (!session || typeof currentWorkspace === 'undefined' || !currentWorkspace) return;
            bind();
            state.session = session;
            state.workspaceUuid = currentWorkspace.uuid;
            state.events = [];
            state.filter = 'all';
            $('about-filters').querySelectorAll('.about-filter').forEach((item) => {
                item.classList.toggle('active', item.getAttribute('data-about-filter') === 'all');
            });
            $('about-timeline').innerHTML = '<div class="about-loading">加载中…</div>';
            $('about-name').textContent = session.name || '(未命名会话)';
            $('about-id').textContent = session.session_id || '';
            $('about-hidden').hidden = !session.hidden;
            $('about-meta').textContent = '';
            $('about-usage').textContent = '';
            $('session-about-modal').style.display = 'flex';
            load();
        },
    };
})();
