"""文件类型 → 扩展名映射（find / grep 工具共享的单一真相源）。

新增文件类型时只需改此处，find 与 grep 同步生效。
"""

from __future__ import annotations

TYPE_EXTENSIONS: dict[str, list[str]] = {
    "py":     ["*.py", "*.pyi"],
    "js":     ["*.js", "*.jsx", "*.mjs", "*.cjs"],
    "ts":     ["*.ts", "*.tsx", "*.mts", "*.cts"],
    "md":     ["*.md", "*.mdx"],
    "json":   ["*.json"],
    "yaml":   ["*.yaml", "*.yml"],
    "html":   ["*.html", "*.htm"],
    "css":    ["*.css", "*.scss", "*.sass", "*.less"],
    "go":     ["*.go"],
    "rust":   ["*.rs"],
    "java":   ["*.java", "*.kt", "*.scala"],
    "sh":     ["*.sh", "*.bash"],
    "txt":    ["*.txt"],
    "xml":    ["*.xml", "*.svg"],
    "sql":    ["*.sql"],
    "c":      ["*.c", "*.h"],
    "cpp":    ["*.cpp", "*.hpp", "*.cc", "*.hh"],
}
