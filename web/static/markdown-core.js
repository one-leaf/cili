// ── 共享 Markdown 渲染核心（主界面 / 会话页 / 设置说明）──
// 职责：保护公式 → marked.parse → mermaid/PDF 后处理 → 还原公式 → XSS 清洗。
// 不做 workspace_uuid 链接注入；调用方在传入前完成各自的链接改写。
function renderMarkdownCore(text) {
    if (!text) return '';

    const mathBlocks = [];
    text = text.replace(/\$\$([\s\S]*?)\$\$/g, (match) => {
        const placeholder = 'MATHBLOCK{' + mathBlocks.length + '}';
        mathBlocks.push(match);
        return placeholder;
    });
    text = text.replace(/(?<!\$)\$(?!\$)(.*?)(?<!\$)\$(?!\$)/g, (match) => {
        const placeholder = 'MATHBLOCK{' + mathBlocks.length + '}';
        mathBlocks.push(match);
        return placeholder;
    });

    let html = marked.parse(text);

    // mermaid 代码块 → 容器，由页面上的 mermaid.js 渲染
    html = html.replace(/<pre><code class="language-mermaid">([\s\S]*?)<\/code><\/pre>/g, (match, content) => {
        const decoded = content
            .replace(/&lt;/g, '<')
            .replace(/&gt;/g, '>')
            .replace(/&quot;/g, '"')
            .replace(/&#39;/g, "'")
            .replace(/&amp;/g, '&');
        return '<div class="mermaid">' + decoded + '</div>';
    });

    // PDF <img> → iframe 内嵌预览
    html = html.replace(
        /<img([^>]*)\ssrc="([^"]*\.pdf)(\?[^"]*)?"/g,
        (match, before, src, query) => {
            const url = src + (query || '');
            const altMatch = before.match(/alt="([^"]*)"/);
            const alt = altMatch ? altMatch[1] : 'PDF 文件';
            return `<div class="pdf-preview"><iframe src="${url}" title="${alt}"></iframe><a href="${url}" target="_blank" class="pdf-link">📄 ${alt || '查看 PDF'}</a></div>`;
        }
    );

    // 还原公式；用函数替换，避免 $$ / $& 等被当作替换模式
    mathBlocks.forEach((block, idx) => {
        html = html.replace('MATHBLOCK{' + idx + '}', () => block);
    });

    // XSS 防护：sanitize HTML（允许 MathJax 所需标签）
    if (typeof DOMPurify !== 'undefined') {
        html = DOMPurify.sanitize(html, {
            ADD_TAGS: ['mjx-container', 'annotation', 'semantics', 'math', 'iframe'],
            ADD_ATTR: ['encoding', 'display', 'xmlns', 'target', 'allowfullscreen'],
        });
    } else {
        // DOMPurify 加载失败时 fail-closed：转义所有标签
        html = html.replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
    return html;
}
