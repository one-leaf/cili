// ── voice-input.js ── 语音输入（浏览器内置 Web Speech API）
// 依赖 utils.js 的 showToast；输入框 #chat-input，按钮 #mic-btn。
// 识别引擎由浏览器提供：Edge 走微软服务，Chrome 走 Google（国内不可用）。
// 不支持的浏览器（如 Firefox）保持按钮隐藏。

(() => {
    const micBtn = document.getElementById('mic-btn');
    const chatInput = document.getElementById('chat-input');
    if (!micBtn || !chatInput) return;

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) return;

    micBtn.hidden = false;

    let recognition = null;
    let recording = false;   // 用户期望的录音状态（按钮点亮）
    let prefix = '';         // 开始录音时输入框的光标前缀/后缀
    let suffix = '';
    let finalText = '';      // 已定稿的转写
    let interimText = '';    // 临时转写（实时显示，停止时并入正文）

    function applyText() {
        chatInput.value = prefix + finalText + interimText + suffix;
        chatInput.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function commitInterim() {
        if (!interimText) return;
        finalText += interimText;
        interimText = '';
        applyText();
    }

    function setRecording(on) {
        recording = on;
        micBtn.classList.toggle('recording', on);
        micBtn.title = on ? '停止语音输入' : '语音输入';
        micBtn.setAttribute('aria-label', micBtn.title);
    }

    function buildRecognition() {
        const rec = new SpeechRecognition();
        rec.lang = 'zh-CN';
        rec.continuous = true;
        rec.interimResults = true;

        rec.onresult = (e) => {
            interimText = '';
            for (let i = e.resultIndex; i < e.results.length; i++) {
                const text = e.results[i][0].transcript;
                if (e.results[i].isFinal) finalText += text;
                else interimText += text;
            }
            applyText();
        };

        rec.onerror = (e) => {
            if (e.error === 'no-speech' || e.error === 'aborted') return;
            recording = false;  // 阻止 onend 继续重启
            if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
                showToast('麦克风权限被拒绝，请在浏览器允许麦克风');
            } else if (e.error === 'network') {
                showToast('语音识别服务不可用（Chrome 国内请改用 Edge 打开）');
            } else if (e.error === 'audio-capture') {
                showToast('未检测到麦克风');
            } else {
                showToast('语音识别出错：' + e.error);
            }
        };

        rec.onend = () => {
            // continuous 模式下浏览器仍可能因静音自动结束：用户没停就续上
            if (recording) {
                try {
                    rec.start();
                    return;
                } catch (_) {
                    // 启动失败则复位
                }
            }
            setRecording(false);
            commitInterim();
        };
        return rec;
    }

    function start() {
        const startPos = chatInput.selectionStart ?? chatInput.value.length;
        const endPos = chatInput.selectionEnd ?? startPos;
        prefix = chatInput.value.slice(0, startPos);
        suffix = chatInput.value.slice(endPos);
        finalText = '';
        interimText = '';
        recognition = buildRecognition();
        try {
            recognition.start();
        } catch (e) {
            showToast('语音识别启动失败：' + e.message);
            return;
        }
        setRecording(true);
    }

    function stop() {
        if (!recording) return;
        setRecording(false);
        if (recognition) {
            try {
                recognition.stop();
            } catch (_) {
                // 已结束
            }
        }
        commitInterim();
    }

    micBtn.addEventListener('click', () => {
        if (recording) stop();
        else start();
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && recording) stop();
    });

    // 供 chat.js 在发送前调用：先收尾转写，再读取输入框内容
    window.CiliVoice = { stop, isRecording: () => recording };
})();
