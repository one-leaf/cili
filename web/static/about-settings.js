// ── 关于页签：版本信息、升级检查/应用与轮询 ──
// 依赖 settings.js 的弹窗/状态工具。

// ─── 关于 Tab：版本与升级 ─────────────────────────────────────────────
// 只负责检查版本和下载升级包到脚本目录；解压覆盖由启动循环在服务重启时进行
// （下载完成后服务端会等会话空闲自动重启，也可手动「立即重启应用」）。

let updatePollTimer = null;
// 轮询间隔 10 秒；上限 15 分钟（后台最坏情况：4 个镜像 × 120s 超时 + 传输）
const UPDATE_POLL_INTERVAL_MS = 10000;
const UPDATE_POLL_MAX_MS = 15 * 60 * 1000;

let aboutVersionPromise = null;

// 读取 version.json 填充品牌头（静态文件缺失时保留默认文案）
async function loadAboutVersionInfo() {
    if (aboutVersionPromise) return aboutVersionPromise;
    aboutVersionPromise = (async () => {
        try {
            const resp = await fetch(`/static/version.json?t=${Date.now()}`);
            const v = await resp.json();
            const nameEl = document.getElementById('about-app-name');
            const authorEl = document.getElementById('about-author');
            if (nameEl && v.app_name) nameEl.textContent = v.app_name;
            if (authorEl && v.author) authorEl.textContent = 'by ' + v.author;
        } catch (e) {
            // 保留默认文案
        }
    })();
    return aboutVersionPromise;
}

function setAboutStatus(el, text, kind) {
    if (!el) return;
    el.className = 'about-status' + (kind ? ' ' + kind : '');
    el.textContent = text;
}

function setAboutLocalVersion(version) {
    const v = version || '-';
    const localEl = document.getElementById('about-local-version');
    const heroEl = document.getElementById('about-hero-version');
    if (localEl) localEl.textContent = v;
    if (heroEl) heroEl.textContent = v;
}

async function loadAboutStatus() {
    const statusEl = document.getElementById('about-update-status');
    if (!statusEl) return;
    loadAboutVersionInfo();
    const card = document.getElementById('about-card');
    try {
        const resp = await fetch('/api/update/status');
        const data = await resp.json();
        setAboutLocalVersion(data.local_version);
        const zipEl = document.getElementById('about-zip-status');
        const restartBtn = document.getElementById('btn-restart-update');
        const launcherEl = document.getElementById('about-launcher');
        if (launcherEl) {
            launcherEl.textContent = data.launcher_managed
                ? '当前运行方式：由 start.cmd 托管'
                : '当前运行方式：手动启动';
        }
        if (data.zip_present && data.zip_version) {
            zipEl.textContent = data.launcher_managed
                ? `升级包 ${data.zip_version} 已就绪，空闲时将自动重启应用。`
                : `升级包 ${data.zip_version} 已就绪，重启服务后自动应用。`;
            if (restartBtn) restartBtn.style.display = data.launcher_managed ? '' : 'none';
        } else {
            zipEl.textContent = '';
            if (restartBtn) restartBtn.style.display = 'none';
        }
        if (data.downloading) {
            if (card) card.classList.add('downloading');
            setAboutStatus(statusEl, '正在下载升级包…', '');
            pollDownloadStatus();
        } else {
            if (card) card.classList.remove('downloading');
            if (data.last_result && !data.last_result.success) {
                // 自动下载的失败也在这里可见（结果由 core.updater 统一记录）
                setAboutStatus(statusEl, '✗ ' + (data.last_result.error || '上次下载失败'), 'err');
            }
        }
    } catch (e) {
        setAboutStatus(statusEl, '✗ 加载升级状态失败: ' + e.message, 'err');
    }
}

async function checkForUpdate() {
    const statusEl = document.getElementById('about-update-status');
    const remoteEl = document.getElementById('about-remote-version');
    const badge = document.getElementById('about-update-badge');
    setAboutStatus(statusEl, '正在检查更新…', '');
    try {
        const resp = await fetch('/api/update/check');
        const data = await resp.json();
        if (data.error) {
            remoteEl.textContent = '检查失败';
            remoteEl.classList.add('pending');
            if (badge) badge.hidden = true;
            setAboutStatus(statusEl, '✗ ' + data.error, 'err');
            return;
        }
        remoteEl.textContent = data.remote_version;
        remoteEl.classList.remove('pending');
        if (badge) badge.hidden = !data.has_update;
        document.getElementById('btn-download-update').style.display = data.has_update ? '' : 'none';
        if (data.has_update) {
            setAboutStatus(statusEl, `发现新版本 ${data.remote_version}（当前 ${data.local_version}）`, 'info');
        } else {
            setAboutStatus(statusEl, '已是最新版本', 'ok');
        }
    } catch (e) {
        setAboutStatus(statusEl, '✗ 检查更新失败: ' + e.message, 'err');
    }
}

async function downloadUpdate() {
    const statusEl = document.getElementById('about-update-status');
    const card = document.getElementById('about-card');
    try {
        const resp = await fetch('/api/update/download', { method: 'POST' });
        const data = await resp.json();
        if (!data.success) {
            setAboutStatus(statusEl, '✗ ' + (data.error || '下载失败'), 'err');
            return;
        }
        if (card) card.classList.add('downloading');
        setAboutStatus(statusEl, '正在下载升级包…', '');
        pollDownloadStatus();
    } catch (e) {
        setAboutStatus(statusEl, '✗ 发起下载失败: ' + e.message, 'err');
    }
}

async function restartUpdateNow() {
    const statusEl = document.getElementById('about-update-status');
    try {
        const resp = await fetch('/api/update/restart', { method: 'POST' });
        const data = await resp.json();
        if (!data.success) {
            setAboutStatus(statusEl, '✗ ' + (data.error || '重启失败'), 'err');
            return;
        }
        setAboutStatus(statusEl, '服务重启中，等待新实例上线…', '');
        pollRestartStatus();
    } catch (e) {
        setAboutStatus(statusEl, '✗ 发起重启失败: ' + e.message, 'err');
    }
}

function pollRestartStatus() {
    if (updatePollTimer) { clearInterval(updatePollTimer); updatePollTimer = null; }
    const startedAt = Date.now();
    updatePollTimer = setInterval(async () => {
        const elapsed = Date.now() - startedAt;
        if (elapsed > 3 * 60 * 1000) {
            clearInterval(updatePollTimer);
            updatePollTimer = null;
            setAboutStatus(document.getElementById('about-update-status'),
                '⚠ 等待重启超时，请稍后刷新页面确认', 'err');
            return;
        }
        try {
            const resp = await fetch('/api/update/status');
            if (!resp.ok) return;
            const data = await resp.json();
            // 新实例已上线的标志：升级包应用后已删除（zip_present 变 false）。
            // 前 8 秒旧实例可能仍在优雅停机，避免把旧实例当作重启完成。
            if (elapsed > 8000 && data.zip_present === false) {
                clearInterval(updatePollTimer);
                updatePollTimer = null;
                setAboutStatus(document.getElementById('about-update-status'),
                    '✓ 服务已重启，页面即将刷新', 'ok');
                setTimeout(() => location.reload(), 800);
            }
        } catch (e) {
            // 服务下线中：继续等待新实例上线
        }
    }, 3000);
}

function pollDownloadStatus() {
    if (updatePollTimer) return;
    const startedAt = Date.now();
    updatePollTimer = setInterval(async () => {
        try {
            const resp = await fetch('/api/update/status');
            const data = await resp.json();
            // 是否结束只看 downloading：不能用 last_result 为空当「尚未结束」，
            // 否则自动下载失败（没有结果写入口）后会无限轮询。
            if (data.downloading && Date.now() - startedAt < UPDATE_POLL_MAX_MS) return;
            clearInterval(updatePollTimer);
            updatePollTimer = null;
            const statusEl = document.getElementById('about-update-status');
            const card = document.getElementById('about-card');
            if (data.downloading) {
                // 超时兜底：后台仍在下载但轮询已达上限，停止轮询并提示
                setAboutStatus(statusEl, '⚠ 下载耗时过长，仍在后台进行；稍后重新打开本页查看结果', '');
                return;
            }
            if (card) card.classList.remove('downloading');
            const result = data.last_result;
            if (result && result.success) {
                setAboutStatus(statusEl, '✓ ' + (result.message || '升级包已下载'), 'ok');
                loadAboutStatus();  // 刷新升级包就绪提示
            } else if (result) {
                setAboutStatus(statusEl, '✗ ' + (result.error || '下载失败'), 'err');
            } else {
                // 无结果记录（异常路径兜底）：刷新整体状态并停止轮询
                loadAboutStatus();
            }
        } catch (e) {
            // 网络抖动：下一轮继续（受上面的时间上限约束）
        }
    }, UPDATE_POLL_INTERVAL_MS);
}

(function initAboutSettings() {
    document.addEventListener('DOMContentLoaded', () => {
        const checkBtn = document.getElementById('btn-check-update');
        const downloadBtn = document.getElementById('btn-download-update');
        const restartBtn = document.getElementById('btn-restart-update');
        if (checkBtn) checkBtn.addEventListener('click', checkForUpdate);
        if (downloadBtn) downloadBtn.addEventListener('click', downloadUpdate);
        if (restartBtn) restartBtn.addEventListener('click', restartUpdateNow);
        // 切到「关于」页签时刷新状态
        document.querySelectorAll('.settings-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                if (tab.dataset.tab === 'about') loadAboutStatus();
            });
        });
    });
})();
