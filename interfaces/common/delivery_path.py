"""接入端出站媒体的共享件：路径白名单校验与错误类型。"""

from __future__ import annotations

import os
from pathlib import Path


class MediaError(Exception):
    """出站媒体处理失败（文案可直接给用户看）。"""


def validate_media_path(path: str | Path, workspace_root: str | Path) -> Path:
    """路径白名单校验（realpath 双侧比对，防符号链接绕过）。

    ``send_user_file`` 工具已经校验过一遍，这里再校验一次是纵深防御——接入端
    才是真正把文件内容交给传输层的地方。
    """
    target = Path(os.path.realpath(str(path)))
    root = Path(os.path.realpath(str(workspace_root)))
    try:
        inside = os.path.commonpath([str(target), str(root)]) == str(root)
    except ValueError:
        inside = False
    if not inside:
        raise MediaError(f"文件不在工作区内：{path}")
    if not target.is_file():
        raise MediaError(f"文件不存在：{path}")
    return target
