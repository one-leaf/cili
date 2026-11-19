// ── tts.js ── 消息朗读（浏览器内置 speechSynthesis，离线零依赖）
// 依赖 utils.js 的 showToast；chat.js 在助手消息气泡上挂「朗读」按钮。
// 语音由系统提供：Windows 无中文语音包时提示安装路径。
// 不支持的浏览器（无 speechSynthesis）不渲染按钮（chat.js 以 window.CiliTTS 判断）。

(() => {
    const synth = window.speechSynthesis;
    if (!synth) return;

    const RATE_KEY = 'cili-tts-rate';
    const MAX_CHUNK = 160;   // 单条 utterance 上限：过长在 Chrome 会被静默截断

    let current = null;      // { btn, chunks, index }
    let gen = 0;             // 每次开始/停止递增，旧 utterance 的回调据此作废
    let voicesWarned = false;

    const BLOCK_TAGS = new Set([
        'P', 'DIV', 'LI', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
        'BLOCKQUOTE', 'UL', 'OL', 'TR',
    ]);
    // 代码块与表格读出来没有意义，跳过
    const SKIP_TAGS = new Set(['PRE', 'TABLE', 'SCRIPT', 'STYLE', 'SVG', 'BUTTON', 'IMG']);

    function currentRate() {
        const value = parseFloat(localStorage.getItem(RATE_KEY) || '1');
        return isFinite(value) && value >= 0.5 && value <= 2 ? value : 1;
    }

    function pickVoice() {
        const voices = synth.getVoices() || [];
        const zh = voices.filter(v => /^zh/i.test(v.lang || ''));
        if (!zh.length) return null;
        // 优先本地语音（离线可用）
        return zh.find(v => v.localService) || zh[0];
    }

    function warnNoChineseVoice() {
        if (voicesWarned) return;
        voicesWarned = true;
        showToast(
            '未检测到中文语音包，朗读效果可能异常。安装：设置 → 时间和语言 → 语言和区域 → 中文(简体) → 语言选项 → 语音',
            6000,
        );
    }

    function checkVoices() {
        if (pickVoice() !== null) return;
        if ((synth.getVoices() || []).length > 0) {
            warnNoChineseVoice();
            return;
        }
        // 语音列表异步加载中：等 voiceschanged 再判断，不阻断朗读
        const onLoaded = () => {
            if (pickVoice() === null) warnNoChineseVoice();
        };
        synth.addEventListener('voiceschanged', onLoaded, { once: true });
        setTimeout(onLoaded, 1000);
    }

    // 从渲染后的气泡提取可朗读文本（跳过代码块/表格等）
    function extractText(messageDiv) {
        const contentDiv = messageDiv.querySelector('.message-content');
        if (!contentDiv) return '';
        const parts = [];
        const walk = (node) => {
            if (node.nodeType === Node.TEXT_NODE) {
                parts.push(node.nodeValue);
                return;
            }
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            if (SKIP_TAGS.has(node.tagName)) return;
            const block = BLOCK_TAGS.has(node.tagName);
            if (block) parts.push('\n');
            node.childNodes.forEach(walk);
            if (block) parts.push('\n');
        };
        walk(contentDiv);
        return parts.join('')
            .replace(/[ \t ]+/g, ' ')
            .replace(/\s*\n\s*/g, '\n')
            .replace(/\n{3,}/g, '\n\n')
            .trim();
    }

    // 按段落/句末标点切块；无标点的超长文本硬切
    function chunkText(text) {
        const chunks = [];
        for (const para of text.split(/\n+/)) {
            const trimmed = para.trim();
            if (!trimmed) continue;
            const sentences = trimmed.match(/[^。！？!?；;]+[。！？!?；;]?/g) || [trimmed];
            let buf = '';
            for (const sentence of sentences) {
                if (sentence.length > MAX_CHUNK) {
                    if (buf) {
                        chunks.push(buf);
                        buf = '';
                    }
                    for (let i = 0; i < sentence.length; i += MAX_CHUNK) {
                        chunks.push(sentence.slice(i, i + MAX_CHUNK));
                    }
                    continue;
                }
                if (buf && buf.length + sentence.length > MAX_CHUNK) {
                    chunks.push(buf);
                    buf = '';
                }
                buf += sentence;
            }
            if (buf) chunks.push(buf);
        }
        return chunks.filter(c => c.trim());
    }

    function setBtnState(btn, speaking) {
        if (!btn) return;
        btn.classList.toggle('speaking', speaking);
        const title = speaking ? '停止朗读' : '朗读';
        btn.title = title;
        btn.setAttribute('aria-label', title);
    }

    function stop() {
        gen += 1;
        if (current) setBtnState(current.btn, false);
        current = null;
        synth.cancel();
    }

    function speakChunk() {
        if (!current) return;
        if (current.index >= current.chunks.length) {
            stop();
            return;
        }
        const myGen = gen;
        const utterance = new SpeechSynthesisUtterance(current.chunks[current.index++]);
        utterance.lang = 'zh-CN';
        utterance.rate = currentRate();
        const voice = pickVoice();
        if (voice) utterance.voice = voice;
        utterance.onend = () => {
            if (myGen === gen) speakChunk();
        };
        utterance.onerror = (event) => {
            if (myGen !== gen) return;
            if (event.error !== 'canceled' && event.error !== 'interrupted') {
                console.warn('[tts] 朗读失败:', event.error);
            }
            stop();
        };
        synth.speak(utterance);
    }

    function speak(messageDiv, btn) {
        const text = extractText(messageDiv);
        if (!text) {
            showToast('这条消息没有可朗读的文字');
            return;
        }
        stop();
        current = { btn, chunks: chunkText(text), index: 0 };
        setBtnState(btn, true);
        checkVoices();
        speakChunk();
    }

    // 同一按钮再次点击 = 停止；点另一条消息 = 切换
    function toggle(messageDiv, btn) {
        if (current && current.btn === btn) {
            stop();
            return;
        }
        speak(messageDiv, btn);
    }

    // 语速偏好：仅本浏览器生效（设置页「朗读语速」）
    function initRateControl() {
        const select = document.getElementById('system-tts-rate');
        if (!select) return;
        select.value = String(currentRate());
        select.addEventListener('change', () => {
            const value = parseFloat(select.value) || 1;
            localStorage.setItem(RATE_KEY, String(value));
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initRateControl);
    } else {
        initRateControl();
    }

    window.CiliTTS = { toggle, stop };
})();
