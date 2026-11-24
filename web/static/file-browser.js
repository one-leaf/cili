// ── file-browser.js ── 目录浏览弹窗与文件插入浏览器（REST 封装见 file-api.js）
// 依赖 app.js 全局变量: currentWorkspace
// 依赖 utils.js 函数: getFileIcon, showToast, escapeHtml

// ── 文件管理器状态 ──
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
        const response = await fetch(`/api/fs/browse?path=${encodeURIComponent(path)}`);
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

// File browser for inserting file paths（支持浏览任意目录，不限于 workspace）

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
            <div class="modal-content file-browser-modal-content">
                <div class="modal-header">
                    <h2>添加附件</h2>
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
    // 默认从工作区目录开始，可随时导航到其他目录
    await loadFileBrowser(currentWorkspace.directory || '');
}

async function loadFileBrowser(path) {
    const listEl = document.getElementById('file-browser-list');
    const pathEl = document.getElementById('file-browser-current-path');

    listEl.innerHTML = '<div class="file-browser-loading">加载中...</div>';
    pathEl.textContent = path || '选择位置';

    try {
        const response = await fetch(`/api/fs/browse?path=${encodeURIComponent(path)}`);
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${response.status}`);
        }

        const data = await response.json();

        // 更新当前路径显示
        pathEl.textContent = data.path || '选择位置';

        // 渲染文件列表
        listEl.innerHTML = '';

        // 添加父目录按钮（如果有）
        if (data.parent !== null && data.parent !== undefined) {
            const parentItem = document.createElement('div');
            parentItem.className = 'file-browser-item file-browser-parent';
            parentItem.innerHTML = `<span>⬆️ ..</span>`;
            parentItem.onclick = () => loadFileBrowser(data.parent);
            listEl.appendChild(parentItem);
        }

        const directories = data.directories || [];
        const files = data.files || [];

        // 添加目录和文件列表
        if (directories.length === 0 && files.length === 0) {
            const emptyItem = document.createElement('div');
            emptyItem.className = 'file-browser-item file-browser-empty';
            emptyItem.textContent = '（空目录）';
            listEl.appendChild(emptyItem);
        } else {
            for (const dir of directories) {
                const el = document.createElement('div');
                el.className = 'file-browser-item';
                // Windows 驱动器根目录显示为驱动器名，其他目录显示文件夹名
                const displayName = dir.name.match(/^[A-Z]:\\$/) ? dir.name : `📁 ${dir.name}`;
                el.textContent = displayName;
                el.onclick = () => loadFileBrowser(dir.path);
                listEl.appendChild(el);
            }
            for (const file of files) {
                const el = document.createElement('div');
                el.className = 'file-browser-item';
                el.textContent = `📄 ${file.name}`;
                el.onclick = () => attachFilePath(file.path);
                listEl.appendChild(el);
            }
        }
    } catch (err) {
        listEl.innerHTML = `<div class="file-browser-error">加载失败: ${escapeHtml(err.message)}</div>`;
    }
}

// 工作区内的文件返回相对路径（/ 分隔，与拖放上传一致），工作区外返回绝对路径
function filePathToInsert(absPath) {
    const wsDir = currentWorkspace && currentWorkspace.directory;
    if (!wsDir) return absPath;
    const normAbs = absPath.replace(/\\/g, '/').toLowerCase();
    const normWs = wsDir.replace(/\\/g, '/').toLowerCase().replace(/\/+$/, '');
    if (normAbs === normWs) return '.';
    if (normAbs.startsWith(normWs + '/')) {
        return absPath.slice(wsDir.length).replace(/\\/g, '/').replace(/^\/+/, '');
    }
    return absPath;
}

// 从文件浏览器选择文件 → 加入输入区附件 chip（与 Ctrl+V/预览选择同一套逻辑）
// 工作区内用相对路径，工作区外保留绝对路径；读取权限由后端 outside.read 裁决
function attachFilePath(filePath) {
    const rel = filePathToInsert(filePath);
    if (!rel || rel === '.') return;
    if (pendingAttachments.some(a => a.path === rel)) {
        closeFileBrowser();
        return;
    }
    pendingAttachments.push({
        path: rel,
        name: rel.split(/[\\/]/).pop(),
        media_type: '',
    });
    renderComposerAttachments();
    updateComposerButtons();
    closeFileBrowser();
}

function closeFileBrowser() {
    const modal = document.getElementById('file-browser-modal');
    if (modal) {
        modal.style.display = 'none';
    }
}
