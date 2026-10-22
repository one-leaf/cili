"""受保护路径判定：青少年模式下系统数据与会话数据的写/删硬拒绝。

受保护清单集中在此（便于扩充）：
- ``data/cili/**``   —— 系统数据：setting.json、workspaces.json、teen_mode.json、
                        cron.d、工具状态等。agent 改这些等于改配置/关模式
- ``data/.cili/**``  —— system 工作区数据目录
- 各工作区 ``{directory}/.cili/**`` —— 会话、记忆、审批规则、执行日志
                        （``tmp/`` 子目录除外：临时文件不持久化）

判定是**硬 deny**：属于模式语义本身，解锁窗口也不放行（对照：越界写/删
只是降级，解锁后可走正常审批）。模式关闭时一律返回 None（零行为差异）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from core.config import DATA_DIR, PROJECT_ROOT, SYSTEM_DATA_DIR, load_workspaces_index

logger = logging.getLogger(__name__)


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _is_within(child: str, root: str) -> bool:
    """child 是否在 root 内（双方 normcase；跨盘符 ValueError → False）。"""
    try:
        return os.path.commonpath([child, root]) == root
    except ValueError:
        return False


def protected_roots() -> list[str]:
    """当前受保护根目录清单（含全部工作区的 .cili 数据目录）。"""
    roots = [_norm(DATA_DIR), _norm(SYSTEM_DATA_DIR),
             _norm(PROJECT_ROOT / "workspace" / "default" / ".cili")]
    seen = set(roots)
    try:
        entries = load_workspaces_index()
    except Exception:  # noqa: BLE001 —— 索引读不出时按静态根判定
        entries = []
    for entry in entries:
        directory = entry.get("directory")
        if not directory:
            continue
        root = _norm(Path(directory) / ".cili")
        if root not in seen:
            seen.add(root)
            roots.append(root)
    return roots


def teen_path_verdict(resolved: str | None, op: str) -> str | None:
    """受保护路径判定：resolved 落在受保护清单 → ``"deny"``；否则 None。

    模式关闭（或 resolved 无法解析）一律 None，由调用方走原有判定。
    op（write/delete）当前不做区分——两类操作同样硬禁。
    """
    if not resolved:
        return None
    from core.teen_mode import get_teen_mode

    if not get_teen_mode().is_enabled():
        return None
    target = _norm(resolved)
    for root in protected_roots():
        if _is_within(target, root):
            tmp = _norm(os.path.join(root, "tmp"))
            if _is_within(target, tmp):
                return None  # 临时目录不持久化，放行
            return "deny"
    return None
