// ── file-tree.js ── 工作区文件树（VS Code 风格）+ 右侧只读预览面板
// 依赖 utils.js：getFileIcon / formatFileSize / renderMarkdown / showToast
// 由 app.js 在切换工作区后调用 FileTree.setWorkspace(currentWorkspace | null)

const FileTree = (() => {
    'use strict';

    const KEYS = {
        tree: 'cili_ft_state',
        filesHeight: 'cili_ft_files_height',
        filesCollapsed: 'cili_ft_files_collapsed',
        previewWidth: 'cili_preview_width',
        previewOpen: 'cili_preview_open',
        previewLast: 'cili_preview_last',
    };

    const INDENT = 14;
    const ROW_BASE = 8;
    const MAX_ROWS = 3000;
    const MAX_DIRS = 200;
    const MAX_EXPANDED = 100;
    const AUTO_REFRESH_MS = 400;
    const PREVIEW_DEFAULT_WIDTH = 420;
    const PREVIEW_MIN_WIDTH = 280;

    const MD_EXTS = new Set(['md', 'markdown']);
    const HTML_EXTS = new Set(['html', 'htm']);
    const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'svg']);
    const AUDIO_EXTS = new Set(['mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac']);
    const VIDEO_EXTS = new Set(['mp4', 'webm', 'ogv']);
    const OFFICE_EXTS = new Set([
        'doc', 'docx', 'docm', 'rtf', 'xls', 'xlsx', 'xlsm', 'xlsb',
        'ppt', 'pptx', 'pptm', 'ppsx', 'ppsm', 'potx',
    ]);
    const TEXT_EXTS = new Set([
        'txt', 'md', 'markdown', 'js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx', 'css', 'scss',
        'less', 'xml', 'json', 'jsonl', 'yaml', 'yml', 'toml', 'ini',
        'cfg', 'conf', 'log', 'py', 'pyw', 'java', 'kt', 'kts', 'c', 'cpp', 'h', 'hpp',
        'cs', 'go', 'rs', 'rb', 'php', 'swift', 'm', 'sh', 'bash', 'zsh', 'bat', 'cmd',
        'ps1', 'sql', 'vue', 'svelte', 'astro', 'tex', 'latex', 'csv', 'tsv',
        'env', 'gitignore', 'dockerfile', 'makefile', 'gradle', 'properties',
    ]);

    const state = {
        ws: null,
        token: 0,
        dirs: new Map(),          // dir -> { items, loading, error, loadedAt }
        inflight: new Map(),      // dir -> Promise
        expanded: new Set(),
        selected: null,
        focusPath: null,
        rows: [],
        restoreScroll: null,
        preview: {
            open: false,
            path: null,
            mtime: null,
            wrap: false,
            requestToken: 0,
            scrollTop: 0,
        },
    };

    let el = null;
    let initialized = false;
    let booted = false;
    let saveTimer = null;
    let refreshTimer = null;
    let previewScrollTimer = null;
    let treeScrollTimer = null;
    let ctxMenu = null;

    // ---------- 通用 ----------
    function normalizePath(value) {
        return String(value || '').replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
    }

    function basename(path) {
        const s = normalizePath(path);
        const i = s.lastIndexOf('/');
        return i === -1 ? s : s.slice(i + 1);
    }

    function extOf(name) {
        const i = name.lastIndexOf('.');
        return i <= 0 ? '' : name.slice(i + 1).toLowerCase();
    }

    function categoryOf(path) {
        const name = basename(path);
        const ext = extOf(name);
        if (MD_EXTS.has(ext)) return 'markdown';
        if (HTML_EXTS.has(ext)) return 'html';
        if (IMAGE_EXTS.has(ext)) return 'image';
        if (ext === 'pdf') return 'pdf';
        if (AUDIO_EXTS.has(ext)) return 'audio';
        if (VIDEO_EXTS.has(ext)) return 'video';
        if (OFFICE_EXTS.has(ext)) return 'office';
        if (TEXT_EXTS.has(ext)) return 'text';
        return 'probe';
    }

    function formatSize(bytes) {
        if (bytes === null || bytes === undefined) return '';
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
        return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
    }

    function clamp(value, min, max) {
        return Math.min(max, Math.max(min, value));
    }

    function readJSON(key, fallback) {
        try {
            const value = JSON.parse(localStorage.getItem(key) || 'null');
            return value === null ? fallback : value;
        } catch (e) {
            return fallback;
        }
    }

    function writeJSON(key, value) {
        try {
            localStorage.setItem(key, JSON.stringify(value));
        } catch (e) { /* localStorage 不可用则忽略 */ }
    }

    function readNum(key, fallback) {
        const value = parseInt(localStorage.getItem(key), 10);
        return Number.isFinite(value) ? value : fallback;
    }

    function toast(message) {
        if (typeof showToast === 'function') showToast(message);
    }

    function wsQuery() {
        return `workspace_uuid=${encodeURIComponent(state.ws?.uuid || '')}`;
    }

    function encodePath(path) {
        return normalizePath(path).split('/').map(encodeURIComponent).join('/');
    }

    function fileUrl(path, extra) {
        return `/api/files/${encodePath(path)}?${wsQuery()}${extra || ''}`;
    }

    function workspaceFileUrl(path, version) {
        const base = `/api/workspaces/${encodeURIComponent(state.ws?.uuid || '')}/files/${encodePath(path)}`;
        return version ? `${base}?v=${encodeURIComponent(version)}` : base;
    }

    function previewUrl(path) {
        return `/api/file-preview?${wsQuery()}&path=${encodeURIComponent(normalizePath(path))}`;
    }

    // ---------- 持久化 ----------
    function treeStore() {
        const all = readJSON(KEYS.tree, {});
        return all && typeof all === 'object' ? all : {};
    }

    function saveTreeState() {
        if (!state.ws) return;
        const all = treeStore();
        all[state.ws.uuid] = {
            expanded: Array.from(state.expanded).slice(-MAX_EXPANDED),
            selected: state.selected || null,
            scrollTop: el ? el.tree.scrollTop : 0,
        };
        writeJSON(KEYS.tree, all);
    }

    function scheduleSaveTreeState() {
        clearTimeout(saveTimer);
        saveTimer = setTimeout(saveTreeState, 300);
    }

    function savePreviewState() {
        try {
            localStorage.setItem(KEYS.previewOpen, state.preview.open ? '1' : '0');
        } catch (e) { /* ignore */ }
        if (!state.ws || !state.preview.path) return;
        const all = readJSON(KEYS.previewLast, {});
        const store = all && typeof all === 'object' ? all : {};
        store[state.ws.uuid] = {
            path: state.preview.path,
            scrollTop: state.preview.scrollTop,
            wrap: state.preview.wrap,
            mtime: state.preview.mtime,
        };
        writeJSON(KEYS.previewLast, store);
    }

    function loadTreeState() {
        const entry = treeStore()[state.ws.uuid] || {};
        state.expanded = new Set((entry.expanded || []).map(normalizePath).filter(Boolean));
        state.selected = entry.selected ? normalizePath(entry.selected) : null;
        state.focusPath = state.selected;
        state.restoreScroll = Number.isFinite(entry.scrollTop) ? entry.scrollTop : null;
    }

    // ---------- 数据层 ----------
    function normalizeItem(item) {
        return {
            name: item.name,
            path: normalizePath(item.path),
            is_file: !!item.is_file,
            size: item.size,
            modified: item.modified,
        };
    }

    function pruneDirs() {
        if (state.dirs.size <= MAX_DIRS) return;
        const removable = Array.from(state.dirs.keys())
            .filter((dir) => dir !== '' && !state.expanded.has(dir))
            .sort((a, b) => (state.dirs.get(a)?.loadedAt || 0) - (state.dirs.get(b)?.loadedAt || 0));
        while (state.dirs.size > MAX_DIRS && removable.length) {
            state.dirs.delete(removable.shift());
        }
    }

    async function loadDir(dir, opts = {}) {
        if (!state.ws) return null;
        const token = state.token;
        const current = state.dirs.get(dir) || {};
        if (!opts.force && current.items) return current.items;
        if (state.inflight.has(dir)) return state.inflight.get(dir);

        state.dirs.set(dir, { ...current, loading: true, error: null });
        render();

        const promise = (async () => {
            const res = await fetch(`/api/files?${wsQuery()}&path=${encodeURIComponent(dir)}`);
            if (!res.ok) {
                let detail = `HTTP ${res.status}`;
                try {
                    const data = await res.json();
                    if (data && data.detail) detail = data.detail;
                } catch (e) { /* ignore */ }
                throw new Error(detail);
            }
            const data = await res.json();
            if (token !== state.token) return null;
            const items = (data.items || []).map(normalizeItem);
            state.dirs.set(dir, { items, loadedAt: Date.now(), loading: false, error: null });
            pruneDirs();
            return items;
        })();

        state.inflight.set(dir, promise);
        try {
            return await promise;
        } catch (err) {
            if (token === state.token) {
                state.dirs.set(dir, {
                    ...(state.dirs.get(dir) || {}),
                    items: current.items || null,
                    loading: false,
                    error: err.message || '加载失败',
                });
            }
            return null;
        } finally {
            state.inflight.delete(dir);
            if (token === state.token) render();
        }
    }

    function findItem(path) {
        for (const entry of state.dirs.values()) {
            if (!entry.items) continue;
            for (const item of entry.items) {
                if (item.path === path) return item;
            }
        }
        return null;
    }

    function expandPath(path) {
        state.expanded.delete(path);
        state.expanded.add(path);
        while (state.expanded.size > MAX_EXPANDED) {
            state.expanded.delete(state.expanded.values().next().value);
        }
    }

    function toggleDir(path, forceOpen) {
        const isOpen = state.expanded.has(path);
        const next = forceOpen === undefined ? !isOpen : forceOpen;
        if (next === isOpen) return;
        if (next) {
            expandPath(path);
            if (!state.dirs.get(path)?.items) loadDir(path);
        } else {
            state.expanded.delete(path);
        }
        scheduleSaveTreeState();
        render();
    }

    function collapseTree(path) {
        if (path === undefined) {
            state.expanded.clear();
        } else {
            for (const dir of Array.from(state.expanded)) {
                if (dir.startsWith(path + '/')) state.expanded.delete(dir);
            }
        }
        scheduleSaveTreeState();
        render();
    }

    function restoreExpansion() {
        const dirs = Array.from(state.expanded).sort(
            (a, b) => a.split('/').length - b.split('/').length
        );
        for (const dir of dirs) {
            loadDir(dir);
        }
    }

    // ---------- 渲染层 ----------
    function buildRows() {
        const rows = [];

        const walk = (dir, level) => {
            if (rows.length >= MAX_ROWS) return;
            const entry = state.dirs.get(dir);
            if (!entry || !entry.items) return;
            for (const item of entry.items) {
                if (rows.length >= MAX_ROWS) return;
                rows.push({ item, level, type: item.is_file ? 'file' : 'dir' });
                if (!item.is_file && state.expanded.has(item.path)) {
                    const child = state.dirs.get(item.path);
                    if (child?.loading && !child.items) {
                        rows.push({ level: level + 1, dir: item.path, type: 'loading' });
                    } else if (child?.error && !child.items) {
                        rows.push({
                            level: level + 1,
                            dir: item.path,
                            type: 'error',
                            error: child.error,
                        });
                    } else {
                        walk(item.path, level + 1);
                    }
                }
            }
        };

        walk('', 0);

        if (rows.length === 0) {
            const root = state.dirs.get('');
            if (root?.loading) rows.push({ level: 0, dir: '', type: 'loading' });
            else if (root?.error) rows.push({ level: 0, dir: '', type: 'error', error: root.error });
        }
        return rows;
    }

    function emptyNode(text, cls) {
        const div = document.createElement('div');
        div.className = cls || 'file-tree-empty';
        div.textContent = text;
        return div;
    }

    function guideNode(index) {
        const span = document.createElement('span');
        span.className = 'ft-guide';
        span.style.left = `${ROW_BASE + index * INDENT + 7}px`;
        return span;
    }

    function renderRow(row, index) {
        const div = document.createElement('div');
        div.className = 'ft-row';
        div.id = `ft-row-${index}`;
        div.dataset.index = String(index);
        div.setAttribute('role', 'treeitem');
        div.setAttribute('aria-level', String(row.level + 1));
        div.style.paddingLeft = `${ROW_BASE + row.level * INDENT}px`;

        if (row.type === 'loading' || row.type === 'error') {
            div.classList.add('muted');
            for (let i = 0; i < row.level; i++) div.appendChild(guideNode(i));
            const name = document.createElement('span');
            name.className = 'ft-name';
            name.textContent = row.type === 'loading'
                ? '加载中…'
                : `加载失败：${row.error || '未知错误'}`;
            div.appendChild(name);
            if (row.type === 'error') {
                const retry = document.createElement('button');
                retry.type = 'button';
                retry.className = 'ft-retry';
                retry.textContent = '重试';
                retry.addEventListener('click', (event) => {
                    event.stopPropagation();
                    loadDir(row.dir, { force: true });
                });
                div.appendChild(retry);
            }
            return div;
        }

        const item = row.item;
        div.dataset.path = item.path;
        div.dataset.type = row.type;
        if (item.path === state.selected) div.classList.add('selected');
        if (item.path === state.focusPath) div.classList.add('focused');
        div.setAttribute('aria-selected', item.path === state.selected ? 'true' : 'false');
        if (row.type === 'dir') {
            const open = state.expanded.has(item.path);
            div.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (open) div.classList.add('expanded');
        }

        for (let i = 0; i < row.level; i++) div.appendChild(guideNode(i));

        const chevron = document.createElement('span');
        chevron.className = 'ft-chevron' + (row.type === 'dir' ? '' : ' spacer');
        chevron.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" '
            + 'stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>';
        div.appendChild(chevron);

        const icon = document.createElement('span');
        icon.className = 'ft-icon';
        if (row.type === 'dir') {
            icon.textContent = state.expanded.has(item.path) ? '📂' : '📁';
        } else {
            icon.textContent = typeof getFileIcon === 'function' ? getFileIcon(item.name) : '📄';
        }
        div.appendChild(icon);

        const name = document.createElement('span');
        name.className = 'ft-name';
        name.textContent = item.name;
        name.title = item.path;
        div.appendChild(name);

        return div;
    }

    function render() {
        if (!initialized || !el) return;
        const scrollTop = el.tree.scrollTop;
        state.rows = buildRows();
        el.tree.textContent = '';

        if (!state.ws) {
            el.tree.appendChild(emptyNode('选择一个工作区'));
        } else if (state.rows.length === 0) {
            const root = state.dirs.get('');
            if (root?.loading) el.tree.appendChild(emptyNode('加载中…'));
            else if (root?.error) el.tree.appendChild(emptyNode(`加载失败：${root.error}`));
            else if (root?.items) el.tree.appendChild(emptyNode('空目录'));
            else el.tree.appendChild(emptyNode(state.ws ? '加载中…' : '选择一个工作区'));
        } else {
            const frag = document.createDocumentFragment();
            state.rows.forEach((row, index) => frag.appendChild(renderRow(row, index)));
            el.tree.appendChild(frag);
        }

        updateStatus();
        updateAria();
        el.tree.scrollTop = state.restoreScroll !== null ? state.restoreScroll : scrollTop;
        state.restoreScroll = null;
    }

    function updateStatus() {
        if (!el) return;
        let text = '';
        if (state.ws && state.rows.length >= MAX_ROWS) {
            text = `仅显示前 ${MAX_ROWS} 项，请折叠部分目录后查看`;
        } else {
            const root = state.dirs.get('');
            if (root?.error && root?.items) text = `刷新失败：${root.error}`;
        }
        el.treeStatus.textContent = text;
        el.treeStatus.hidden = !text;
    }

    function updateAria() {
        if (!el) return;
        const index = state.rows.findIndex((row) => row.item && row.item.path === state.focusPath);
        if (index >= 0) el.tree.setAttribute('aria-activedescendant', `ft-row-${index}`);
        else el.tree.removeAttribute('aria-activedescendant');
    }

    function focusRow(path) {
        state.focusPath = path || null;
        render();
        const index = state.rows.findIndex((row) => row.item && row.item.path === path);
        if (index >= 0) {
            const node = document.getElementById(`ft-row-${index}`);
            if (node) node.scrollIntoView({ block: 'nearest' });
        }
    }

    function selectFile(path) {
        state.selected = path;
        state.focusPath = path;
        scheduleSaveTreeState();
    }

    // ---------- 树交互 ----------
    function rowFromEvent(target) {
        const rowEl = target.closest ? target.closest('.ft-row') : null;
        if (!rowEl || !el.tree.contains(rowEl)) return null;
        const index = parseInt(rowEl.dataset.index, 10);
        return Number.isFinite(index) ? state.rows[index] : null;
    }

    function onTreeClick(event) {
        const row = rowFromEvent(event.target);
        if (!row || !row.item) return;
        if (row.type === 'dir') {
            toggleDir(row.item.path);
            focusRow(row.item.path);
        } else {
            selectFile(row.item.path);
            openPreview(row.item.path);
        }
    }

    function onTreeContextMenu(event) {
        const row = rowFromEvent(event.target);
        if (!row || !row.item) return;
        event.preventDefault();
        showContextMenu(event.clientX, event.clientY, row.item);
    }

    function onTreeKeyDown(event) {
        if (!state.rows.length) return;
        const index = state.rows.findIndex((row) => row.item && row.item.path === state.focusPath);
        const row = index >= 0 ? state.rows[index] : null;

        switch (event.key) {
            case 'ArrowDown':
                event.preventDefault();
                moveFocus(1);
                break;
            case 'ArrowUp':
                event.preventDefault();
                moveFocus(-1);
                break;
            case 'Home':
                event.preventDefault();
                focusFirst();
                break;
            case 'End':
                event.preventDefault();
                focusLast();
                break;
            case 'ArrowRight':
                if (!row || !row.item) return;
                event.preventDefault();
                if (row.type === 'dir') {
                    if (!state.expanded.has(row.item.path)) toggleDir(row.item.path, true);
                    else focusFirstChild(index, row);
                }
                break;
            case 'ArrowLeft':
                if (!row || !row.item) return;
                event.preventDefault();
                if (row.type === 'dir' && state.expanded.has(row.item.path)) {
                    toggleDir(row.item.path, false);
                } else {
                    focusParent(row);
                }
                break;
            case 'Enter':
                if (!row || !row.item) return;
                event.preventDefault();
                if (row.type === 'dir') toggleDir(row.item.path);
                else {
                    selectFile(row.item.path);
                    openPreview(row.item.path);
                }
                break;
            case ' ':
                if (!row || row.type !== 'dir') return;
                event.preventDefault();
                toggleDir(row.item.path);
                break;
            case 'Escape':
                state.focusPath = null;
                render();
                break;
            default:
                break;
        }
    }

    function moveFocus(delta) {
        const indexes = state.rows.map((row, i) => (row.item ? i : -1)).filter((i) => i >= 0);
        if (!indexes.length) return;
        const current = state.rows.findIndex((row) => row.item && row.item.path === state.focusPath);
        let pos = indexes.indexOf(current);
        if (pos === -1) pos = delta > 0 ? -1 : indexes.length;
        pos = clamp(pos + delta, 0, indexes.length - 1);
        focusRow(state.rows[indexes[pos]].item.path);
    }

    function focusFirst() {
        const index = state.rows.findIndex((row) => row.item);
        if (index >= 0) focusRow(state.rows[index].item.path);
    }

    function focusLast() {
        for (let i = state.rows.length - 1; i >= 0; i--) {
            if (state.rows[i].item) {
                focusRow(state.rows[i].item.path);
                return;
            }
        }
    }

    function focusFirstChild(index, row) {
        for (let i = index + 1; i < state.rows.length; i++) {
            const next = state.rows[i];
            if (next.level <= row.level) break;
            if (next.item && next.level === row.level + 1) {
                focusRow(next.item.path);
                return;
            }
        }
    }

    function focusParent(row) {
        const path = row.item?.path || '';
        const cut = path.lastIndexOf('/');
        if (cut === -1) return;
        focusRow(path.slice(0, cut));
    }

    // ---------- 右键菜单 ----------
    function closeContextMenu() {
        if (ctxMenu) {
            ctxMenu.remove();
            ctxMenu = null;
        }
    }

    function copyPath(path) {
        const text = normalizePath(path);
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(
                () => toast('已复制路径'),
                () => toast('复制失败')
            );
            return;
        }
        const area = document.createElement('textarea');
        area.value = text;
        document.body.appendChild(area);
        area.select();
        try {
            document.execCommand('copy');
            toast('已复制路径');
        } catch (e) {
            toast('复制失败');
        }
        area.remove();
    }

    async function openInExplorer(path) {
        if (!state.ws) return;
        try {
            const res = await fetch('/api/open-in-explorer', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    workspace_uuid: state.ws.uuid,
                    path: normalizePath(path),
                }),
            });
            if (!res.ok) {
                let detail = `HTTP ${res.status}`;
                try {
                    const data = await res.json();
                    if (data && data.detail) detail = data.detail;
                } catch (e) { /* ignore */ }
                throw new Error(detail);
            }
            toast('已在资源管理器中打开');
        } catch (err) {
            toast('打开资源管理器失败：' + (err.message || '未知错误'));
        }
    }

    function parentOf(path) {
        const clean = normalizePath(path);
        const cut = clean.lastIndexOf('/');
        return cut === -1 ? '' : clean.slice(0, cut);
    }

    async function ensureExpanded(dir) {
        if (!dir || !state.ws) return;
        if (!state.expanded.has(dir)) {
            state.expanded.add(dir);
            scheduleSaveTreeState();
        }
        if (!state.dirs.get(dir)?.items) await loadDir(dir);
    }

    function remapPaths(oldPath, newPath) {
        const map = (p) => {
            if (p === oldPath) return newPath;
            if (p.startsWith(oldPath + '/')) return newPath + p.slice(oldPath.length);
            return p;
        };
        state.expanded = new Set(Array.from(state.expanded, map));
        if (state.selected) state.selected = map(state.selected);
        if (state.focusPath) state.focusPath = map(state.focusPath);
        for (const key of Array.from(state.dirs.keys())) {
            if (key === oldPath || key.startsWith(oldPath + '/')) state.dirs.delete(key);
        }
        if (state.preview.path) {
            const mapped = map(state.preview.path);
            if (mapped !== state.preview.path) {
                state.preview.path = mapped;
                state.preview.requestToken += 1;
                renderPreviewHeader(findItem(mapped));
                reloadPreview({ preserveScroll: true });
            }
        }
        restoreExpansion();
        scheduleSaveTreeState();
    }

    function forgetPaths(path) {
        const inside = (p) => p === path || p.startsWith(path + '/');
        state.expanded = new Set(Array.from(state.expanded).filter((p) => !inside(p)));
        if (state.selected && inside(state.selected)) state.selected = null;
        if (state.focusPath && inside(state.focusPath)) state.focusPath = null;
        for (const key of Array.from(state.dirs.keys())) {
            if (inside(key)) state.dirs.delete(key);
        }
        if (state.preview.path && inside(state.preview.path)) {
            state.preview.path = null;
            closePreview();
        }
        scheduleSaveTreeState();
    }

    function validEntryName(name) {
        if (!name) return false;
        const backslash = String.fromCharCode(92);
        return !name.includes('/') && !name.includes(backslash);
    }

    async function createFileIn(dir) {
        const raw = prompt('请输入文件名：');
        const name = (raw || '').trim();
        if (!name) return;
        if (!validEntryName(name)) {
            toast('名称不能包含路径分隔符');
            return;
        }
        try {
            await createFile(dir, name, 'file', '');
            await ensureExpanded(dir);
            await refreshTree();
            toast('已创建文件');
        } catch (err) {
            toast('创建失败：' + (err.message || '未知错误'));
        }
    }

    async function createFolderIn(dir) {
        const raw = prompt('请输入文件夹名：');
        const name = (raw || '').trim();
        if (!name) return;
        if (!validEntryName(name)) {
            toast('名称不能包含路径分隔符');
            return;
        }
        try {
            await createFile(dir, name, 'folder');
            await ensureExpanded(dir);
            await refreshTree();
            toast('已创建文件夹');
        } catch (err) {
            toast('创建失败：' + (err.message || '未知错误'));
        }
    }

    async function renameEntry(path) {
        if (!state.ws) return;
        const oldName = basename(path);
        const raw = prompt('请输入新名称：', oldName);
        const newName = (raw || '').trim();
        if (!newName || newName === oldName) return;
        if (!validEntryName(newName)) {
            toast('名称不能包含路径分隔符');
            return;
        }
        const parent = parentOf(path);
        const newPath = parent ? `${parent}/${newName}` : newName;
        try {
            await renameFile(path, newPath);
            remapPaths(path, newPath);
            await refreshTree();
            toast('已重命名');
        } catch (err) {
            toast('重命名失败：' + (err.message || '未知错误'));
        }
    }

    async function deleteEntry(path, isDir) {
        if (!state.ws) return;
        const name = basename(path);
        const message = isDir
            ? `确定删除文件夹「${name}」及其全部内容吗？`
            : `确定删除「${name}」吗？`;
        if (!confirm(message)) return;
        try {
            const result = await deleteFiles([path]);
            const errors = result && result.errors ? result.errors : [];
            if (errors.length) {
                toast('删除失败：' + (errors[0].error || '未知错误'));
                return;
            }
            forgetPaths(path);
            await refreshTree();
            toast('已删除');
        } catch (err) {
            toast('删除失败：' + (err.message || '未知错误'));
        }
    }

    function pickFilesForUpload(dir) {
        if (!state.ws) return;
        const input = document.createElement('input');
        input.type = 'file';
        input.multiple = true;
        input.style.display = 'none';
        document.body.appendChild(input);
        const cleanup = () => input.remove();
        input.addEventListener('cancel', cleanup);
        input.addEventListener('change', async () => {
            const files = Array.from(input.files || []);
            cleanup();
            if (!files.length) return;
            try {
                const result = await uploadFiles(dir, files);
                const errors = result && result.errors ? result.errors : [];
                if (errors.length) {
                    toast('部分文件上传失败：' + (errors[0].error || '未知错误'));
                } else {
                    toast(`已上传 ${files.length} 个文件`);
                }
                await ensureExpanded(dir);
                await refreshTree();
            } catch (err) {
                toast('上传失败：' + (err.message || '未知错误'));
            }
        });
        input.click();
    }

    function showContextMenu(x, y, item) {
        closeContextMenu();
        const menu = document.createElement('div');
        menu.className = 'ft-context-menu';

        const entries = item.is_file
            ? [
                ['预览', () => openPreview(item.path)],
                ['下载', () => window.open(fileUrl(item.path), '_blank', 'noopener')],
                ['-', null],
                ['在文件资源管理器中显示', () => openInExplorer(item.path)],
                ['复制相对路径', () => copyPath(item.path)],
                ['-', null],
                ['重命名', () => renameEntry(item.path)],
                ['删除', () => deleteEntry(item.path, false)],
            ]
            : [
                [state.expanded.has(item.path) ? '折叠' : '展开', () => toggleDir(item.path)],
                ['全部折叠', () => collapseTree(item.path)],
                ['-', null],
                ['新建文件', () => createFileIn(item.path)],
                ['新建文件夹', () => createFolderIn(item.path)],
                ['上传', () => pickFilesForUpload(item.path)],
                ['-', null],
                ['在文件资源管理器中显示', () => openInExplorer(item.path)],
                ['复制相对路径', () => copyPath(item.path)],
                ['-', null],
                ['重命名', () => renameEntry(item.path)],
                ['删除', () => deleteEntry(item.path, true)],
            ];

        for (const [label, action] of entries) {
            if (label === '-') {
                const sep = document.createElement('div');
                sep.className = 'ft-context-sep';
                menu.appendChild(sep);
                continue;
            }
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'ft-context-item';
            if (label === '删除') btn.classList.add('danger');
            btn.textContent = label;
            btn.addEventListener('click', () => {
                closeContextMenu();
                action();
            });
            menu.appendChild(btn);
        }

        document.body.appendChild(menu);
        const rect = menu.getBoundingClientRect();
        menu.style.left = `${Math.min(x, window.innerWidth - rect.width - 8)}px`;
        menu.style.top = `${Math.min(y, window.innerHeight - rect.height - 8)}px`;
        ctxMenu = menu;
    }

    // ---------- 预览面板 ----------
    function isMaximized() {
        return !!el && el.pane.classList.contains('maximized');
    }

    function setMaximized(value) {
        if (!el) return;
        const on = !!value;
        el.pane.classList.toggle('maximized', on);
        const container = document.querySelector('.container');
        if (container) container.classList.toggle('preview-maximized', on);
        if (el.maximizeBtn) {
            el.maximizeBtn.title = on ? '还原预览' : '最大化预览';
            el.maximizeBtn.classList.toggle('active', on);
            el.maximizeBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
        }
        el.previewHandle.hidden = on || !state.preview.open;
        if (!on) applyPreviewWidth();
    }

    function openPane() {
        if (state.preview.open) return;
        state.preview.open = true;
        el.pane.hidden = false;
        el.previewHandle.hidden = false;
        applyPreviewWidth();
        savePreviewState();
    }

    function closePreview() {
        if (!state.preview.open) return;
        state.preview.open = false;
        state.preview.requestToken += 1;
        setMaximized(false);
        el.pane.hidden = true;
        el.previewHandle.hidden = true;
        el.body.textContent = '';
        savePreviewState();
    }

    function openPreview(path, opts = {}) {
        if (!state.ws) return;
        const clean = normalizePath(path);
        if (!clean) return;
        const item = findItem(clean);
        if (item && !item.is_file) return;

        selectFile(clean);
        openPane();

        const changed = state.preview.path !== clean;
        state.preview.path = clean;
        state.preview.mtime = item?.modified ?? null;
        state.preview.requestToken += 1;
        if (changed && !opts.restore) state.preview.scrollTop = 0;
        savePreviewState();

        render();
        renderPreviewHeader(item);
        renderPreviewBody();
    }

    function renderPreviewHeader(item) {
        const name = basename(state.preview.path);
        el.previewIcon.textContent = typeof getFileIcon === 'function' ? getFileIcon(name) : '📄';
        el.previewName.textContent = name;
        el.previewName.title = name;
        const meta = [state.preview.path];
        if (item?.size !== null && item?.size !== undefined) meta.push(formatSize(item.size));
        el.previewPath.textContent = meta.join(' · ');
        el.downloadBtn.href = fileUrl(state.preview.path);
        el.downloadBtn.download = name;

        const cat = categoryOf(state.preview.path);
        el.wrapBtn.hidden = !(cat === 'text' || cat === 'probe');
        el.wrapBtn.classList.toggle('active', state.preview.wrap);
    }

    function renderPreviewBody() {
        if (!state.preview.path) return;
        const path = state.preview.path;
        const cat = categoryOf(path);

        el.body.classList.remove('media', 'pdf', 'office');
        el.body.textContent = '';
        el.previewStatus.textContent = '';
        el.previewStatus.hidden = true;

        if (cat === 'image') {
            renderImage(path);
        } else if (cat === 'pdf') {
            renderPdf(path);
        } else if (cat === 'html') {
            renderHtml(path);
        } else if (cat === 'audio' || cat === 'video') {
            renderAv(path, cat);
        } else if (cat === 'office') {
            renderOffice(path);
        } else if (cat === 'unsupported') {
            el.body.classList.remove('media');
            renderUnsupported(path);
        } else {
            el.body.classList.remove('media');
            loadTextPreview(path);
        }
    }

    function mediaUrl(path) {
        const version = encodeURIComponent(state.preview.mtime || Date.now());
        return `${fileUrl(path)}&v=${version}`;
    }

    function renderImage(path) {
        el.body.classList.add('media');
        const wrap = document.createElement('div');
        wrap.className = 'preview-media';
        const link = document.createElement('a');
        link.href = fileUrl(path);
        link.target = '_blank';
        link.rel = 'noopener';
        const img = document.createElement('img');
        img.src = mediaUrl(path);
        img.alt = basename(path);
        link.appendChild(img);
        wrap.appendChild(link);
        el.body.appendChild(wrap);
        restorePreviewScroll();
    }

    function pdfAssetUrl(name) {
        return `/static/libs/pdfjs/${name}`;
    }

    function renderOffice(path) {
        el.body.classList.remove('media', 'pdf');
        el.body.classList.add('office');
        el.body.textContent = '';

        const loading = document.createElement('div');
        loading.className = 'office-loading';
        const spinner = document.createElement('div');
        spinner.className = 'office-spinner';
        const text = document.createElement('div');
        text.className = 'office-loading-text';
        text.textContent = `正在转换 ${basename(path)}…`;
        const progress = document.createElement('div');
        progress.className = 'office-progress';
        loading.appendChild(spinner);
        loading.appendChild(text);
        loading.appendChild(progress);
        el.body.appendChild(loading);

        const token = state.preview.requestToken;
        const version = state.preview.mtime || Date.now();
        const statusUrl = `/api/office-preview/status?${wsQuery()}`
            + `&path=${encodeURIComponent(normalizePath(path))}`
            + `&v=${encodeURIComponent(version)}`;

        fetch(statusUrl)
            .then(async (res) => {
                if (!res.ok) {
                    let detail = `HTTP ${res.status}`;
                    try {
                        const data = await res.json();
                        if (data && data.detail) detail = data.detail;
                    } catch (e) { /* ignore */ }
                    throw new Error(detail);
                }
                return res.json();
            })
            .then((data) => {
                if (token !== state.preview.requestToken || state.preview.path !== path) return;
                const fileAbs = new URL(data.pdf_url, window.location.href).href;
                const frame = document.createElement('iframe');
                frame.className = 'preview-pdf';
                frame.title = basename(path);
                frame.src = `${pdfAssetUrl('web/viewer.html')}?file=${encodeURIComponent(fileAbs)}&locale=zh-CN`;
                el.body.classList.remove('office');
                el.body.classList.add('media');
                el.body.textContent = '';
                el.body.appendChild(frame);
            })
            .catch((err) => {
                if (token !== state.preview.requestToken || state.preview.path !== path) return;
                el.body.classList.remove('office');
                renderPreviewError(err.message || 'Office 文档转换失败');
            });
    }

    function renderPdf(path) {
        el.body.classList.remove('pdf');
        el.body.classList.add('media');
        el.body.textContent = '';

        const fileAbs = new URL(fileUrl(path), window.location.href).href;
        const frame = document.createElement('iframe');
        frame.className = 'preview-pdf';
        frame.title = basename(path);
        frame.src = `${pdfAssetUrl('web/viewer.html')}?file=${encodeURIComponent(fileAbs)}&locale=zh-CN`;
        el.body.appendChild(frame);
    }

    function renderHtml(path) {
        el.body.classList.add('media');
        const frame = document.createElement('iframe');
        frame.className = 'preview-html';
        frame.title = basename(path);
        frame.src = workspaceFileUrl(path, state.preview.mtime);
        // 隔离渲染：脚本可运行，但处于 opaque origin，拿不到父页面/同源存储；
        // 表单、弹窗与顶层导航均被 sandbox 禁用。
        frame.setAttribute('sandbox', 'allow-scripts');
        frame.referrerPolicy = 'no-referrer';
        el.body.appendChild(frame);
    }

    function renderAv(path, kind) {
        el.body.classList.add('media');
        const wrap = document.createElement('div');
        wrap.className = 'preview-av';
        const media = document.createElement(kind === 'audio' ? 'audio' : 'video');
        media.controls = true;
        media.preload = 'metadata';
        media.src = mediaUrl(path);
        wrap.appendChild(media);
        el.body.appendChild(wrap);
    }

    function renderUnsupported(path, reason) {
        el.body.classList.remove('media');
        el.body.textContent = '';
        const box = document.createElement('div');
        box.className = 'preview-empty';
        const icon = document.createElement('div');
        icon.className = 'preview-big-icon';
        icon.textContent = '📄';
        const title = document.createElement('div');
        title.textContent = reason || '此文件类型不支持预览';
        const meta = document.createElement('div');
        const item = findItem(path);
        meta.className = 'preview-meta';
        meta.textContent = item?.size !== null && item?.size !== undefined
            ? `${path} · ${formatSize(item.size)}`
            : path;
        const link = document.createElement('a');
        link.className = 'btn btn-small';
        link.href = fileUrl(path);
        link.download = basename(path);
        link.textContent = '下载文件';
        box.appendChild(icon);
        box.appendChild(title);
        box.appendChild(meta);
        box.appendChild(link);
        el.body.appendChild(box);
    }

    async function loadTextPreview(path) {
        const token = ++state.preview.requestToken;
        el.body.textContent = '';
        const loading = document.createElement('div');
        loading.className = 'preview-empty';
        loading.textContent = '加载中…';
        el.body.appendChild(loading);

        try {
            const res = await fetch(previewUrl(path));
            if (!res.ok) {
                let detail = `HTTP ${res.status}`;
                try {
                    const data = await res.json();
                    if (data && data.detail) detail = data.detail;
                } catch (e) { /* ignore */ }
                throw new Error(detail);
            }
            const data = await res.json();
            if (token !== state.preview.requestToken || state.preview.path !== path) return;
            state.preview.mtime = data.mtime;
            renderTextData(data, path);
        } catch (err) {
            if (token !== state.preview.requestToken) return;
            renderPreviewError(err.message || '加载失败');
        }
    }

    function renderTextData(data, path) {
        el.body.textContent = '';
        el.body.classList.remove('media');

        if (data.kind === 'binary') {
            el.wrapBtn.hidden = true;
            renderUnsupported(path, '二进制文件不支持预览');
            return;
        }

        const meta = [
            path,
            formatSize(data.size ?? 0),
            String(data.encoding || 'utf-8').toUpperCase(),
        ];
        el.previewPath.textContent = meta.join(' · ');

        if (data.truncated) {
            const notice = document.createElement('div');
            notice.className = 'preview-notice';
            notice.textContent = `文件较大（${formatSize(data.size ?? 0)}），仅显示前 512 KB。下载查看完整内容。`;
            el.body.appendChild(notice);
        }

        if (!data.content) {
            const empty = document.createElement('div');
            empty.className = 'preview-empty';
            empty.textContent = '空文件';
            el.body.appendChild(empty);
            return;
        }

        if (categoryOf(path) === 'markdown') {
            el.body.classList.remove('media');
            const body = document.createElement('div');
            body.className = 'preview-md';
            try {
                body.innerHTML = typeof renderMarkdown === 'function'
                    ? renderMarkdown(data.content)
                    : (typeof escapeHtml === 'function' ? escapeHtml(data.content) : '');
            } catch (e) {
                body.textContent = data.content;
            }
            el.body.appendChild(body);
            if (window.MathJax && window.MathJax.typesetPromise) {
                window.MathJax.typesetPromise([body]).catch(() => {});
            }
        } else {
            const pre = document.createElement('pre');
            pre.className = 'preview-code' + (state.preview.wrap ? ' wrap' : '');
            pre.tabIndex = 0;
            const code = document.createElement('code');
            code.textContent = data.content;
            pre.appendChild(code);
            el.body.appendChild(pre);
        }

        restorePreviewScroll();
    }

    function renderPreviewError(message) {
        el.body.textContent = '';
        const box = document.createElement('div');
        box.className = 'preview-error';
        const text = document.createElement('div');
        const raw = String(message || '加载失败');
        text.textContent = /not found/i.test(raw) ? '文件不存在或已被移动' : raw;
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn btn-small';
        retry.textContent = '重试';
        retry.addEventListener('click', () => reloadPreview());
        box.appendChild(text);
        box.appendChild(retry);
        el.body.appendChild(box);
    }

    function restorePreviewScroll() {
        const target = state.preview.scrollTop || 0;
        requestAnimationFrame(() => {
            if (el && state.preview.open) el.body.scrollTop = target;
        });
    }

    function reloadPreview(opts = {}) {
        if (!state.preview.open || !state.preview.path) return;
        if (opts.preserveScroll !== false) {
            state.preview.scrollTop = el.body.scrollTop;
        }
        const cat = categoryOf(state.preview.path);
        if (cat === 'image' || cat === 'pdf' || cat === 'audio' || cat === 'video' || cat === 'html' || cat === 'office') {
            state.preview.mtime = Date.now();
            renderPreviewBody();
        } else {
            loadTextPreview(state.preview.path);
        }
    }

    function toggleWrap() {
        state.preview.wrap = !state.preview.wrap;
        el.wrapBtn.classList.toggle('active', state.preview.wrap);
        const pre = el.body.querySelector('.preview-code');
        if (pre) pre.classList.toggle('wrap', state.preview.wrap);
        savePreviewState();
    }

    function restorePreview() {
        try {
            if (localStorage.getItem(KEYS.previewOpen) !== '1') return;
        } catch (e) {
            return;
        }
        const store = readJSON(KEYS.previewLast, {});
        const entry = store && store[state.ws.uuid];
        if (!entry || !entry.path) return;
        state.preview.wrap = !!entry.wrap;
        state.preview.scrollTop = Number(entry.scrollTop) || 0;
        openPreview(entry.path, { restore: true });
    }

    // ---------- 布局 ----------
    function applyPreviewWidth() {
        if (!el) return;
        const maxWidth = Math.min(720, Math.max(PREVIEW_MIN_WIDTH, Math.floor(window.innerWidth * 0.6)));
        const width = clamp(readNum(KEYS.previewWidth, PREVIEW_DEFAULT_WIDTH), PREVIEW_MIN_WIDTH, maxWidth);
        el.pane.style.width = `${width}px`;
    }

    function sidebarBodyHeight() {
        const sidebar = document.querySelector('.sidebar');
        if (!sidebar) return 0;
        const header = sidebar.querySelector('.sidebar-header');
        const footer = sidebar.querySelector('.sidebar-footer');
        return Math.max(
            0,
            sidebar.clientHeight
                - (header ? header.offsetHeight : 0)
                - (footer ? footer.offsetHeight : 0)
                - 6
        );
    }

    function applyFilesHeight() {
        if (!el) return;
        const collapsed = localStorage.getItem(KEYS.filesCollapsed) === '1';
        el.filesSection.classList.toggle('collapsed', collapsed);
        el.hSplit.classList.toggle('hidden', collapsed);
        if (collapsed) {
            el.filesSection.style.flex = '';
            return;
        }
        const bodyHeight = sidebarBodyHeight();
        let height = readNum(KEYS.filesHeight, 0);
        if (!height) height = Math.round(bodyHeight * 0.55);
        height = clamp(height, 140, Math.max(160, bodyHeight - 140));
        el.filesSection.style.flex = `0 0 ${height}px`;
    }

    function initFilesSplitter() {
        let startY = 0;
        let startHeight = 0;
        let dragging = false;

        el.hSplit.addEventListener('mousedown', (event) => {
            if (el.filesSection.classList.contains('collapsed')) return;
            event.preventDefault();
            dragging = true;
            startY = event.clientY;
            startHeight = el.filesSection.getBoundingClientRect().height;
            el.hSplit.classList.add('dragging');
            document.body.style.cursor = 'ns-resize';
            document.body.style.userSelect = 'none';
        });

        document.addEventListener('mousemove', (event) => {
            if (!dragging) return;
            const bodyHeight = sidebarBodyHeight();
            const height = clamp(
                startHeight + (event.clientY - startY),
                140,
                Math.max(160, bodyHeight - 140)
            );
            el.filesSection.style.flex = `0 0 ${height}px`;
        });

        document.addEventListener('mouseup', () => {
            if (!dragging) return;
            dragging = false;
            el.hSplit.classList.remove('dragging');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            const height = Math.round(el.filesSection.getBoundingClientRect().height);
            try {
                localStorage.setItem(KEYS.filesHeight, String(height));
            } catch (e) { /* ignore */ }
        });

        el.hSplit.addEventListener('dblclick', () => {
            try {
                localStorage.removeItem(KEYS.filesHeight);
            } catch (e) { /* ignore */ }
            applyFilesHeight();
        });
    }

    function toggleFilesSection(force) {
        const collapsed = force === undefined
            ? !el.filesSection.classList.contains('collapsed')
            : !force;
        try {
            localStorage.setItem(KEYS.filesCollapsed, collapsed ? '1' : '0');
        } catch (e) { /* ignore */ }
        if (collapsed) {
            el.filesSection.classList.add('collapsed');
            el.hSplit.classList.add('hidden');
            el.filesSection.style.flex = '';
        } else {
            applyFilesHeight();
        }
    }

    function initPreviewResize() {
        let startX = 0;
        let startWidth = 0;
        let dragging = false;

        el.previewHandle.addEventListener('mousedown', (event) => {
            event.preventDefault();
            dragging = true;
            startX = event.clientX;
            startWidth = el.pane.getBoundingClientRect().width;
            el.previewHandle.classList.add('dragging');
            document.body.style.cursor = 'ew-resize';
            document.body.style.userSelect = 'none';
        });

        document.addEventListener('mousemove', (event) => {
            if (!dragging) return;
            const maxWidth = Math.min(720, Math.max(PREVIEW_MIN_WIDTH, Math.floor(window.innerWidth * 0.6)));
            const width = clamp(startWidth + (startX - event.clientX), PREVIEW_MIN_WIDTH, maxWidth);
            el.pane.style.width = `${width}px`;
        });

        document.addEventListener('mouseup', () => {
            if (!dragging) return;
            dragging = false;
            el.previewHandle.classList.remove('dragging');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            const width = Math.round(el.pane.getBoundingClientRect().width);
            try {
                localStorage.setItem(KEYS.previewWidth, String(width));
            } catch (e) { /* ignore */ }
        });

        el.previewHandle.addEventListener('dblclick', () => {
            el.pane.style.width = `${PREVIEW_DEFAULT_WIDTH}px`;
            try {
                localStorage.setItem(KEYS.previewWidth, String(PREVIEW_DEFAULT_WIDTH));
            } catch (e) { /* ignore */ }
        });
    }

    // ---------- 工作区与刷新 ----------
    function updateSectionState() {
        if (!el) return;
        const disabled = !state.ws;
        el.refreshBtn.disabled = disabled;
        el.collapseAllBtn.disabled = disabled;
        el.toggleBtn.disabled = disabled;
    }

    function setWorkspace(ws) {
        const uuid = ws?.uuid || null;
        const prevUuid = state.ws?.uuid || null;
        if (uuid && uuid === prevUuid && ws?.directory === state.ws?.directory) return;

        const first = !booted;
        booted = true;

        if (state.ws) {
            saveTreeState();
            savePreviewState();
        }

        state.token += 1;
        const token = state.token;
        state.ws = ws || null;
        state.dirs.clear();
        state.inflight.clear();
        state.expanded.clear();
        state.selected = null;
        state.focusPath = null;
        state.rows = [];
        state.restoreScroll = null;
        state.preview.requestToken += 1;

        if (state.preview.open && !first) {
            state.preview.path = null;
            closePreview();
        } else if (!state.ws && state.preview.open) {
            state.preview.path = null;
            closePreview();
        }

        updateSectionState();

        if (!state.ws) {
            render();
            return;
        }

        loadTreeState();
        render();

        loadDir('', { force: true }).then(() => {
            if (token !== state.token) return;
            restoreExpansion();
            if (first) restorePreview();
            render();
        });
    }

    async function refreshTree() {
        if (!state.ws) return;
        const token = state.token;
        const targets = new Set(['']);
        for (const dir of state.expanded) {
            if (state.dirs.get(dir)?.items) targets.add(dir);
        }
        await Promise.all(Array.from(targets).map((dir) => loadDir(dir, { force: true })));
        if (token !== state.token) return;
        render();
        if (state.preview.open) reloadPreview({ preserveScroll: true });
    }

    function scheduleAutoRefresh() {
        if (!state.ws || !el) return;
        if (el.filesSection.classList.contains('collapsed') && !state.preview.open) return;
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(refreshTree, AUTO_REFRESH_MS);
    }

    // ---------- 初始化 ----------
    function init() {
        if (initialized) return;
        el = {
            tree: document.getElementById('file-tree'),
            treeStatus: document.getElementById('file-tree-status'),
            filesSection: document.getElementById('files-section'),
            hSplit: document.getElementById('sidebar-h-split'),
            toggleBtn: document.getElementById('files-toggle-btn'),
            collapseAllBtn: document.getElementById('files-collapse-all-btn'),
            refreshBtn: document.getElementById('files-refresh-btn'),
            pane: document.getElementById('preview-pane'),
            previewHandle: document.getElementById('preview-resize-handle'),
            previewIcon: document.getElementById('preview-file-icon'),
            previewName: document.getElementById('preview-name'),
            previewPath: document.getElementById('preview-path'),
            body: document.getElementById('preview-body'),
            previewStatus: document.getElementById('preview-status'),
            wrapBtn: document.getElementById('preview-wrap-btn'),
            maximizeBtn: document.getElementById('preview-maximize-btn'),
            reloadBtn: document.getElementById('preview-reload-btn'),
            downloadBtn: document.getElementById('preview-download-btn'),
            closeBtn: document.getElementById('preview-close-btn'),
        };
        if (!el.tree || !el.pane) return;
        initialized = true;

        el.tree.addEventListener('click', onTreeClick);
        el.tree.addEventListener('contextmenu', onTreeContextMenu);
        el.tree.addEventListener('keydown', onTreeKeyDown);
        el.tree.addEventListener('scroll', () => {
            clearTimeout(treeScrollTimer);
            treeScrollTimer = setTimeout(saveTreeState, 300);
        });

        el.toggleBtn.addEventListener('click', () => toggleFilesSection());
        el.collapseAllBtn.addEventListener('click', () => collapseTree());
        el.refreshBtn.addEventListener('click', () => refreshTree());

        el.closeBtn.addEventListener('click', closePreview);
        el.reloadBtn.addEventListener('click', () => reloadPreview({ preserveScroll: true }));
        el.wrapBtn.addEventListener('click', toggleWrap);
        el.maximizeBtn.addEventListener('click', () => setMaximized(!isMaximized()));
        el.pane.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                if (isMaximized()) setMaximized(false);
                else closePreview();
            }
        });
        el.body.addEventListener('scroll', () => {
            state.preview.scrollTop = el.body.scrollTop;
            clearTimeout(previewScrollTimer);
            previewScrollTimer = setTimeout(savePreviewState, 300);
        });

        document.addEventListener('mousedown', (event) => {
            if (ctxMenu && !ctxMenu.contains(event.target)) closeContextMenu();
        });
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') closeContextMenu();
        });
        window.addEventListener('scroll', closeContextMenu, true);

        window.addEventListener('resize', () => {
            applyPreviewWidth();
            if (!el.filesSection.classList.contains('collapsed')) applyFilesHeight();
        });

        window.addEventListener('cili:turn-complete', scheduleAutoRefresh);
        window.addEventListener('cili:files-changed', scheduleAutoRefresh);

        initFilesSplitter();
        initPreviewResize();
        applyFilesHeight();
        updateSectionState();
        render();
    }

    document.addEventListener('DOMContentLoaded', init);

    return {
        init,
        setWorkspace,
        refresh: refreshTree,
        openPreview,
        closePreview,
        isPreviewOpen: () => state.preview.open,
    };
})();

window.FileTree = FileTree;
