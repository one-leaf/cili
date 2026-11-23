"""进程临时目录访问：权限判定、shell 与工具共用同一解析口径。"""

from __future__ import annotations

import os
import tempfile


def temp_root() -> str:
    """进程临时根目录（每次读环境变量，realpath 解析短名与符号链接）。

    ``tempfile.gettempdir()`` 会缓存首次结果，环境变量在测试/子进程里改变后
    不生效；这里先读 TMP/TEMP，与权限判定的既有语义保持一致。
    """
    for var in ("TMP", "TEMP"):
        value = os.environ.get(var)
        if value:
            return os.path.realpath(value)
    return os.path.realpath(tempfile.gettempdir())


def new_temp_dir(prefix: str) -> str:
    """在进程临时根目录下创建唯一子目录。"""
    return tempfile.mkdtemp(prefix=prefix, dir=temp_root())
