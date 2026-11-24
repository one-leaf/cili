// ── 教材下载面板（工作区设置 → 教材下载页签）──
// 依赖 app.js: currentWorkspace, showToast；依赖 file-manager/window 全局的 fetch 封装。

// ─── 教材下载 ───────────────────────────────────────────────────────────────

const textbookState = {
    initialized: false,
    loading: false,
    workspaceUuid: null,
    options: null,        // /api/wizard/options：学段/年级/科目/教材版本
    books: [],            // 匹配到的教材
    taskId: null,
    taskRunning: false,   // 任务是否仍在 queued/running（与 taskId 分离，便于失败后重试）
    pollTimer: null,
};

function textbookFormatSize(bytes) {
    const value = Number(bytes || 0);
    if (value >= 1024 * 1024 * 1024) return (value / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    if (value >= 1024 * 1024) return (value / 1024 / 1024).toFixed(1) + ' MB';
    if (value >= 1024) return (value / 1024).toFixed(0) + ' KB';
    return value + ' B';
}

function textbookSetStatus(message, type = '') {
    const el = document.getElementById('textbook-task-status');
    if (!el) return;
    el.textContent = message || '';
    el.className = 'settings-status' + (type ? ' ' + type : '');
}

function textbookSelectedBooks() {
    return textbookState.books.filter(book =>
        document.querySelector(`#textbook-list input[data-book-key="${book.book_key}"]`)?.checked
    );
}

function textbookSetButtons(taskRunning = false, hasFailures = false) {
    const downloadBtn = document.getElementById('textbook-download-btn');
    if (downloadBtn) downloadBtn.disabled = textbookState.loading || taskRunning || !textbookState.books.length;
    const refreshBtn = document.getElementById('textbook-refresh-btn');
    if (refreshBtn) refreshBtn.disabled = textbookState.loading || taskRunning;
    document.getElementById('textbook-cancel-btn').style.display = taskRunning ? '' : 'none';
    document.getElementById('textbook-retry-btn').style.display = (!taskRunning && hasFailures) ? '' : 'none';
}

async function initTextbookPanel() {
    const ws = currentWorkspace;
    if (!ws || ws.system || ws.uuid === 'system') {
        textbookSetStatus('系统工作区不支持教材下载', 'error');
        document.getElementById('textbook-list').innerHTML =
            '<div class="textbook-empty">系统工作区不支持教材下载。</div>';
        return;
    }
    if (textbookState.workspaceUuid !== ws.uuid) {
        textbookState.workspaceUuid = ws.uuid;
        textbookState.books = [];
        textbookState.taskId = null;
        textbookState.taskRunning = false;
        if (textbookState.pollTimer) {
            clearInterval(textbookState.pollTimer);
            textbookState.pollTimer = null;
        }
        document.getElementById('textbook-progress').style.display = 'none';
        document.getElementById('textbook-progress').value = 0;
    }
    if (!textbookState.initialized) {
        document.getElementById('textbook-stage').addEventListener('change', () => {
            renderTextbookGrades();
            renderTextbookSubjects();
        });
        document.getElementById('textbook-grade').addEventListener('change', renderTextbookSubjects);
        document.getElementById('textbook-refresh-btn').addEventListener('click', refreshTextbookPanel);
        document.getElementById('textbook-select-all-btn').addEventListener('click', () => {
            document.querySelectorAll('#textbook-list input[type="checkbox"]').forEach(cb => { cb.checked = true; });
            updateTextbookSummary();
        });
        document.getElementById('textbook-clear-btn').addEventListener('click', () => {
            document.querySelectorAll('#textbook-list input[type="checkbox"]').forEach(cb => { cb.checked = false; });
            updateTextbookSummary();
        });
        document.getElementById('textbook-download-btn').addEventListener('click', downloadSelectedTextbooks);
        document.getElementById('textbook-cancel-btn').addEventListener('click', cancelTextbookTask);
        document.getElementById('textbook-retry-btn').addEventListener('click', retryFailedTextbooks);
        textbookState.initialized = true;
        refreshTextbookPanel();
    } else if (!textbookState.options) {
        refreshTextbookPanel();
    }
}

async function refreshTextbookPanel() {
    textbookSetStatus('加载教材目录...');
    try {
        const response = await fetch('/api/wizard/options');
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '加载失败');
        textbookState.options = data;
        const stageSelect = document.getElementById('textbook-stage');
        const stages = data.stages || [];
        stageSelect.innerHTML = '';
        stages.forEach(stage => {
            const option = document.createElement('option');
            option.value = stage;
            option.textContent = stage;
            stageSelect.appendChild(option);
        });
        if (stages.includes('小学')) stageSelect.value = '小学';
        textbookSetStatus('');
        renderTextbookGrades();
        renderTextbookSubjects();
    } catch (error) {
        textbookSetStatus('✗ 加载教材目录失败: ' + (error.message || error), 'error');
    }
}

function renderTextbookGrades() {
    const stage = document.getElementById('textbook-stage').value;
    const gradeSelect = document.getElementById('textbook-grade');
    const gradeGroup = document.getElementById('textbook-grade-group');
    // 高中教材统一标「高中年级」（按必修/选择性必修分册，不按年级），
    // 年级筛选无区分度，隐藏以免"选了年级列表不变"的困惑
    if (stage.includes('高中')) {
        gradeGroup.style.display = 'none';
        gradeSelect.value = '';
        return;
    }
    gradeGroup.style.display = '';
    const grades = textbookState.options?.grades?.[stage] || [];
    const previous = gradeSelect.value;
    gradeSelect.innerHTML = '';
    grades.forEach(grade => {
        const option = document.createElement('option');
        option.value = grade;
        option.textContent = grade;
        gradeSelect.appendChild(option);
    });
    if (grades.includes(previous)) gradeSelect.value = previous;
}

function renderTextbookSubjects() {
    const stage = document.getElementById('textbook-stage').value;
    const grade = document.getElementById('textbook-grade').value;
    const container = document.getElementById('textbook-subjects');
    const subjects = textbookState.options?.study_subjects?.[stage] || [];
    // 保留已有勾选状态（切换学段/年级/刷新时不丢）
    const previous = {};
    container.querySelectorAll('.wizard-subject-row').forEach(row => {
        const cb = row.querySelector('input[type="checkbox"]');
        const sel = row.querySelector('select');
        if (cb && sel && sel.dataset.subject) {
            previous[sel.dataset.subject] = { checked: cb.checked, edition: sel.value };
        }
    });
    container.innerHTML = '';
    subjects.forEach(subject => {
        const row = document.createElement('div');
        row.className = 'wizard-subject-row';
        const label = document.createElement('label');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = subject;
        checkbox.checked = previous[subject]
            ? previous[subject].checked
            : ['语文', '数学', '英语'].includes(subject);
        checkbox.addEventListener('change', loadTextbookBooks);
        label.appendChild(checkbox);
        label.append(subject);
        row.appendChild(label);

        const select = document.createElement('select');
        select.className = 'wizard-subject-edition';
        select.dataset.subject = subject;
        select.title = `${subject} 教材版本`;
        const allEditions = textbookState.options?.textbook_editions?.[stage]?.[subject] || [];
        const editions = allEditions.filter(item => editionCoversGrade(item, grade));
        const usable = editions.length ? editions : allEditions;
        if (usable.length) {
            usable.forEach(edition => {
                const option = document.createElement('option');
                option.value = edition.name;
                option.textContent = edition.publisher
                    ? `${edition.name}（${edition.publisher}）` : edition.name;
                select.appendChild(option);
            });
            const preserved = previous[subject]?.edition;
            if (preserved && usable.some(item => item.name === preserved)) {
                select.value = preserved;
            } else {
                const fallback = usable.find(edition => edition.default) || usable[0];
                select.value = fallback.name;
            }
        } else {
            select.disabled = true;
            const option = document.createElement('option');
            option.value = '';
            option.textContent = '暂无教材';
            select.appendChild(option);
        }
        select.addEventListener('change', loadTextbookBooks);
        row.appendChild(select);
        container.appendChild(row);
    });
    loadTextbookBooks();
}

function textbookSelections() {
    const selections = [];
    document.querySelectorAll('#textbook-subjects .wizard-subject-row').forEach(row => {
        const cb = row.querySelector('input[type="checkbox"]');
        const sel = row.querySelector('select');
        if (cb && cb.checked && sel && sel.value) {
            selections.push({ subject: sel.dataset.subject, edition: sel.value });
        }
    });
    return selections;
}

async function loadTextbookBooks() {
    const stage = document.getElementById('textbook-stage').value;
    const grade = document.getElementById('textbook-grade').value;
    const selections = textbookSelections();
    if (!selections.length) {
        textbookState.books = [];
        renderTextbookBooks();
        return;
    }
    textbookState.loading = true;
    textbookSetButtons();
    try {
        const responses = await Promise.all(selections.map(({ subject, edition }) => {
            const params = new URLSearchParams({ stage, subject, edition, grade });
            return fetch('/api/textbooks/options?' + params.toString()).then(r => r.json());
        }));
        const seen = new Set();
        const books = [];
        responses.forEach(data => {
            (data.books || []).forEach(book => {
                if (seen.has(book.book_key)) return;
                seen.add(book.book_key);
                books.push(book);
            });
        });
        books.sort((a, b) =>
            (a.subject || '').localeCompare(b.subject || '', 'zh') ||
            (a.grade || '').localeCompare(b.grade || '', 'zh') ||
            (a.volume || '').localeCompare(b.volume || '', 'zh'));
        textbookState.books = books;
        renderTextbookBooks();
    } catch (error) {
        textbookSetStatus('✗ 加载教材列表失败: ' + (error.message || error), 'error');
    } finally {
        textbookState.loading = false;
        textbookSetButtons();
    }
}

function renderTextbookBooks() {
    const listEl = document.getElementById('textbook-list');
    if (!textbookState.books.length) {
        listEl.innerHTML = '<div class="textbook-empty">没有匹配的教材。可换学段/年级或调整科目版本。</div>';
        updateTextbookSummary();
        return;
    }
    listEl.innerHTML = '';
    textbookState.books.forEach(book => {
        const label = document.createElement('label');
        label.className = 'textbook-item';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = true;
        checkbox.dataset.bookKey = book.book_key;
        checkbox.addEventListener('change', updateTextbookSummary);
        const text = document.createElement('span');
        text.className = 'textbook-item-text';
        const sizeText = Number(book.total_size) > 0 ? ' · ' + textbookFormatSize(book.total_size) : '';
        text.textContent = `${book.title} · ${book.grade || ''}${book.semester || ''}${sizeText}`;
        label.appendChild(checkbox);
        label.appendChild(text);
        listEl.appendChild(label);
    });
    updateTextbookSummary();
}

function updateTextbookSummary() {
    const selected = textbookSelectedBooks();
    const totalSize = selected.reduce((sum, book) => sum + Number(book.total_size || 0), 0);
    const el = document.getElementById('textbook-summary');
    if (el) {
        const sizeText = totalSize > 0 ? '，共 ' + textbookFormatSize(totalSize) : '';
        el.textContent = textbookState.books.length
            ? '已选 ' + selected.length + ' 本' + sizeText + '（共匹配 ' + textbookState.books.length + ' 本）'
            : '';
    }
    textbookSetButtons(textbookState.taskRunning);
}

function isTextbookTaskRunning(status) {
    return status === 'queued' || status === 'running';
}

const TEXTBOOK_STATUS_TEXT = {
    running: '下载中',
    done: '已完成',
    partial_failed: '部分失败',
    failed: '失败',
    cancelled: '已取消',
};

async function downloadSelectedTextbooks() {
    const selected = textbookSelectedBooks();
    if (!selected.length) {
        textbookSetStatus('请先选择要下载的教材。', 'error');
        return;
    }
    const downloadBtn = document.getElementById('textbook-download-btn');
    downloadBtn.disabled = true;
    textbookSetStatus('创建下载任务...');
    try {
        const response = await fetch('/api/textbooks/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                workspace_uuid: currentWorkspace.uuid,
                book_keys: selected.map(book => book.book_key),
            }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '创建任务失败');
        startTextbookPolling(data.task_id);
    } catch (error) {
        downloadBtn.disabled = false;
        textbookSetStatus('✗ ' + error.message, 'error');
    }
}

function startTextbookPolling(taskId) {
    textbookState.taskId = taskId;
    textbookState.taskRunning = true;
    textbookSetButtons(true);
    const progress = document.getElementById('textbook-progress');
    progress.style.display = '';
    progress.value = 0;
    if (textbookState.pollTimer) clearInterval(textbookState.pollTimer);
    textbookState.pollTimer = setInterval(pollTextbookTask, 1000);
}

async function pollTextbookTask() {
    if (!textbookState.taskId) return;
    try {
        const response = await fetch('/api/textbooks/download/' + textbookState.taskId);
        const task = await response.json();
        if (!response.ok) throw new Error(task.detail || '查询失败');
        const percent = task.total_bytes
            ? Math.min(100, Math.round(task.downloaded_bytes / task.total_bytes * 100))
            : 0;
        document.getElementById('textbook-progress').value = percent;
        const failed = (task.books || []).filter(book => book.status === 'failed');
        const skipped = (task.books || []).filter(book => book.status === 'skipped');
        const done = (task.books || []).filter(book => book.status === 'done');
        textbookSetStatus(
            '下载状态：' + (TEXTBOOK_STATUS_TEXT[task.status] || task.status) +
            '，完成 ' + done.length + '，跳过 ' + skipped.length +
            '，失败 ' + failed.length + '；' + textbookFormatSize(task.downloaded_bytes) +
            ' / ' + textbookFormatSize(task.total_bytes) + '（' + percent + '%）' +
            (failed.length ? '；首个错误：' + failed[0].error : '')
        );
        if (!isTextbookTaskRunning(task.status)) {
            textbookState.taskRunning = false;
            stopTextbookPolling(null, task.status === 'done' ? 'success' : 'error');
            textbookSetButtons(false, failed.length > 0);
        } else {
            textbookSetButtons(true);
        }
    } catch (error) {
        textbookSetStatus('查询下载进度失败: ' + error.message, 'error');
    }
}

function stopTextbookPolling(message, type = '') {
    if (textbookState.pollTimer) {
        clearInterval(textbookState.pollTimer);
        textbookState.pollTimer = null;
    }
    if (message !== null) textbookSetStatus(message, type);
}

async function retryFailedTextbooks() {
    if (!textbookState.taskId) return;
    try {
        const response = await fetch('/api/textbooks/download/' + textbookState.taskId + '/retry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '重试失败');
        startTextbookPolling(data.task_id);
    } catch (error) {
        textbookSetStatus('✗ ' + error.message, 'error');
    }
}


async function cancelTextbookTask() {
    if (!textbookState.taskId) return;
    try {
        const response = await fetch('/api/textbooks/download/' + textbookState.taskId + '/cancel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '取消失败');
        textbookState.taskId = data.task_id;
        pollTextbookTask();
    } catch (error) {
        textbookSetStatus('✗ ' + error.message, 'error');
    }
}
