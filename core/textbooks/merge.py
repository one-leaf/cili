"""教材分片合并：按数字序号做字节拼接，并校验 PDF 头尾与大小。

ChinaTextbook 仓库把超过 50MB 的 PDF 拆成 ``.pdf.1``、``.pdf.2`` 等分片。
README 推荐用 ``mergePDFs`` 程序，或 Windows 的 ``copy /b``、macOS/Linux 的
``cat``。本模块做同样的二进制拼接；序号由调用方传入，避免 ``.10`` 在字符串
排序时落在 ``.2`` 前面。
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class TextbookMergeError(ValueError):
    """分片缺失、大小不符或合并后的 PDF 校验失败。"""


@dataclass(frozen=True)
class MergeResult:
    """合并结果。"""

    target: Path
    size: int
    sha256: str
    chunk_count: int


def _validate_pdf_bytes(head: bytes, tail: bytes) -> None:
    if not head.startswith(b"%PDF-"):
        raise TextbookMergeError("PDF 文件头校验失败（缺少 %PDF-）")
    if b"%%EOF" not in tail:
        raise TextbookMergeError("PDF 文件尾校验失败（缺少 %%EOF）")


def merge_textbook_chunks(
    chunks: Sequence[Path | str],
    target: Path | str,
    *,
    expected_size: int | None = None,
    expected_sha256: str = "",
    overwrite: bool = False,
) -> MergeResult:
    """把分片按调用方给定的顺序合并为 ``target``。

    ``chunks`` 必须按分片序号传入（通常是清单里的 ``files[].index``）。合并先写
    同目录临时文件，全部校验通过后再 ``os.replace``，避免留下半个 PDF。
    """
    chunk_paths = [Path(item) for item in chunks]
    if not chunk_paths:
        raise TextbookMergeError("没有可合并的教材分片")
    target = Path(target)
    if target.exists() and not overwrite:
        raise TextbookMergeError(f"目标教材已存在: {target}")

    total_size = 0
    for chunk in chunk_paths:
        if not chunk.is_file():
            raise TextbookMergeError(f"教材分片不存在: {chunk}")
        size = chunk.stat().st_size
        if size <= 0:
            raise TextbookMergeError(f"教材分片为空: {chunk}")
        total_size += size
    if expected_size is not None and total_size != expected_size:
        raise TextbookMergeError(
            f"分片总大小不符: 实际 {total_size}，清单 {expected_size}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f"{target.name}.merge.{secrets.token_hex(4)}")
    digest = hashlib.sha256()
    try:
        with open(tmp_path, "wb") as out:
            for chunk in chunk_paths:
                with open(chunk, "rb") as src:
                    while True:
                        block = src.read(1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        out.write(block)
            out.flush()
            os.fsync(out.fileno())
        if expected_sha256 and digest.hexdigest().lower() != expected_sha256.lower():
            raise TextbookMergeError("合并后的 SHA256 校验失败")
        with open(tmp_path, "rb") as check:
            head = check.read(5)
            check.seek(-min(4096, total_size), os.SEEK_END)
            tail = check.read()
        _validate_pdf_bytes(head, tail)
        os.replace(tmp_path, target)
    except TextbookMergeError:
        tmp_path.unlink(missing_ok=True)
        raise
    except OSError as e:
        tmp_path.unlink(missing_ok=True)
        raise TextbookMergeError(f"教材合并写入失败: {e}") from e
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return MergeResult(target=target, size=total_size, sha256=digest.hexdigest(), chunk_count=len(chunk_paths))
