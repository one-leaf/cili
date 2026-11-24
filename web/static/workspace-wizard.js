// ── 新建工作区向导（分类 → 配置 → 目录 → 预览 → 创建）──
// 依赖 app.js: currentWorkspace, showToast, switchWorkspaceSettingsTab 等全局。

function joinPreviewPath(parent, child) {
    if (!parent) return child;
    if (/[\\/]$/.test(parent)) return parent + child;
    return parent + (parent.includes('\\') ? '\\' : '/') + child;
}

function updateWizardLocationHint() {
    if (!wizardState.options) return;
    const location = document.getElementById('wizard-location').value;
    const item = (wizardState.options.locations || []).find(entry => entry.id === location);
    const slug = document.getElementById('wizard-slug').value.trim() ||
        document.getElementById('wizard-name').value.trim();
    const parent = item?.parent || '';
    const customDir = document.getElementById('wizard-custom-directory').value.trim();
    const hint = document.getElementById('wizard-location-hint');
    if (hint) hint.textContent = location === 'custom' ? '' : `父目录：${parent}`;
    const customHint = document.getElementById('wizard-custom-hint');
    if (customHint) customHint.textContent = location === 'custom' ? '最终目录：自定义父目录 + 目录名' : '';
    const preview = document.getElementById('wizard-path-preview');
    if (preview) {
        preview.textContent = location === 'custom'
            ? '最终目录：' + (customDir ? (slug ? joinPreviewPath(customDir, slug) : customDir) : '(未填写)')
            : '最终目录：' + (parent ? (slug ? joinPreviewPath(parent, slug) : parent) : '(未知)');
    }
    const customGroup = document.getElementById('wizard-custom-group');
    if (customGroup) customGroup.style.display = location === 'custom' ? '' : 'none';
}

// ─── 新建工作区向导 ─────────────────────────────────────────────────────────

const wizardState = {
    step: 1,
    options: null,
    category: '',
    preview: null,
    creating: false,
    result: null,
};

function handleNewWorkspace() {
    document.getElementById('new-workspace-modal').style.display = 'flex';
    wizardState.step = 1;
    wizardState.category = '';
    wizardState.preview = null;
    wizardState.result = null;
    wizardState.creating = false;
    wizardState.options = null;
    document.getElementById('wizard-status').textContent = '';
    document.getElementById('wizard-result').innerHTML = '';
    document.getElementById('wizard-preview').innerHTML = '<div class="textbook-empty">点击下一步生成预览。</div>';
    document.getElementById('wizard-name').value = '';
    document.getElementById('wizard-slug').value = '';
    document.getElementById('wizard-custom-directory').value = '';
    document.getElementById('wizard-allow-existing').checked = false;
    document.getElementById('wizard-git-enabled').checked = true;
    document.getElementById('wizard-memory-enabled').checked = true;
    renderWizardCategories();
    loadWizardOptions();
    showWizardStep(1);
}

async function loadWizardOptions() {
    try {
        const response = await fetch('/api/wizard/options');
        if (!response.ok) throw new Error((await response.json()).detail || '加载向导选项失败');
        wizardState.options = await response.json();
        renderWizardOptions();
    } catch (error) {
        setWizardStatus('✗ ' + error.message, 'error');
    }
}

function applyWizardCategoryInputs() {
    const isStudy = wizardState.category === 'study';
    const nameInput = document.getElementById('wizard-name');
    const slugInput = document.getElementById('wizard-slug');
    if (nameInput) {
        nameInput.disabled = isStudy;
        nameInput.placeholder = isStudy ? '学习项目自动按「孩子称呼-科目」命名' : '输入项目名称（选填）';
    }
    if (slugInput) {
        slugInput.disabled = isStudy;
        slugInput.placeholder = isStudy ? '自动按「孩子称呼-科目」生成' : '默认按名称生成，选填';
    }
}

function renderWizardCategories() {
    applyWizardCategoryInputs();
    const categories = wizardState.options?.categories || [
        { id: 'study', name: '学习项目', description: '按科目拆分工作区。' },
        { id: 'office', name: '日常办公', description: '文档、表格、演示与归档。' },
        { id: 'research', name: '科研项目', description: '文献、实验与论文写作。' },
        { id: 'code', name: '编程项目', description: '选语言与项目类型；生成编码准则和工程约束。' },
        { id: 'blank', name: '空白项目', description: '只创建基础工作区。' },
    ];
    const grid = document.getElementById('wizard-categories');
    grid.innerHTML = '';
    categories.forEach(category => {
        const card = document.createElement('button');
        card.type = 'button';
        card.className = 'wizard-category-card' + (wizardState.category === category.id ? ' selected' : '');
        card.innerHTML = '<strong>' + escapeHtml(category.name) + '</strong><span>' + escapeHtml(category.description) + '</span>';
        card.addEventListener('click', () => {
            wizardState.category = category.id;
            wizardState.preview = null;
            renderWizardCategories();
            renderWizardConfig();
            showWizardStep(2);
        });
        grid.appendChild(card);
    });
}

function renderWizardOptions() {
    const options = wizardState.options;
    if (!options) return;
    const locationSelect = document.getElementById('wizard-location');
    const old = locationSelect.value;
    locationSelect.innerHTML = '';
    (options.locations || []).forEach(item => {
        const option = document.createElement('option');
        option.value = item.id;
        option.textContent = item.label;
        locationSelect.appendChild(option);
    });
    locationSelect.value = old || options.defaults?.location || 'cili';
    updateWizardLocationHint();

    const profile = collectWizardProfile(false);
    const stageSelect = document.getElementById('wizard-study-stage');
    if (stageSelect) {
        const oldStage = stageSelect.value || profile.stage;
        stageSelect.innerHTML = '<option value="">选择学段...</option>';
        (options.stages || []).forEach(stage => {
            const option = document.createElement('option');
            option.value = stage;
            option.textContent = stage;
            stageSelect.appendChild(option);
        });
        stageSelect.value = oldStage;
    }

    updateStudyGradeOptions();
}

function updateStudyGradeOptions() {
    const gradeSelect = document.getElementById('wizard-study-grade');
    if (!gradeSelect) return;
    const stage = document.getElementById('wizard-study-stage')?.value || '';
    const grades = (wizardState.options?.grades || {})[stage] || [];
    const old = gradeSelect.value;
    gradeSelect.innerHTML = '<option value="">选择年级...</option>';
    grades.forEach(grade => {
        const option = document.createElement('option');
        option.value = grade;
        option.textContent = grade;
        gradeSelect.appendChild(option);
    });
    gradeSelect.value = grades.includes(old) ? old : '';
}

function renderWizardConfig() {
    applyWizardCategoryInputs();
    const container = document.getElementById('wizard-category-config');
    const category = wizardState.category;
    if (!category) {
        container.innerHTML = '<div class="textbook-empty">请先选择项目类型。</div>';
        return;
    }
    if (category === 'study') {
        container.innerHTML = getStudyConfigHtml();
        bindStudyConfigEvents();
        return;
    }
    if (category === 'office') {
        container.innerHTML = getOfficeConfigHtml();
        return;
    }
    if (category === 'research') {
        container.innerHTML = getResearchConfigHtml();
        return;
    }
    if (category === 'code') {
        container.innerHTML = getCodeConfigHtml();
        bindCodeConfigEvents();
        return;
    }
    container.innerHTML = '<p>空白项目无需额外配置。</p>';
}

function getStudyConfigHtml() {
    return `
        <div class="wizard-grid">
            <div class="form-group"><label>孩子称呼</label><input type="text" id="wizard-study-learner" placeholder="用于对话称呼"></div>
            <div class="form-group"><label>学段</label><select id="wizard-study-stage"></select></div>
            <div class="form-group"><label>年级</label><select id="wizard-study-grade"></select></div>
            <div class="form-group"><label>考区</label><input type="text" id="wizard-study-region" placeholder="例如：广东"></div>
            <div class="form-group"><label>学习重点</label><select id="wizard-study-focus"><option>同步学习</option><option>中考复习</option><option>高考复习</option></select></div>
            <div class="form-group"><label>每天学习分钟数</label><input type="number" id="wizard-study-minutes" min="0" max="600" value="60"></div>
        </div>
        <div class="form-group"><label>家长备注</label><textarea id="wizard-study-note" rows="2" placeholder="孩子学习习惯、薄弱点等"></textarea></div>
        <div class="form-group"><label>科目（勾选后按科目选择教材版本）</label><div id="wizard-study-subjects" class="wizard-subject-list"></div></div>
        <label class="wizard-checkbox-row"><input type="checkbox" id="wizard-download-textbooks" checked> 自动下载匹配教材</label>
    `;
}

function getOfficeConfigHtml() {
    return `
        <div class="wizard-grid">
            <div class="form-group"><label>岗位/角色</label><input type="text" id="wizard-office-role" placeholder="例如：行政 / 项目经理"></div>
            <div class="form-group"><label>文档语言</label><select id="wizard-office-language"><option>中文</option><option>英文</option><option>中英双语</option></select></div>
        </div>
    `;
}

function getResearchConfigHtml() {
    return `
        <div class="wizard-grid">
            <div class="form-group"><label>学科</label><input type="text" id="wizard-research-field" placeholder="例如：计算机"></div>
            <div class="form-group"><label>研究方向</label><input type="text" id="wizard-research-direction"></div>
            <div class="form-group"><label>引用格式</label><select id="wizard-research-citation"><option>GB/T 7714</option><option>APA</option><option>MLA</option><option>Chicago</option><option>IEEE</option></select></div>
        </div>
    `;
}

const CODE_LANGUAGE_FALLBACK = [
    { id: 'python', name: 'Python' }, { id: 'ts', name: 'TypeScript / JavaScript' },
    { id: 'java', name: 'Java' }, { id: 'csharp', name: 'C# / .NET' },
    { id: 'go', name: 'Go' }, { id: 'rust', name: 'Rust' },
    { id: 'cpp', name: 'C / C++' }, { id: 'php', name: 'PHP' },
    { id: 'kotlin', name: 'Kotlin' }, { id: 'swift', name: 'Swift' },
    { id: 'other', name: '其他' },
];

const CODE_PROJECT_TYPE_FALLBACK = [
    { id: 'web-api', name: 'Web 后端（HTTP API）' }, { id: 'web-frontend', name: 'Web 前端' },
    { id: 'fullstack', name: '全栈 Web 应用' }, { id: 'cli', name: '命令行工具' },
    { id: 'desktop', name: '桌面应用' }, { id: 'mobile', name: '移动应用' },
    { id: 'data', name: '数据处理 / 脚本' }, { id: 'library', name: '库 / SDK' },
    { id: 'game', name: '游戏' }, { id: 'other', name: '其他 / 自定义' },
];

function getCodeConfigHtml() {
    return `
        <div class="wizard-grid">
            <div class="form-group"><label>开发语言</label><select id="wizard-code-language"></select></div>
            <div class="form-group"><label>项目类型</label><select id="wizard-code-project-type"></select></div>
        </div>
        <div class="form-group" id="wizard-code-language-custom-group" style="display:none;">
            <label>自定义语言名称</label><input type="text" id="wizard-code-language-custom" placeholder="例如：Dart">
        </div>
        <div class="form-group" id="wizard-code-type-custom-group" style="display:none;">
            <label>自定义项目类型</label><input type="text" id="wizard-code-type-custom" placeholder="例如：浏览器插件">
        </div>
    `;
}

function bindCodeConfigEvents() {
    const languageSelect = document.getElementById('wizard-code-language');
    const typeSelect = document.getElementById('wizard-code-project-type');
    const fill = (select, options, fallback) => {
        select.innerHTML = '';
        ((options && options.length) ? options : fallback).forEach(item => {
            const option = document.createElement('option');
            option.value = item.id;
            option.textContent = item.name;
            select.appendChild(option);
        });
    };
    fill(languageSelect, wizardState.options?.code_languages, CODE_LANGUAGE_FALLBACK);
    fill(typeSelect, wizardState.options?.code_project_types, CODE_PROJECT_TYPE_FALLBACK);
    const refreshCustom = () => {
        document.getElementById('wizard-code-language-custom-group').style.display =
            languageSelect.value === 'other' ? '' : 'none';
        document.getElementById('wizard-code-type-custom-group').style.display =
            typeSelect.value === 'other' ? '' : 'none';
    };
    languageSelect.addEventListener('change', refreshCustom);
    typeSelect.addEventListener('change', refreshCustom);
    refreshCustom();
}

function editionCoversGrade(item, grade) {
    if (!grade) return true;
    const grades = item.grades || [];
    if (!grades.length) return true;
    return grades.includes(grade);
}

function renderStudySubjectRows() {
    const stage = document.getElementById('wizard-study-stage')?.value || '';
    const grade = document.getElementById('wizard-study-grade')?.value || '';
    const subjects = wizardState.options?.study_subjects?.[stage] || [];
    const container = document.getElementById('wizard-study-subjects');
    container.innerHTML = '';
    subjects.forEach(subject => {
        const row = document.createElement('div');
        row.className = 'wizard-subject-row';
        const label = document.createElement('label');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = subject;
        checkbox.checked = ['语文', '数学', '英语'].includes(subject);
        checkbox.addEventListener('change', updateWizardSubjectHint);
        label.appendChild(checkbox);
        label.append(subject);
        row.appendChild(label);
        const select = document.createElement('select');
        select.className = 'wizard-subject-edition';
        select.dataset.subject = subject;
        select.title = `${subject} 教材版本`;
        const allEditions = wizardState.options?.textbook_editions?.[stage]?.[subject] || [];
        const editions = allEditions.filter(item => editionCoversGrade(item, grade));
        const usable = editions.length ? editions : allEditions;
        if (usable.length) {
            usable.forEach(edition => {
                const option = document.createElement('option');
                option.value = edition.name;
                option.textContent = edition.publisher ? `${edition.name}（${edition.publisher}）` : edition.name;
                select.appendChild(option);
            });
            const fallback = usable.find(edition => edition.default) || usable[0];
            select.value = fallback.name;
        } else {
            select.disabled = true;
            const option = document.createElement('option');
            option.value = '';
            option.textContent = '暂无教材';
            select.appendChild(option);
        }
        row.appendChild(select);
        container.appendChild(row);
    });
    updateWizardSubjectHint();
}

function bindStudyConfigEvents() {
    document.getElementById('wizard-study-stage')?.addEventListener('change', () => {
        renderStudySubjectRows();
        updateStudyGradeOptions();
    });
    document.getElementById('wizard-study-grade')?.addEventListener('change', renderStudySubjectRows);
    renderWizardOptions();
}

function updateWizardSubjectHint() {
    const checked = Array.from(document.querySelectorAll('#wizard-study-subjects input:checked')).map(x => x.value);
    setWizardStatus(checked.length ? `已选 ${checked.length} 个科目，将创建 ${checked.length} 个工作区。` : '请至少选择一个科目。');
}

function collectWizardProfile(strict = false) {
    const category = wizardState.category;
    if (category === 'study') {
        const subject_overrides = {};
        document.querySelectorAll('#wizard-study-subjects select.wizard-subject-edition').forEach(sel => {
            if (sel.dataset.subject && sel.value) {
                subject_overrides[sel.dataset.subject] = { textbook_edition: sel.value };
            }
        });
        const profile = {
            learner_name: document.getElementById('wizard-study-learner')?.value.trim() || '',
            stage: document.getElementById('wizard-study-stage')?.value || '',
            grade: document.getElementById('wizard-study-grade')?.value || '',
            exam_region: document.getElementById('wizard-study-region')?.value.trim() || '',
            focus: document.getElementById('wizard-study-focus')?.value || '同步学习',
            daily_minutes: Number(document.getElementById('wizard-study-minutes')?.value || 0),
            parent_note: document.getElementById('wizard-study-note')?.value.trim() || '',
            subject_overrides,
        };
        if (strict && !profile.learner_name) throw new Error('请填写孩子称呼');
        if (strict && !profile.stage) throw new Error('请选择学段');
        if (strict && !profile.grade) throw new Error('请选择年级');
        return profile;
    }
    if (category === 'office') {
        return {
            role: document.getElementById('wizard-office-role')?.value.trim() || '',
            doc_language: document.getElementById('wizard-office-language')?.value || '中文',
        };
    }
    if (category === 'research') {
        return {
            field: document.getElementById('wizard-research-field')?.value.trim() || '',
            direction: document.getElementById('wizard-research-direction')?.value.trim() || '',
            citation_style: document.getElementById('wizard-research-citation')?.value || 'GB/T 7714',
        };
    }
    if (category === 'code') {
        const languageValue = document.getElementById('wizard-code-language')?.value || '';
        const typeValue = document.getElementById('wizard-code-project-type')?.value || '';
        const language = languageValue === 'other'
            ? (document.getElementById('wizard-code-language-custom')?.value.trim() || 'other')
            : languageValue;
        const project_type = typeValue === 'other'
            ? (document.getElementById('wizard-code-type-custom')?.value.trim() || 'other')
            : typeValue;
        if (strict && !language) throw new Error('请选择开发语言');
        if (strict && !project_type) throw new Error('请选择项目类型');
        return { language, project_type };
    }
    return {};
}

function collectWizardSubjects() {
    return Array.from(document.querySelectorAll('#wizard-study-subjects input:checked')).map(x => x.value);
}

function setWizardStatus(message, type = '') {
    const el = document.getElementById('wizard-status');
    el.textContent = message;
    el.className = 'settings-status' + (type ? ' ' + type : '');
}

function showWizardStep(step) {
    wizardState.step = step;
    document.querySelectorAll('.wizard-step').forEach(item => {
        item.classList.toggle('active', item.id === `wizard-step-${step}`);
    });
    document.querySelectorAll('.wizard-step-indicator').forEach(item => {
        item.classList.toggle('active', Number(item.dataset.step) <= step);
        item.classList.toggle('current', Number(item.dataset.step) === step);
    });
    const back = document.getElementById('wizard-back-btn');
    const next = document.getElementById('wizard-next-btn');
    back.style.display = step === 1 ? 'none' : '';
    next.textContent = step === 4 ? '创建' : (step === 5 ? '完成' : '下一步');
    next.disabled = false;
}

function wizardGoBack() {
    if (wizardState.step > 1) showWizardStep(wizardState.step - 1);
}

async function workspaceNameExists(name) {
    try {
        const response = await fetch('/api/workspaces');
        if (!response.ok) return false;
        const data = await response.json();
        return (data.workspaces || []).some(ws => ws.name === name);
    } catch {
        return false; // 网络失败时放行，由后端预检兜底
    }
}

async function wizardGoNext() {
    try {
        if (wizardState.step === 1) {
            if (!wizardState.category) throw new Error('请选择项目类型');
            renderWizardConfig();
            showWizardStep(2);
            return;
        }
        if (wizardState.step === 2) {
            const name = document.getElementById('wizard-name').value.trim();
            if (name && wizardState.category !== 'study' && await workspaceNameExists(name)) {
                throw new Error(`已存在同名工作区「${name}」，请修改项目名称`);
            }
            renderWizardConfig();
            showWizardStep(3);
            return;
        }
        if (wizardState.step === 3) {
            collectWizardProfile(true);
            await previewWizard();
            showWizardStep(4);
            return;
        }
        if (wizardState.step === 4) {
            await createWizard();
            return;
        }
        if (wizardState.step === 5) {
            document.getElementById('new-workspace-modal').style.display = 'none';
            return;
        }
    } catch (error) {
        setWizardStatus('✗ ' + error.message, 'error');
    }
}

function wizardRequest() {
    return {
        category: wizardState.category,
        name: document.getElementById('wizard-name').value.trim(),
        location: document.getElementById('wizard-location').value,
        slug: document.getElementById('wizard-slug').value.trim(),
        custom_directory: document.getElementById('wizard-custom-directory').value.trim(),
        profile: collectWizardProfile(true),
        subjects: collectWizardSubjects(),
        allow_existing: document.getElementById('wizard-allow-existing').checked,
    };
}

function renderWizardPreview(data) {
    const container = document.getElementById('wizard-preview');
    container.innerHTML = '';
    const warnings = data.warnings || [];
    if (warnings.length) {
        const warning = document.createElement('div');
        warning.className = 'wizard-warning';
        warning.textContent = warnings.join('；');
        container.appendChild(warning);
    }
    data.workspaces.forEach(item => {
        const card = document.createElement('div');
        card.className = 'wizard-preview-card';
        const title = document.createElement('strong');
        title.textContent = item.name;
        const path = document.createElement('div');
        path.textContent = item.directory + (item.exists ? '（目录已存在）' : '');
        const pre = document.createElement('pre');
        pre.textContent = item.agents_md;
        card.append(title, path, pre);
        container.appendChild(card);
    });
}

async function previewWizard() {
    const next = document.getElementById('wizard-next-btn');
    next.disabled = true;
    setWizardStatus('生成预览...');
    try {
        const response = await fetch('/api/wizard/preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(wizardRequest()),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '生成预览失败');
        wizardState.preview = data;
        renderWizardPreview(data);
        setWizardStatus('预览已生成，确认后点击创建。');
    } finally {
        next.disabled = false;
    }
}

async function createWizard() {
    if (wizardState.creating) return;
    wizardState.creating = true;
    const next = document.getElementById('wizard-next-btn');
    next.disabled = true;
    setWizardStatus('正在创建...');
    try {
        const body = {
            ...wizardRequest(),
            overwrite_agents: false,
            git_enabled: document.getElementById('wizard-git-enabled').checked,
            memory_enabled: document.getElementById('wizard-memory-enabled').checked,
            download_textbooks: document.getElementById('wizard-download-textbooks')?.checked || false,
        };
        const response = await fetch('/api/wizard/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || '创建失败');
        wizardState.result = data;
        showWizardStep(5);
        renderWizardResult(data);
        setWizardStatus('✓ 创建完成', 'success');
        await loadWorkspaces();
    } catch (error) {
        setWizardStatus('✗ ' + error.message, 'error');
    } finally {
        wizardState.creating = false;
        next.disabled = false;
    }
}

function renderWizardResult(data) {
    const container = document.getElementById('wizard-result');
    container.innerHTML = '';
    (data.created || []).forEach(item => {
        const card = document.createElement('div');
        card.className = 'wizard-result-card success';
        card.innerHTML = '<strong>' + escapeHtml(item.name) + '</strong><div>' + escapeHtml(item.directory) + '</div>';
        if (item.subject) {
            const subject = document.createElement('div');
            subject.textContent = '科目：' + item.subject;
            card.appendChild(subject);
        }
        container.appendChild(card);
    });
    (data.failed || []).forEach(item => {
        const card = document.createElement('div');
        card.className = 'wizard-result-card error';
        card.innerHTML = '<strong>' + escapeHtml(item.name) + '</strong><div>' + escapeHtml(item.error) + '</div>';
        container.appendChild(card);
    });
    (data.textbook_tasks || []).forEach(task => {
        const card = document.createElement('div');
        card.className = 'wizard-result-card info';
        card.textContent = `已启动教材下载：${task.subject}，共 ${task.total_books} 本。`;
        container.appendChild(card);
    });
    (data.warnings || []).forEach(warning => {
        const card = document.createElement('div');
        card.className = 'wizard-warning';
        card.textContent = warning;
        container.appendChild(card);
    });
}

// Load sessions
