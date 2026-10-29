"""教材 Markdown 库：``core/templates/textbooks/<content_id>.md`` 的读取与复制。

PDF 下载无论成功与否，向导创建与手动下载前都会先把对应教材的 Markdown
复制到工作区 ``教材/<可读教材名>.md``，保证工作区始终有一份可读教材内容。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Iterable

from core.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

MD_DIR = PROJECT_ROOT / "core" / "templates" / "textbooks"


def markdown_path(content_id: str) -> Path:
    return MD_DIR / f"{content_id}.md"


def has_markdown(content_id: str) -> bool:
    return markdown_path(content_id).is_file()


def copy_textbook_markdown(
    workspace_dir: str | Path,
    catalog: Any,
    books: Iterable[Any],
) -> list[str]:
    """把已生成的教材 Markdown 复制到工作区 ``教材/``，返回复制后的路径列表。

    文件名使用目录生成的平铺可读名（重名带版本前缀），后缀改为 ``.md``；
    与随后的 PDF 下载（``.pdf``）并存。源文件更新时才覆盖。
    """
    target_dir = Path(workspace_dir) / "教材"
    copied: list[str] = []
    for book in books:
        source = markdown_path(book.content_id)
        if not source.is_file():
            continue
        flat = catalog.flat_filename(book)
        stem = flat[:-4] if flat.lower().endswith(".pdf") else flat
        target = target_dir / f"{stem}.md"
        try:
            if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(str(target))
        except OSError as e:
            logger.warning(f"[textbooks] 复制教材 Markdown 失败: {source} -> {target}: {e}")
    return copied
