"""工作区 Git 版本管理模块。

为工作区目录提供 Git 版本控制功能：
- 初始化 Git 仓库（创建 .gitignore）
- 自动提交变更（对话结束后触发）
- 使用 LLM 生成提交摘要
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from core.config import (
    PROJECT_ROOT,
    get_workspace_data_dir,
    load_config,
    load_workspace_config,
    find_workspace_entry,
)
from core.llm import create_llm_client

logger = logging.getLogger(__name__)


# .gitignore 内容
_GITIGNORE_CONTENT = """# 临时文件和缓存
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
env/
venv/
ENV/
.venv
pip-log.txt
pip-delete-this-directory.txt

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# 系统文件
.DS_Store
Thumbs.db

# 日志文件
*.log

# 草履虫临时数据
.cili/tmp/

# Node.js
node_modules/
npm-debug.log*
yarn-debug.log*
yarn-error.log*

# Python
.tox/
.coverage
htmlcov/
.pytest_cache/
.mypy_cache/

# 构建产物
dist/
build/
*.egg-info/

# 环境变量
.env
.env.local
"""


def _find_git() -> str | None:
    """定位 git 可执行文件：内置 deps git 优先，其次系统 PATH。"""
    candidates = [
        PROJECT_ROOT / "data" / "deps" / "git" / "cmd" / "git.exe",
        PROJECT_ROOT / "data" / "deps" / "git" / "mingw64" / "bin" / "git.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("git")


def _git_cmd(workspace_dir: str | Path, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """执行 git 命令。"""
    git = _find_git()
    if not git:
        raise FileNotFoundError("git not available")
    return subprocess.run(
        [git, *args],
        cwd=str(workspace_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _get_workspace_dir(workspace_uuid: str) -> Path | None:
    """获取工作区目录路径。"""
    entry = find_workspace_entry(workspace_uuid)
    if not entry:
        return None
    directory = entry.get("directory", "")
    if not directory:
        return None
    return Path(directory)


def _sanitize_gitignore(workspace_dir: Path) -> None:
    """清理 .gitignore：
    1. 移除 .cili/ 条目（整个目录要纳入追踪）
    2. 确保 .cili/tmp/ 条目存在（临时数据要忽略）
    """
    gitignore_path = workspace_dir / ".gitignore"
    if not gitignore_path.exists():
        return
    try:
        lines = gitignore_path.read_text(encoding="utf-8").splitlines()
        # 移除 .cili 或 .cili/ 条目（但保留 .cili/tmp 相关条目）
        cleaned = []
        for line in lines:
            stripped = line.strip()
            # 精确匹配 .cili 或 .cili/（排除 .cili/tmp 等）
            if stripped in (".cili", ".cili/"):
                continue
            cleaned.append(line)

        # 检查是否已有 .cili/tmp 相关条目
        has_cili_tmp = any(
            ".cili/tmp" in line.strip()
            for line in cleaned
        )
        if not has_cili_tmp:
            cleaned.append(".cili/tmp/")

        if len(cleaned) != len(lines) or not has_cili_tmp:
            gitignore_path.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
            logger.info(f"[workspace-git] 已更新 .gitignore")
    except Exception as e:
        logger.warning(f"[workspace-git] 清理 .gitignore 失败: {e}")


def is_git_initialized(workspace_uuid: str) -> bool:
    """检查工作区是否已初始化 Git 仓库。"""
    workspace_dir = _get_workspace_dir(workspace_uuid)
    if not workspace_dir:
        return False
    git_dir = workspace_dir / ".git"
    return git_dir.is_dir()


def init_workspace_git(workspace_uuid: str) -> tuple[bool, str]:
    """初始化工作区 Git 仓库。

    创建 .git 目录、.gitignore 文件，配置本地用户信息。

    Returns:
        (success, message)
    """
    workspace_dir = _get_workspace_dir(workspace_uuid)
    if not workspace_dir:
        return False, "工作区目录不存在"

    try:
        # 检查 git 是否可用
        git = _find_git()
        if not git:
            return False, "Git 未安装"

        # 检查是否已初始化
        if (workspace_dir / ".git").is_dir():
            # 已有仓库，仍检查 .gitignore 确保 .cili/ 未被排除
            _sanitize_gitignore(workspace_dir)
            return True, "Git 仓库已存在"

        # 初始化仓库
        result = _git_cmd(workspace_dir, ["init", "-q"])
        if result.returncode != 0:
            return False, f"git init 失败: {result.stderr.strip()}"

        # 配置本地用户信息
        _git_cmd(workspace_dir, ["config", "user.email", "cili@localhost"])
        _git_cmd(workspace_dir, ["config", "user.name", "cili"])

        # 创建 .gitignore
        gitignore_path = workspace_dir / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text(_GITIGNORE_CONTENT, encoding="utf-8")

        # 确保已有 .gitignore 中不包含 .cili/
        _sanitize_gitignore(workspace_dir)

        # 初始提交（添加 .gitignore）
        _git_cmd(workspace_dir, ["add", ".gitignore"])
        _git_cmd(workspace_dir, ["commit", "-q", "-m", "初始化：添加 .gitignore"])

        logger.info(f"[workspace-git] 初始化工作区 Git 仓库: {workspace_uuid}")
        return True, "Git 仓库初始化成功"

    except Exception as e:
        logger.error(f"[workspace-git] 初始化失败: {e}")
        return False, f"初始化失败: {e}"


def _generate_commit_summary(diff_stat: str) -> str:
    """使用 LLM 生成提交摘要。

    Args:
        diff_stat: git diff --stat 输出

    Returns:
        提交摘要字符串，失败时返回默认摘要
    """
    if not diff_stat.strip():
        return "自动提交"

    try:
        config = load_config()
        model = config.lite_model or config.model
        client = create_llm_client(model)

        prompt = f"""根据以下文件变更统计，生成一行简洁的中文提交摘要（不超过 50 字）：

{diff_stat}

只返回摘要文本，不要其他内容。"""

        response = client.chat([
            {"role": "user", "content": prompt}
        ], max_tokens=100)

        summary = response.content.strip()
        # 清理可能的引号包裹
        if summary.startswith(("\"", "'", "\"")) and summary.endswith(("\"", "'", "\"")):
            summary = summary[1:-1]
        # 限制长度
        if len(summary) > 60:
            summary = summary[:57] + "..."
        return summary or "自动提交"

    except Exception as e:
        logger.warning(f"[workspace-git] 生成提交摘要失败: {e}")
        return "自动提交"


def auto_commit_workspace(workspace_uuid: str, session_id: str = "", sync_remote: bool = False) -> tuple[bool, str]:
    """自动提交工作区变更。

    检查是否有文件变更，如有则使用 LLM 生成摘要并提交。
    如果 sync_remote=True 且配置了远程仓库，自动拉取、推送并解决冲突。

    Args:
        workspace_uuid: 工作区 UUID
        session_id: 当前会话 ID（用于日志）
        sync_remote: 是否同步远程仓库（拉取+推送+自动解决冲突）

    Returns:
        (success, message)
    """
    workspace_dir = _get_workspace_dir(workspace_uuid)
    if not workspace_dir:
        return False, "工作区目录不存在"

    try:
        # 检查 git 是否已初始化
        if not (workspace_dir / ".git").is_dir():
            # 尝试自动初始化
            ok, msg = init_workspace_git(workspace_uuid)
            if not ok:
                return False, msg

        # 添加所有变更
        result = _git_cmd(workspace_dir, ["add", "-A"])
        if result.returncode != 0:
            return False, f"git add 失败: {result.stderr.strip()}"

        # 检查是否有变更
        result = _git_cmd(workspace_dir, ["diff", "--cached", "--stat", "-M"])
        diff_stat = result.stdout.strip()
        if not diff_stat:
            return True, "无变更"

        # 生成提交摘要
        summary = _generate_commit_summary(diff_stat)
        if session_id:
            summary = f"{summary} (session: {session_id[:8]})"

        # 执行提交
        result = _git_cmd(workspace_dir, ["commit", "-q", "-m", summary])
        if result.returncode != 0:
            return False, f"git commit 失败: {result.stderr.strip()}"

        logger.info(f"[workspace-git] 自动提交成功: {workspace_uuid} - {summary}")

        # 如果配置了远程仓库且需要同步
        if sync_remote:
            from core.config import load_workspace_config
            ws_config = load_workspace_config(workspace_uuid) or {}
            if ws_config.get("git_remote_url"):
                sync_ok, sync_msg = _sync_with_remote(workspace_uuid, workspace_dir)
                if sync_ok:
                    return True, f"{summary}; {sync_msg}"
                else:
                    logger.warning(f"[workspace-git] 远程同步失败: {sync_msg}")
                    return True, f"{summary}; 但远程同步失败: {sync_msg}"

        return True, summary

    except Exception as e:
        logger.error(f"[workspace-git] 自动提交失败: {e}")
        return False, f"自动提交失败: {e}"


def _sync_with_remote(workspace_uuid: str, workspace_dir: Path) -> tuple[bool, str]:
    """同步远程仓库：拉取（自动解决冲突）+ 推送。

    冲突解决策略：拉取时使用 rebase，失败时回退到 --strategy-option=ours（本地优先）。

    Returns:
        (success, message)
    """
    from core.config import load_workspace_config
    ws_config = load_workspace_config(workspace_uuid) or {}
    if not ws_config.get("git_remote_url"):
        return False, "未配置远程仓库"

    try:
        _sync_remote_to_git(workspace_uuid)

        # 先尝试 rebase 方式拉取
        result = _git_cmd(workspace_dir, ["pull", "--rebase", "origin", "HEAD"], timeout=120)

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # 如果没有上游分支，设置上游
            if "no tracking information" in stderr.lower() or "there is no tracking information" in stderr.lower():
                # 首次推送，设置上游分支
                _git_cmd(workspace_dir, ["push", "-u", "origin", "HEAD"], timeout=120)
                return True, "首次同步完成"

            # 如果有冲突，使用 ours 策略解决（保留本地版本）
            if "conflict" in stderr.lower() or "CONFLICT" in stderr:
                logger.warning(f"[workspace-git] 检测到冲突，使用 ours 策略解决")
                # 使用 ours 策略解决冲突（本地优先）
                _git_cmd(workspace_dir, ["rebase", "--abort"], timeout=30)
                _git_cmd(workspace_dir, ["pull", "--strategy-option=ours", "origin", "HEAD"], timeout=120)

            # 检查是否只是 "Already up to date"
            if "Already up to date" in stderr or "Already up-to-date" in stderr:
                pass  # 继续推送
            elif result.returncode != 0:
                return False, f"pull 失败: {stderr}"

        # 推送
        result = _git_cmd(workspace_dir, ["push", "origin", "HEAD"], timeout=120)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "Everything up-to-date" in stderr:
                return True, "已是最新"
            return False, f"push 失败: {stderr}"

        return True, "同步推送成功"

    except Exception as e:
        logger.error(f"[workspace-git] 远程同步失败: {e}")
        return False, f"同步失败: {e}"


from urllib.parse import urlparse, urlunparse


def _build_auth_url(remote_url: str, username: str = "", token: str = "") -> str:
    """将用户名和 Token 嵌入远程 URL，返回带凭证的 URL。

    示例: https://github.com/user/repo.git → https://user:token@github.com/user/repo.git
    已是 SSH 或已包含凭证则原样返回。
    """
    if not remote_url:
        return remote_url
    # SSH 协议不嵌入凭证
    if remote_url.startswith(("git@", "ssh://")):
        return remote_url
    parsed = urlparse(remote_url)
    if parsed.username:
        # 已含凭证，原样返回
        return remote_url
    if not username:
        return remote_url
    # 嵌入 user:token
    host = parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    auth = username
    if token:
        auth += f":{token}"
    netloc = f"{auth}@{host}{port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def _mask_token(token: str) -> str:
    """脱敏 Token：保留前 4 位 + 后 4 位，中间用 * 替换。"""
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return token[:4] + "*" * (len(token) - 8) + token[-4:]


def set_git_remote(workspace_uuid: str, remote_url: str, username: str = "", token: str = "") -> tuple[bool, str]:
    """设置工作区的 Git 远程仓库。

    将 URL + 用户名 + Token 存入工作区配置，并设置 git remote origin。

    Returns:
        (success, message)
    """
    workspace_dir = _get_workspace_dir(workspace_uuid)
    if not workspace_dir:
        return False, "工作区目录不存在"

    try:
        # 更新工作区配置
        from core.config import load_workspace_config, save_workspace_config
        ws_config = load_workspace_config(workspace_uuid)
        if not ws_config:
            return False, "工作区配置不存在"
        ws_config["git_remote_url"] = remote_url
        ws_config["git_username"] = username
        ws_config["git_token"] = token
        if not save_workspace_config(workspace_uuid, ws_config):
            return False, "保存配置失败"

        if not remote_url:
            return True, "远程地址已清除"

        # 设置 git remote origin
        auth_url = _build_auth_url(remote_url, username, token)
        if not (workspace_dir / ".git").is_dir():
            ok, msg = init_workspace_git(workspace_uuid)
            if not ok:
                return False, f"Git 初始化失败: {msg}"

        # 检查是否已有 origin
        result = _git_cmd(workspace_dir, ["remote", "get-url", "origin"], timeout=10)
        if result.returncode == 0:
            # 已存在，更新
            result = _git_cmd(workspace_dir, ["remote", "set-url", "origin", auth_url], timeout=10)
        else:
            # 不存在，添加
            result = _git_cmd(workspace_dir, ["remote", "add", "origin", auth_url], timeout=10)

        if result.returncode != 0:
            return False, f"设置 remote 失败: {result.stderr.strip()}"

        logger.info(f"[workspace-git] 设置远程仓库: {workspace_uuid} → {remote_url}")
        return True, "远程仓库设置成功"

    except Exception as e:
        logger.error(f"[workspace-git] 设置远程仓库失败: {e}")
        return False, f"设置失败: {e}"


def get_git_remote_info(workspace_uuid: str) -> dict:
    """获取工作区 Git 远程仓库信息（脱敏）。

    Returns:
        {
            "remote_url": str,      # 原始 URL（不含凭证）
            "username": str,        # 用户名（明文）
            "has_token": bool,      # 是否配置了 Token
            "token_preview": str,   # Token 前 4 后 4 位
        }
    """
    from core.config import load_workspace_config
    ws_config = load_workspace_config(workspace_uuid) or {}
    remote_url = ws_config.get("git_remote_url", "")
    username = ws_config.get("git_username", "")
    token = ws_config.get("git_token", "")

    return {
        "remote_url": remote_url,
        "username": username,
        "has_token": bool(token),
        "token_preview": _mask_token(token),
    }


def git_pull(workspace_uuid: str) -> tuple[bool, str]:
    """从远程拉取变更。

    Returns:
        (success, message)
    """
    workspace_dir = _get_workspace_dir(workspace_uuid)
    if not workspace_dir:
        return False, "工作区目录不存在"

    try:
        # 同步远程地址到 git config
        _sync_remote_to_git(workspace_uuid)

        result = _git_cmd(workspace_dir, ["pull", "--rebase", "origin", "HEAD"], timeout=120)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            if "No remote" in stderr or "no tracking" in stderr:
                return False, "未配置远程仓库"
            if "Already up to date" in stderr or "Already up-to-date" in stderr:
                return True, "已是最新"
            # 首次拉取（没有上游分支），尝试 fetch + merge
            if "no tracking information" in stderr or "There is no tracking information" in stderr:
                return True, "远程仓库为空或无对应分支"
            return False, f"pull 失败: {stderr or stdout}"
        stdout = result.stdout.strip()
        return True, stdout or "拉取成功"
    except Exception as e:
        logger.error(f"[workspace-git] pull 失败: {e}")
        return False, f"pull 失败: {e}"


def git_push(workspace_uuid: str) -> tuple[bool, str]:
    """推送到远程仓库。

    Returns:
        (success, message)
    """
    workspace_dir = _get_workspace_dir(workspace_uuid)
    if not workspace_dir:
        return False, "工作区目录不存在"

    try:
        _sync_remote_to_git(workspace_uuid)

        # 首次推送用 -u 设置上游跟踪
        result = _git_cmd(workspace_dir, ["push", "-u", "origin", "HEAD"], timeout=120)
        if result.returncode != 0:
            # 已有上游时回退到普通 push
            result = _git_cmd(workspace_dir, ["push", "origin", "HEAD"], timeout=120)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "No remote" in stderr or "no upstream" in stderr:
                return False, "未配置远程仓库"
            if "Everything up-to-date" in stderr:
                return True, "已是最新，无需推送"
            return False, f"push 失败: {stderr}"
        return True, "推送成功"
    except Exception as e:
        logger.error(f"[workspace-git] push 失败: {e}")
        return False, f"push 失败: {e}"


def git_sync(workspace_uuid: str) -> tuple[bool, str]:
    """完整同步：pull → auto_commit → push。

    Returns:
        (success, message)
    """
    from core.config import load_workspace_config
    ws_config = load_workspace_config(workspace_uuid) or {}
    if not ws_config.get("git_remote_url"):
        return False, "未配置远程仓库"

    messages = []

    # 1. pull
    ok, msg = git_pull(workspace_uuid)
    messages.append(f"pull: {msg}")
    if not ok and "未配置远程仓库" not in msg:
        # pull 失败但有冲突，尝试继续
        logger.warning(f"[workspace-git] sync pull 失败: {msg}")

    # 2. auto commit
    ok, msg = auto_commit_workspace(workspace_uuid)
    if ok and msg != "无变更":
        messages.append(f"commit: {msg}")

    # 3. push
    ok, msg = git_push(workspace_uuid)
    messages.append(f"push: {msg}")
    if not ok:
        return False, "; ".join(messages)

    return True, "; ".join(messages)


def _sync_remote_to_git(workspace_uuid: str) -> None:
    """确保 git config 中的 origin URL 与 workspace config 一致。"""
    workspace_dir = _get_workspace_dir(workspace_uuid)
    if not workspace_dir or not (workspace_dir / ".git").is_dir():
        return

    from core.config import load_workspace_config
    ws_config = load_workspace_config(workspace_uuid) or {}
    remote_url = ws_config.get("git_remote_url", "")
    if not remote_url:
        return

    username = ws_config.get("git_username", "")
    token = ws_config.get("git_token", "")
    auth_url = _build_auth_url(remote_url, username, token)

    # 检查当前 remote origin 是否一致
    result = _git_cmd(workspace_dir, ["remote", "get-url", "origin"], timeout=10)
    current_url = result.stdout.strip() if result.returncode == 0 else ""
    if current_url != auth_url:
        if result.returncode == 0:
            _git_cmd(workspace_dir, ["remote", "set-url", "origin", auth_url], timeout=10)
        else:
            _git_cmd(workspace_dir, ["remote", "add", "origin", auth_url], timeout=10)


def get_workspace_git_status(workspace_uuid: str) -> dict:
    """获取工作区 Git 状态。

    Returns:
        {
            "enabled": bool,  # 是否启用
            "initialized": bool,  # 是否已初始化
            "commits": [...],  # 最近提交记录
        }
    """
    workspace_cfg = load_workspace_config(workspace_uuid)
    enabled = bool(workspace_cfg.get("git_enabled", False))
    initialized = is_git_initialized(workspace_uuid)

    commits = []
    if initialized:
        workspace_dir = _get_workspace_dir(workspace_uuid)
        if workspace_dir:
            try:
                result = _git_cmd(
                    workspace_dir,
                    ["log", "--pretty=%h|%ct|%s", "-n", "10"],
                    timeout=10
                )
                if result.returncode == 0:
                    from datetime import datetime
                    for line in result.stdout.strip().splitlines():
                        if "|" not in line:
                            continue
                        parts = line.split("|", 2)
                        if len(parts) < 3:
                            continue
                        short_hash, ts, subject = parts
                        try:
                            date = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
                        except (ValueError, OSError):
                            date = ""
                        commits.append({
                            "hash": short_hash,
                            "date": date,
                            "subject": subject
                        })
            except Exception as e:
                logger.warning(f"[workspace-git] 读取提交记录失败: {e}")

    return {
        "enabled": enabled,
        "initialized": initialized,
        "commits": commits,
    }
