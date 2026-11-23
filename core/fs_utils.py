"""Filesystem helpers: atomic JSON writes and corruption-safe loads.

状态文件（cron、loop、agent 进度等）直接 open("w") 写入时，进程中断
会产生半写文件；加载侧遇到损坏文件返回默认值后，下一次保存会用默认数据
覆盖原始文件，造成损坏放大。这里统一两个动作：
- atomic_write_json: 先写临时文件再 os.replace，保证读者看到的要么是
  旧文件要么是新文件，不会是半写内容
- load_json_or_backup: 加载失败时把损坏文件改名备份（保留现场供恢复），
  再返回默认值，避免下一次保存无痕覆盖原始数据

Windows 上 os.replace 会因目标/临时文件被短暂占用而报 WinError 5（读者没有
FILE_SHARE_DELETE、杀软扫描等），替换带指数退避重试。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def os_temp_root() -> str:
    """进程环境的临时目录根（realpath 后），作为路径白名单安全区。"""
    for var in ("TMP", "TEMP"):
        value = os.environ.get(var)
        if value:
            return os.path.realpath(value)
    return os.path.realpath(tempfile.gettempdir())


def is_path_within(resolved: str, root: str) -> bool:
    """resolved 是否在 root 内（Windows 大小写不敏感，跨盘符视为越界）。"""
    try:
        return os.path.normcase(
            os.path.commonpath([resolved, root])
        ) == os.path.normcase(root)
    except ValueError:
        return False


def normalize_bash_path(path: str) -> str:
    """把 bash 风格路径（/c/...）转为 Windows 路径（C:/...）。

    LLM 经常从 shell 输出中复制 /e/AI/... 风格路径，
    而 Windows Python 不识别这种绝对路径（os.path.isabs 返回 False），
    需提前转换，否则会被错误地当作相对路径拼到 cwd。

    Examples:
        /e/AI/cili -> E:/AI/cili
        /c/Users/x -> C:/Users/x
        E:/AI/cili -> E:/AI/cili  (已是 Windows 路径，不变)
        ./src/main -> ./src/main  (相对路径，不变)
    """
    if not path or not path.startswith('/'):
        return path
    # 检测 /x/ 格式（x 是单字母盘符，紧跟 /）
    if len(path) >= 3 and path[2] == '/' and path[1].isalpha():
        drive = path[1].upper()
        return f"{drive}:{path[2:]}"
    return path


def _atomic_temp_path(path: Path) -> Path:
    """生成目标文件唯一的临时路径（随机后缀，避免并发写互踩，SEC-23）。"""
    return path.with_name(f"{path.name}.tmp.{secrets.token_hex(3)}")


# Windows 上 os.replace 会因目标/临时文件被短暂占用而失败：
# - 读者（前端轮询 exec 日志、会话详情）用 Python open 读取时没有
#   FILE_SHARE_DELETE，替换目标会得到 WinError 5（拒绝访问）；
# - 杀毒/索引服务扫描刚写出的 .tmp 也会短暂锁住它。
# 这类占用是瞬时的，重试几次即可成功；不重试会丢一次进度写入并刷 WARNING。
_REPLACE_ATTEMPTS = 6
_REPLACE_BASE_DELAY = 0.02
_REPLACE_MAX_DELAY = 0.25


def _replace_with_retry(tmp_path: Path, path: Path) -> None:
    """os.replace，遇到 PermissionError（被占用）按指数退避重试。"""
    delay = _REPLACE_BASE_DELAY
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if attempt >= _REPLACE_ATTEMPTS:
                raise
            logger.debug(
                f"[fs] replace 被占用，重试 {attempt}/{_REPLACE_ATTEMPTS}: {path}"
            )
            time.sleep(delay)
            delay = min(delay * 2, _REPLACE_MAX_DELAY)


def atomic_write_text(path: Path | str, content: str, newline: str = "",
                      encoding: str = "utf-8") -> None:
    """原子写入任意文本文件：唯一临时文件 + fsync + os.replace。

    newline="" 禁用换行翻译（保持 LF）；传 "\\r\\n" 可保留 CRLF 风格。
    encoding 默认 utf-8；edit 工具按原文件编码传入，避免把 GBK 文件整体转码。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _atomic_temp_path(path)
    try:
        with open(tmp_path, "w", encoding=encoding, newline=newline) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp_path, path)
    except BaseException:
        # 失败时清理临时文件，避免残留（.tmp 后缀文件会一直留在目录里）
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path | str, data: Any, indent: int = 2) -> None:
    """原子写入 JSON 文件：写唯一临时文件 + fsync + os.replace。

    临时文件名带随机后缀，避免固定 `.tmp` 名在多线程/多实例并发写同一
    目标时互踩（SEC-23）；写入用 newline="" 统一 LF。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _atomic_temp_path(path)
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp_path, path)
    except BaseException:
        # 失败时清理临时文件（序列化失败会留下带随机后缀的 .tmp）
        tmp_path.unlink(missing_ok=True)
        raise


def load_json_or_backup(path: Path | str, default: Any) -> Any:
    """加载 JSON；损坏时把文件改名为 *.corrupt-<时间戳>-<随机> 备份后返回 default。"""
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        # 秒级时间戳 + 随机后缀，保证同一秒内多次损坏也能保留每一份现场（C4）
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.corrupt-{stamp}-{secrets.token_hex(2)}")
        try:
            os.replace(path, backup)
            logger.error(f"[fs] JSON 文件损坏，已备份到 {backup}: {e} ({path})")
        except Exception:
            logger.error(f"[fs] JSON 文件损坏且备份失败: {e} ({path})")
        return default


def safe_upload_filename(raw: str) -> str | None:
    """净化上传/下载文件名：取 basename，拒绝 NTFS ADS 与 Windows 保留名/字符。

    返回净化后的安全文件名；非法时返回 None（调用方记为错误）。

    原先私有在 ``web/routes_files.py``；QQ 入站附件与 Web 上传共用同一份规则，
    故下沉到此处。
    """
    if not raw:
        return None
    name = os.path.basename(raw.replace("\\", "/")).strip()
    if not name or name in (".", ".."):
        return None
    # NTFS 备用数据流（file.txt:stream）与 Windows 保留字符
    if ":" in name or any(c in name for c in '<>"|?*'):
        return None
    # Windows 保留设备名：CON/PRN/AUX/NUL/COM1-9/LPT1-9
    stem = name.split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"}:
        return None
    if (stem.startswith("COM") and stem[3:].isdigit()) or        (stem.startswith("LPT") and stem[3:].isdigit()):
        return None
    return name


def ensure_gitignore_entries(
    workspace_dir: Path | str,
    *,
    base_text: str = "",
    add: tuple[str, ...] = (),
    remove: tuple[str, ...] = (),
) -> None:
    """统一维护工作区 .gitignore：可移除指定条目、追加缺失条目。"""
    path = Path(workspace_dir) / ".gitignore"
    try:
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
        else:
            lines = base_text.splitlines()
    except OSError:
        return
    changed = not path.exists()
    kept = []
    for line in lines:
        if line.strip() in remove:
            changed = True
            continue
        kept.append(line)
    normalized = {line.strip() for line in kept}
    for item in add:
        if item not in normalized:
            kept.append(item)
            changed = True
    if changed:
        atomic_write_text(path, "\n".join(kept) + "\n")
