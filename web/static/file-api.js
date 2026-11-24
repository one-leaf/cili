// ── 文件 REST API 封装：创建/删除/重命名/上传（FileTree 等调用）──

// ── 文件管理器 API 封装 ──

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
