"""已登记的迁移步骤（顺序即执行顺序；新增破坏性变更在此追加）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

def migrate_placeholder_meta(workspaces_json: Path, get_data_dir) -> int:
    """会话占位标记 ``completed`` → ``ask_user_completed``（旧会话一次性重写）。

    block 级 _meta 实际存在 index.json 的 commits[].blocks；messages.jsonl
    只存内容历史。两处都扫一遍兼容更老的格式。幂等（新键存在即跳过）。
    """
    changed = 0
    try:
        entries = json.loads(Path(workspaces_json).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(entries, list):
        return 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sessions_dir = Path(get_data_dir(entry.get("uuid", ""))) / "sessions"
        if not sessions_dir.is_dir():
            continue
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            if _rewrite_index_json(session_dir / "index.json"):
                changed += 1
            if _rewrite_messages_jsonl(session_dir / "messages.jsonl"):
                changed += 1
    return changed


def _rewrite_block_meta(meta: dict) -> bool:
    if not isinstance(meta, dict) or "completed" not in meta:
        return False
    if "ask_user_completed" in meta:
        return False
    meta["ask_user_completed"] = bool(meta["completed"])
    return True


def _rewrite_index_json(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    commits = data.get("commits") if isinstance(data, dict) else None
    if not isinstance(commits, list):
        return False
    touched = False
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        blocks = commit.get("blocks")
        if not isinstance(blocks, dict):
            continue
        for meta in blocks.values():
            if _rewrite_block_meta(meta):
                touched = True
    if touched:
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    return touched


def _rewrite_messages_jsonl(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    out: list[str] = []
    touched = False
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        content = record.get("content") if isinstance(record, dict) else None
        if not isinstance(content, list):
            out.append(line)
            continue
        record_touched = False
        for block in content:
            if isinstance(block, dict) and _rewrite_block_meta(block.get("_meta")):
                record_touched = True
        if record_touched:
            touched = True
            out.append(json.dumps(record, ensure_ascii=False))
        else:
            out.append(line)
    if touched:
        atomic_write_text(path, "\n".join(out) + "\n")
    return touched


def migrate_provider_interface_type(global_config_path: Path) -> bool:
    """setting.json providers 的 `interface_type` 键 → `protocol`（§4 契约统一）。"""
    path = Path(global_config_path)
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # 读不出不等于不适用：抛出以便 run_pending 不记入版本文件，下次启动重试
        raise RuntimeError(f"无法读取 {path}: {e}") from e
    if not isinstance(raw, dict):
        return False
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return False
    changed = False
    for entry in providers.values():
        if not isinstance(entry, dict):
            continue
        if "interface_type" in entry:
            entry.setdefault("protocol", entry.pop("interface_type"))
            changed = True
    if changed:
        atomic_write_text(
            Path(global_config_path),
            json.dumps(raw, ensure_ascii=False, indent=2),
        )
    return changed


def migrate_chatgpt_oauth_naming(global_config_path: Path) -> bool:
    """`chatgpt-siwc` provider 键与 `siwc:` 凭证前缀 → `chatgpt-oauth`（§4）。"""
    path = Path(global_config_path)
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RuntimeError(f"无法读取 {path}: {e}") from e
    if not isinstance(raw, dict):
        return False

    changed = False
    providers = raw.get("providers")
    if isinstance(providers, dict):
        if "chatgpt-siwc" in providers:
            # 目标键已存在时不能覆盖用户新配置：只迁移、丢弃旧键前先合并模型
            old = providers["chatgpt-siwc"]
            if "chatgpt-oauth" in providers:
                target = providers["chatgpt-oauth"]
                if isinstance(target, dict) and isinstance(old, dict):
                    old_models = old.get("models") or {}
                    if not isinstance(target.get("models"), dict):
                        target["models"] = {}
                    for k, v in old_models.items():
                        target["models"].setdefault(k, v)
                    providers.pop("chatgpt-siwc")
                changed = True
            else:
                providers["chatgpt-oauth"] = providers.pop("chatgpt-siwc")
                changed = True
        for entry in providers.values():
            if not isinstance(entry, dict):
                continue
            cred = str(entry.get("credential") or "")
            if cred.startswith("siwc:"):
                entry["credential"] = "chatgpt-oauth:" + cred[len("siwc:"):]
                changed = True
            backend = entry.get("backend")
            if backend == "chatgpt-siwc":
                entry["backend"] = "chatgpt-oauth"
                changed = True

    agents = raw.get("agents")
    if isinstance(agents, dict):
        for name, agent in agents.items():
            if not isinstance(agent, dict):
                continue
            model = str(agent.get("model") or "")
            if model.startswith("chatgpt-siwc/"):
                agent["model"] = "chatgpt-oauth/" + model[len("chatgpt-siwc/"):]
                changed = True

    if changed:
        atomic_write_text(path, json.dumps(raw, ensure_ascii=False, indent=2))
    return changed


def migrate_tui_channel(workspaces_json: Path, get_data_dir) -> int:
    """旧会话 metadata 的通道/档案名 `tui` → `cli`（§4 契约）。"""
    changed = 0
    try:
        entries = json.loads(Path(workspaces_json).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(entries, list):
        return 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sessions_dir = Path(get_data_dir(entry.get("uuid", ""))) / "sessions"
        if not sessions_dir.is_dir():
            continue
        for meta_path in sessions_dir.glob("*/meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            metadata = meta.get("metadata")
            if not isinstance(metadata, dict):
                continue
            touched = False
            for key in ("channel", "interface"):
                if metadata.get(key) == "tui":
                    metadata[key] = "cli"
                    touched = True
            if touched:
                atomic_write_text(
                    meta_path, json.dumps(meta, ensure_ascii=False, indent=2)
                )
                changed += 1
    return changed
