"""Filesystem helpers: atomic JSON writes and corruption-safe loads.

状态文件（cron、loop、subagent 进度等）直接 open("w") 写入时，进程中断
会产生半写文件；加载侧遇到损坏文件返回默认值后，下一次保存会用默认数据
覆盖原始文件，造成损坏放大。这里统一两个动作：
- atomic_write_json: 先写临时文件再 os.replace，保证读者看到的要么是
  旧文件要么是新文件，不会是半写内容
- load_json_or_backup: 加载失败时把损坏文件改名备份（保留现场供恢复），
  再返回默认值，避免下一次保存无痕覆盖原始数据
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def atomic_write_json(path: Path | str, data: Any, indent: int = 2) -> None:
    """原子写入 JSON 文件：写临时文件 + fsync + os.replace。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def load_json_or_backup(path: Path | str, default: Any) -> Any:
    """加载 JSON；损坏时把文件改名为 *.corrupt-<时间戳> 备份后返回 default。"""
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        backup = path.with_name(f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        try:
            os.replace(path, backup)
            logger.error(f"[fs] JSON 文件损坏，已备份到 {backup}: {e} ({path})")
        except Exception:
            logger.error(f"[fs] JSON 文件损坏且备份失败: {e} ({path})")
        return default
