"""工作区 Office 文档 → PDF 缓存增量同步。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from core.office_pdf.cache import (
    _DEFAULT_TIMEOUT,
    SUPPORTED_EXTENSIONS,
    _cache_key,
    _prune_cache,
    _read_meta,
    _source_key,
    ensure_pdf,
    workspace_cache_dir,
)
from core.paths import resolve_storage_path

_SKIP_DIR_NAMES = {".cili", ".git", "node_modules", "__pycache__", ".svn", ".idea", ".vscode"}


def _iter_office_docs(workspace_dir: Path):
    """遍历工作区内的 Office 文档（跳过 .cili/.git/node_modules 等目录）。"""
    for dirpath, dirnames, filenames in os.walk(workspace_dir):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                yield p


def sync_workspace_pdf_cache(workspace_uuid: str, *, limit: int = 100,
                             timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """把一个工作区的全部 Office 文档增量同步到 ``.cili/cache/pdf/``。

    - 新增/变更的文档转换为 PDF（受 ``limit`` 限额，超出留给下一轮）；
    - 源文件已删除或 meta 损坏的缓存条目（过期 PDF）直接清理；
    - 同源旧版本由 ``ensure_pdf`` 转换时自动清理。

    写操作只落在 ``{workspace}/.cili/cache/pdf/``（工具内部缓存，不触碰
    用户文件），因此不需要路径策略门禁。
    """
    from core.config import get_workspace_meta

    cfg = get_workspace_meta(workspace_uuid)
    if not cfg or not (cfg.get("directory")):
        return {"workspace_uuid": workspace_uuid, "error": "workspace directory not found"}
    root = Path(cfg["directory"])
    if not root.exists():
        return {"workspace_uuid": workspace_uuid, "error": "workspace directory not found"}

    cache_dir = workspace_cache_dir(workspace_uuid)
    docs = sorted(_iter_office_docs(root))
    valid_sources = {_source_key(d) for d in docs}

    # 过期清理：meta 指向的源已不存在，或 meta 损坏
    removed = 0
    for meta_path in cache_dir.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            source = str(meta.get("source", ""))
            # 源在遍历范围内，或源仍是工作区外/跳过目录中的有效文件 → 保留
            if not source:
                source_ok = False
            else:
                try:
                    source_exists = os.path.exists(resolve_storage_path(source))
                except ValueError:
                    source_exists = False
                source_ok = _source_key(source) in valid_sources or source_exists
        except Exception:
            source_ok = False
        if not source_ok:
            meta_path.unlink(missing_ok=True)
            meta_path.with_suffix(".pdf").unlink(missing_ok=True)
            removed += 1

    converted = skipped = failed = 0
    errors: list[str] = []
    for doc in docs:
        try:
            key, stat = _cache_key(str(doc))
        except OSError:
            continue
        if _read_meta(cache_dir / f"{key}.json", cache_dir / f"{key}.pdf", stat) is not None:
            skipped += 1
            continue
        if converted >= limit:
            continue  # 超出本轮限额：留给下一轮（2 小时后）
        try:
            ensure_pdf(str(doc), cache_dir=cache_dir, timeout=timeout, prune=False)
            converted += 1
        except Exception as e:
            failed += 1
            errors.append(f"{doc.name}: {e}")

    # 批量路径只做一次全局 prune，避免每次转换都扫描整个缓存目录
    _prune_cache(cache_dir)

    return {
        "workspace_uuid": workspace_uuid,
        "docs": len(docs),
        "converted": converted,
        "skipped": skipped,
        "failed": failed,
        "removed_stale": removed,
        "cache_dir": str(cache_dir),
        "errors": errors[:10],
    }
