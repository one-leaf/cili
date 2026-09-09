// ── file-manager.js ── 文件管理相关逻辑
// 依赖 app.js 全局变量: currentWorkspace
// 依赖 utils.js 函数: getFileIcon, formatFileSize, showToast

// ── 文件管理器状态 ──
let fmCurrentPath = '';           // 当前路径（相对 workspace）
let fmSelectedFiles = new Set();  // 选中的文件路径集合
let fmPreviewFile = null;         // 当前预览的文件路径
let directoryBrowserCallback = null;  // 目录浏览器回调

async function browseDirectory(event) {
    // 找到触发事件的输入框
    const targetInput = event.target.parentElement.querySelector('input[type="text"]');
    if (!targetInput) return;

    // 打开目录浏览器弹窗
    const currentPath = targetInput.value || '';
    openDirectoryBrowser(currentPath, (selectedPath) => {
        targetInput.value = selectedPath;
    });
}

async function openDirectoryBrowser(initialPath, callback) {
    directoryBrowserCallback = callback;

    // 创建或获取弹窗
    let modal = document.getElementById('directory-browser-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'directory-browser-modal';
        modal.className = 'modal';
        modal.innerHTML = `
            <div class="modal-backdrop" onclick="closeDirectoryBrowser()"></div>
            <div class="modal-content" style="max-width: 500px;">
                <div class="modal-header">
                    <h2>选择目录</h2>
                    <button class="modal-close" onclick="closeDirectoryBrowser()">&times;</button>
                </div>
                <div class="modal-body">
                    <div class="dir-browser-path" id="dir-browser-current-path"></div>
                    <div class="dir-browser-list" id="dir-browser-list"></div>
                </div>
                <div class="modal-footer">
                    <button class="btn" onclick="closeDirectoryBrowser()">取消</button>
                    <button class="btn btn-primary" onclick="confirmDirectorySelection()">选择此目录</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }

    modal.style.display = 'flex';
    await loadDirectory(initialPath || '');
}

async function loadDirectory(path) {
    const listEl = document.getElementById('dir-browser-list');
    const pathEl = document.getElementById('dir-browser-current-path');

    listEl.innerHTML = '<div class="dir-browser-loading">加载中...</div>';
    pathEl.textContent = path || '选择位置';

    try {
        const response = await fetch(`/api/browse?path=${encodeURIComponent(path)}`);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();

        // 更新当前路径显示
        pathEl.textContent = data.path || '选择位置';
        pathEl.dataset.currentPath = data.path || '';

        // 渲染目录列表
        listEl.innerHTML = '';

        // 显示 fallback 提示（原路径不存在，已跳转到上级目录）
        if (data.fallback) {
            const hintItem = document.createElement('div');
            hintItem.className = 'dir-browser-item dir-browser-hint';
            hintItem.style.color = '#e67e22';
            hintItem.style.fontStyle = 'italic';
            hintItem.textContent = data.fallback;
            listEl.appendChild(hintItem);
        }

        // 添加父目录按钮（如果有）
        if (data.parent !== null && data.parent !== undefined) {
            const parentItem = document.createElement('div');
            parentItem.className = 'dir-browser-item dir-browser-parent';
            parentItem.innerHTML = `<span>⬆️ ..</span>`;
            parentItem.onclick = () => loadDirectory(data.parent);
            listEl.appendChild(parentItem);
        }

        // 添加目录列表
        if (data.directories.length === 0) {
            const emptyItem = document.createElement('div');
            emptyItem.className = 'dir-browser-item dir-browser-empty';
            emptyItem.textContent = '（空目录）';
            listEl.appendChild(emptyItem);
        } else {
            for (const dir of data.directories) {
                const item = document.createElement('div');
                item.className = 'dir-browser-item';
                // Windows 驱动器根目录显示为驱动器名，其他目录显示文件夹名
                const displayName = dir.name.match(/^[A-Z]:\\$/) ? dir.name : `📁 ${dir.name}`;
                item.textContent = displayName;
                item.onclick = () => loadDirectory(dir.path);
                listEl.appendChild(item);
            }
        }
    } catch (err) {
        listEl.innerHTML = `<div class="dir-browser-error">加载失败: ${escapeHtml(err.message)}</div>`;
    }
}

function confirmDirectorySelection() {
    const pathEl = document.getElementById('dir-browser-current-path');
    const selectedPath = pathEl.dataset.currentPath || '';

    if (selectedPath && directoryBrowserCallback) {
        directoryBrowserCallback(selectedPath);
    }

    closeDirectoryBrowser();
}

function closeDirectoryBrowser() {
    const modal = document.getElementById('directory-browser-modal');
    if (modal) {
        modal.style.display = 'none';
    }
    directoryBrowserCallback = null;
}

// File browser for inserting file paths

async function openFileBrowser() {
    if (!currentWorkspace) {
        alert('请先选择工作区');
        return;
    }

    // 创建或获取文件浏览弹窗
    let modal = document.getElementById('file-browser-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'file-browser-modal';
        modal.className = 'modal';
        modal.innerHTML = `
            <div class="modal-backdrop" onclick="closeFileBrowser()"></div>
            <div class="modal-content" style="max-width: 500px;">
                <div class="modal-header">
                    <h2>选择文件</h2>
                    <button class="modal-close" onclick="closeFileBrowser()">&times;</button>
                </div>
                <div class="modal-body">
                    <div class="file-browser-path" id="file-browser-current-path"></div>
                    <div class="file-browser-list" id="file-browser-list"></div>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }

    modal.style.display = 'flex';
    await loadWorkspaceFiles('');
}

async function loadWorkspaceFiles(path) {
    const listEl = document.getElementById('file-browser-list');
    const pathEl = document.getElementById('file-browser-current-path');

    listEl.innerHTML = '<div class="file-browser-loading">加载中...</div>';
    pathEl.textContent = path || '工作区根目录';

    try {
        const response = await fetch(`/api/files?workspace_uuid=${currentWorkspace.uuid}&path=${encodeURIComponent(path)}`);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();

        // 更新当前路径显示
        pathEl.textContent = data.path || '工作区根目录';

        // 渲染文件列表
        listEl.innerHTML = '';

        // 添加父目录按钮（如果有）
        if (data.parent !== null && data.parent !== undefined) {
            const parentItem = document.createElement('div');
            parentItem.className = 'file-browser-item file-browser-parent';
            parentItem.innerHTML = `<span>⬆️ ..</span>`;
            parentItem.onclick = () => loadWorkspaceFiles(data.parent);
            listEl.appendChild(parentItem);
        }

        // 添加文件和目录列表
        if (data.items.length === 0) {
            const emptyItem = document.createElement('div');
            emptyItem.className = 'file-browser-item file-browser-empty';
            emptyItem.textContent = '（空目录）';
            listEl.appendChild(emptyItem);
        } else {
            for (const item of data.items) {
                const el = document.createElement('div');
                el.className = 'file-browser-item';
                if (item.is_file) {
                    el.textContent = `📄 ${item.name}`;
                    el.onclick = () => insertFilePath(item.path);
                } else {
                    el.textContent = `📁 ${item.name}`;
                    el.onclick = () => loadWorkspaceFiles(item.path);
                }
                listEl.appendChild(el);
            }
        }
    } catch (err) {
        listEl.innerHTML = `<div class="file-browser-error">加载失败: ${escapeHtml(err.message)}</div>`;
    }
}

function insertFilePath(filePath) {
    const chatInput = document.getElementById('chat-input');
    const cursorPos = chatInput.selectionStart;
    const textBefore = chatInput.value.substring(0, cursorPos);
    const textAfter = chatInput.value.substring(chatInput.selectionEnd);

    // 插入文件路径，后面加一个空格方便继续输入
    chatInput.value = textBefore + filePath + ' ' + textAfter;

    // 移动光标到插入位置之后（跳过空格）
    const newPos = cursorPos + filePath.length + 1;
    chatInput.setSelectionRange(newPos, newPos);
    chatInput.focus();

    closeFileBrowser();
}

function closeFileBrowser() {
    const modal = document.getElementById('file-browser-modal');
    if (modal) {
        modal.style.display = 'none';
    }
}

// ── 文件管理器 API 封装 ──

// 辅助函数：自动注入 workspace_uuid
async function fetchFiles(path = '') {
    if (!currentWorkspace) throw new Error('未选择工作区');
    const url = new URL('/api/files', window.location.origin);
    url.searchParams.set('workspace_uuid', currentWorkspace.uuid);
    if (path) {
        url.searchParams.set('path', path);
    }
    const res = await fetch(url);
    if (!res.ok) throw new Error('加载文件失败');
    return res.json();
}

// 读取文件内容
async function fetchFileContent(path) {
    if (!currentWorkspace) throw new Error('未选择工作区');
    const url = `/api/files/${path}?workspace_uuid=${currentWorkspace.uuid}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error('读取文件失败');
    const contentType = res.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
        return { type: 'json', content: await res.json() };
    }
    return { type: 'text', content: await res.text() };
}

// 创建文件/文件夹
async function createFile(path, name, type, content = '') {
    if (!currentWorkspace) throw new Error('未选择工作区');
    const res = await fetch('/api/files', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            workspace_uuid: currentWorkspace.uuid,
            path, name, type, content
        })
    });
    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '创建失败');
    }
    return res.json();
}

// 删除文件/文件夹
async function deleteFiles(paths) {
    if (!currentWorkspace) throw new Error('未选择工作区');
    const res = await fetch('/api/files', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            workspace_uuid: currentWorkspace.uuid,
            paths
        })
    });
    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '删除失败');
    }
    return res.json();
}

// 重命名/移动文件
async function renameFile(oldPath, newPath) {
    if (!currentWorkspace) throw new Error('未选择工作区');
    const res = await fetch('/api/files', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            workspace_uuid: currentWorkspace.uuid,
            path: oldPath,
            new_path: newPath
        })
    });
    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '重命名失败');
    }
    return res.json();
}

// 保存文件内容
async function saveFileContent(path, content) {
    if (!currentWorkspace) throw new Error('未选择工作区');
    const res = await fetch('/api/files', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            workspace_uuid: currentWorkspace.uuid,
            path,
            content
        })
    });
    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '保存失败');
    }
    return res.json();
}

// 上传文件
async function uploadFiles(path, files) {
    if (!currentWorkspace) throw new Error('未选择工作区');
    const formData = new FormData();
    formData.append('workspace_uuid', currentWorkspace.uuid);
    formData.append('path', path);
    for (const file of files) {
        formData.append('files', file);
    }
    const res = await fetch('/api/files/upload', {
        method: 'POST',
        body: formData
    });
    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '上传失败');
    }
    return res.json();
}

// ── 文件管理器 UI 逻辑 ──

// 打开文件管理器
async function openFileManager() {
    if (!currentWorkspace) {
        alert('请先选择一个工作区');
        return;
    }
    fmCurrentPath = '';
    fmSelectedFiles.clear();
    fmPreviewFile = null;
    document.getElementById('file-manager-modal').style.display = 'flex';
    document.getElementById('file-preview-area').style.display = 'none';
    await loadFileManagerFiles('');
}

// 加载文件列表
async function loadFileManagerFiles(path) {
    try {
        const data = await fetchFiles(path);
        fmCurrentPath = data.path || '';
        renderFileManagerList(data.items || []);
        updateFmBreadcrumb(fmCurrentPath);
    } catch (error) {
        document.getElementById('file-manager-list').innerHTML = `
            <div class="fm-empty-state">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="12" cy="12" r="10"/>
                    <line x1="12" y1="8" x2="12" y2="12"/>
                    <line x1="12" y1="16" x2="12.01" y2="16"/>
                </svg>
                <span>${escapeHtml(error.message)}</span>
            </div>`;
    }
}

// 更新面包屑
function updateFmBreadcrumb(path) {
    const breadcrumb = document.getElementById('fm-breadcrumb');
    const parts = path ? path.split('/').filter(Boolean) : [];

    breadcrumb.innerHTML = '';
    const root = document.createElement('span');
    root.dataset.path = '';
    root.textContent = currentWorkspace.name;
    breadcrumb.appendChild(root);

    let currentParts = '';
    for (let i = 0; i < parts.length; i++) {
        currentParts += (currentParts ? '/' : '') + parts[i];
        const sep = document.createElement('span');
        sep.className = 'breadcrumb-sep';
        sep.textContent = '/';
        breadcrumb.appendChild(sep);
        const span = document.createElement('span');
        span.textContent = parts[i];
        if (i !== parts.length - 1) {
            span.dataset.path = currentParts;
        }
        breadcrumb.appendChild(span);
    }

    // 绑定点击事件
    breadcrumb.querySelectorAll('span[data-path]').forEach(span => {
        span.addEventListener('click', () => {
            loadFileManagerFiles(span.dataset.path);
        });
    });
}

// 渲染文件列表
function renderFileManagerList(items) {
    const list = document.getElementById('file-manager-list');

    if (items.length === 0) {
        list.innerHTML = `
            <div class="fm-empty-state">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                </svg>
                <span>空文件夹</span>
            </div>`;
        return;
    }

    // 排序：文件夹在前，然后按名称
    items.sort((a, b) => {
        if (a.is_file === b.is_file) {
            return a.name.localeCompare(b.name);
        }
        return a.is_file ? 1 : -1;
    });

    // 使用 DOM API 创建列表项：文件名/path 经 textContent / dataset 写入，
    // 不走 innerHTML，避免文件名包含 HTML 时触发 XSS
    list.innerHTML = '';
    items.forEach(item => {
        const el = document.createElement('div');
        el.className = 'fm-item';
        if (fmSelectedFiles.has(item.path)) el.classList.add('selected');
        el.dataset.path = item.path;
        el.dataset.isFile = item.is_file ? 'true' : 'false';

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'fm-checkbox';
        if (fmSelectedFiles.has(item.path)) checkbox.checked = true;

        const iconSpan = document.createElement('span');
        iconSpan.className = 'fm-item-icon';
        iconSpan.textContent = item.is_file ? getFileIcon(item.name) : '📁';

        const nameSpan = document.createElement('span');
        nameSpan.className = 'fm-item-name';
        nameSpan.textContent = item.name;

        const sizeSpan = document.createElement('span');
        sizeSpan.className = 'fm-item-size';
        sizeSpan.textContent = item.is_file ? formatFileSize(item.size) : '';

        const dateSpan = document.createElement('span');
        dateSpan.className = 'fm-item-date';
        dateSpan.textContent = item.modified ? new Date(item.modified * 1000).toLocaleString('zh-CN') : '';

        el.append(checkbox, iconSpan, nameSpan, sizeSpan, dateSpan);
        list.appendChild(el);
    });

    // 绑定事件
    list.querySelectorAll('.fm-item').forEach(el => {
        const path = el.dataset.path;
        const isFile = el.dataset.isFile === 'true';

        // 点击选中
        el.addEventListener('click', (e) => {
            if (e.target.classList.contains('fm-checkbox')) return;
            toggleFileSelection(path, e.ctrlKey || e.metaKey);
        });

        // checkbox 点击
        el.querySelector('.fm-checkbox').addEventListener('click', (e) => {
            e.stopPropagation();
            toggleFileSelection(path, true);
        });

        // 双击打开
        el.addEventListener('dblclick', () => {
            if (isFile) {
                openFilePreview(path);
            } else {
                loadFileManagerFiles(path);
            }
        });
    });
}

// 切换文件选择
function toggleFileSelection(path, multi = false) {
    if (multi) {
        if (fmSelectedFiles.has(path)) {
            fmSelectedFiles.delete(path);
        } else {
            fmSelectedFiles.add(path);
        }
    } else {
        fmSelectedFiles.clear();
        fmSelectedFiles.add(path);
    }
    renderFileManagerList(getCurrentFileItems());
}

// 获取当前列表中的文件项
function getCurrentFileItems() {
    const items = [];
    document.querySelectorAll('.fm-item').forEach(el => {
        items.push({
            path: el.dataset.path,
            is_file: el.dataset.isFile === 'true',
            name: el.querySelector('.fm-item-name').textContent
        });
    });
    return items;
}

// 打开文件预览
async function openFilePreview(path) {
    // 切换到预览模式
    document.getElementById('file-manager-list').classList.add('preview-mode');

    const previewArea = document.getElementById('file-preview-area');
    const previewContent = document.getElementById('file-preview-content');
    const previewImage = document.getElementById('file-preview-image');
    const previewPdf = document.getElementById('file-preview-pdf');
    const previewName = document.getElementById('file-preview-name');
    const downloadBtn = document.getElementById('file-preview-download-btn');

    try {
        fmPreviewFile = path;
        const filename = path.split('/').pop();
        previewName.textContent = filename;

        // 设置下载链接
        const fileUrl = `/api/files/${path}?workspace_uuid=${currentWorkspace.uuid}`;
        downloadBtn.href = fileUrl;
        downloadBtn.download = filename;

        // 隐藏所有预览元素
        previewContent.style.display = 'none';
        previewImage.style.display = 'none';
        previewPdf.style.display = 'none';

        const ext = filename.split('.').pop().toLowerCase();
        const imageExts = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'];
        const textExts = ['txt', 'md', 'js', 'css', 'java', 'py', 'json', 'xml', 'html', 'yaml', 'yml', 'sh', 'bat', 'log', 'ini', 'cfg'];

        if (imageExts.includes(ext)) {
            // 图片预览
            previewImage.src = fileUrl;
            previewImage.style.display = 'block';
        } else if (ext === 'pdf') {
            // PDF 预览
            previewPdf.src = fileUrl;
            previewPdf.style.display = 'block';
        } else if (textExts.includes(ext)) {
            // 文本文件预览
            const data = await fetchFileContent(path);
            previewContent.value = typeof data.content === 'string' ? data.content : JSON.stringify(data.content, null, 2);
            previewContent.style.display = 'block';
        } else {
            // 其他文件，显示提示
            previewContent.value = '此文件类型不支持预览，请下载后查看';
            previewContent.style.display = 'block';
        }

        previewArea.style.display = 'flex';
    } catch (error) {
        alert('打开文件失败：' + error.message);
    }
}

// 关闭预览
function closeFilePreview() {
    document.getElementById('file-manager-list').classList.remove('preview-mode');
    document.getElementById('file-preview-area').style.display = 'none';
    fmPreviewFile = null;
}

// 新建文件
async function fmCreateFile() {
    const name = prompt('请输入文件名：');
    if (!name) return;

    try {
        await createFile(fmCurrentPath, name, 'file', '');
        await loadFileManagerFiles(fmCurrentPath);
    } catch (error) {
        alert('创建失败：' + error.message);
    }
}

// 新建文件夹
async function fmCreateFolder() {
    const name = prompt('请输入文件夹名：');
    if (!name) return;

    try {
        await createFile(fmCurrentPath, name, 'folder');
        await loadFileManagerFiles(fmCurrentPath);
    } catch (error) {
        alert('创建失败：' + error.message);
    }
}

// 删除选中文件
async function fmDeleteSelected() {
    if (fmSelectedFiles.size === 0) {
        alert('请先选择要删除的文件');
        return;
    }

    if (!confirm(`确定要删除 ${fmSelectedFiles.size} 个项目吗？`)) {
        return;
    }

    try {
        await deleteFiles(Array.from(fmSelectedFiles));
        fmSelectedFiles.clear();
        await loadFileManagerFiles(fmCurrentPath);
    } catch (error) {
        alert('删除失败：' + error.message);
    }
}

// 重命名文件
async function fmRenameSelected() {
    if (fmSelectedFiles.size !== 1) {
        alert('请选择一个文件进行重命名');
        return;
    }

    const oldPath = Array.from(fmSelectedFiles)[0];
    const oldName = oldPath.split('/').pop();
    const newName = prompt('请输入新名称：', oldName);

    if (!newName || newName === oldName) return;

    const newPath = fmCurrentPath ? `${fmCurrentPath}/${newName}` : newName;

    try {
        await renameFile(oldPath, newPath);
        fmSelectedFiles.clear();
        await loadFileManagerFiles(fmCurrentPath);
    } catch (error) {
        alert('重命名失败：' + error.message);
    }
}

// 上传文件
async function fmUploadFiles(files) {
    if (!files || files.length === 0) return;

    try {
        await uploadFiles(fmCurrentPath, files);
        await loadFileManagerFiles(fmCurrentPath);
    } catch (error) {
        alert('上传失败：' + error.message);
    }
}

// 刷新文件列表
function fmRefresh() {
    loadFileManagerFiles(fmCurrentPath);
}

// 文件管理器事件绑定
function setupFileManagerEvents() {
    document.getElementById('file-manager-btn').addEventListener('click', openFileManager);
    document.getElementById('fm-new-file-btn').addEventListener('click', fmCreateFile);
    document.getElementById('fm-new-folder-btn').addEventListener('click', fmCreateFolder);
    document.getElementById('fm-upload-btn').addEventListener('click', () => {
        document.getElementById('fm-upload-input').click();
    });
    document.getElementById('fm-upload-input').addEventListener('change', (e) => {
        fmUploadFiles(e.target.files);
        e.target.value = '';  // 重置
    });
    document.getElementById('fm-delete-btn').addEventListener('click', fmDeleteSelected);
    document.getElementById('fm-rename-btn').addEventListener('click', fmRenameSelected);
    document.getElementById('fm-refresh-btn').addEventListener('click', fmRefresh);
    document.getElementById('file-preview-close-btn').addEventListener('click', closeFilePreview);
}


