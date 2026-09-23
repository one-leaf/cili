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


def auto_commit_workspace(workspace_uuid: str, session_id: str = "") -> tuple[bool, str]:
    """自动提交工作区变更。

    检查是否有文件变更，如有则使用 LLM 生成摘要并提交。

    Args:
        workspace_uuid: 工作区 UUID
        session_id: 当前会话 ID（用于日志）

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
        return True, summary

    except Exception as e:
        logger.error(f"[workspace-git] 自动提交失败: {e}")
        return False, f"自动提交失败: {e}"


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
