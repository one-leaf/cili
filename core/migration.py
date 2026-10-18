"""Migration utilities for session format upgrade.

Migrates old session format (block-level _valid, _compacted, etc.)
to new format: message-level _meta.valid + block-level _meta storage fields.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from core.config import (
    DATA_DIR,
    GLOBAL_CONFIG_PATH,
    load_global_config,
    save_global_config,
)
from core.fs_utils import atomic_write_json

logger = logging.getLogger(__name__)


def _ensure_block_meta(block: dict) -> dict:
    """确保 block 有 _meta 字典（迁移存储字段时写入），返回之。"""
    if block.get("_meta") is None:
        block["_meta"] = {}
    return block["_meta"]


def migrate_session_file(session_file: Path) -> bool:
    """Migrate a single session file from old format to new format.

    Returns True if migration was performed, False otherwise.
    """
    try:
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load session file {session_file}: {e}")
        return False

    modified = False

    # Migrate messages
    messages = data.get("messages", [])
    for msg in messages:
        if migrate_message(msg):
            modified = True

    # Migrate todos from metadata to independent file
    metadata = data.get("metadata", {})
    if "todos" in metadata:
        session_id = data.get("session_id") or session_file.parent.name
        if migrate_todos_from_metadata(metadata, session_id):
            modified = True

    # Save if modified
    if modified:
        try:
            atomic_write_json(session_file, data)
            logger.info(f"Migrated session: {session_file}")
        except Exception as e:
            logger.error(f"Failed to save migrated session {session_file}: {e}")
            return False

    return modified


def migrate_message(msg: dict) -> bool:
    """Migrate a single message from old format to new format.

    Old format: block-level _valid, _compacted, _output_path, etc.
    New format:
    - 消息级 _meta.valid：整条消息有效性（session.get_valid_messages 只读消息级）
    - block 级 _meta.{compacted, output_path, file_size, truncated, tool_name,
      completed}：工具结果存储字段（_resolve_tool_results 只读 block 级）

    Returns True if migration was performed.
    """
    content = msg.get("content", "")
    if not isinstance(content, list):
        # String content: just check for message-level _valid
        if "_valid" in msg:
            if "_meta" not in msg:
                msg["_meta"] = {}
            msg["_meta"]["valid"] = msg.pop("_valid")
            return True
        return False

    # Check if any block has old-format fields
    needs_migration = False
    message_valid = None  # 任一 block 无效则整条消息无效

    for block in content:
        if not isinstance(block, dict):
            continue

        # Block-level _valid → message-level _meta.valid
        # If any block is invalid, the whole message is invalid
        if "_valid" in block:
            needs_migration = True
            if block.pop("_valid") is False:
                message_valid = False

        # 存储类字段（compacted/output_path/file_size/truncated/tool_name）是
        # block 级 _meta（运行时 _resolve_tool_results 只读 block 级），
        # 必须写回 block["_meta"] 而非消息级，否则外部存储工具结果丢失
        if "_compacted" in block:
            needs_migration = True
            if block.pop("_compacted"):
                _ensure_block_meta(block)["compacted"] = True

        if "_output_path" in block:
            needs_migration = True
            _ensure_block_meta(block)["output_path"] = block.pop("_output_path")

        if "_file_size" in block:
            needs_migration = True
            _ensure_block_meta(block)["file_size"] = block.pop("_file_size")

        if "_truncated" in block:
            needs_migration = True
            _ensure_block_meta(block)["truncated"] = block.pop("_truncated")

        # tool_name → block 级 _meta.tool_name
        if "tool_name" in block:
            needs_migration = True
            _ensure_block_meta(block)["tool_name"] = block.pop("tool_name")

        # _content (old microcompact storage) → remove (content already in external file)
        if "_content" in block:
            needs_migration = True
            block.pop("_content")

        # Convert old field names to Anthropic format
        # tool_call_id → tool_use_id
        if block.get("type") == "tool_result":
            if "tool_call_id" in block and "tool_use_id" not in block:
                block["tool_use_id"] = block.pop("tool_call_id")

        # type: reasoning → thinking
        if block.get("type") == "reasoning":
            block["type"] = "thinking"
            if "text" in block:
                block["thinking"] = block.pop("text")

        # type: tool_call → tool_use
        if block.get("type") == "tool_call":
            block["type"] = "tool_use"
            # arguments (string) → input (dict)
            if "arguments" in block:
                args = block.pop("arguments")
                try:
                    if isinstance(args, str):
                        block["input"] = json.loads(args) if args else {}
                    else:
                        block["input"] = args
                except json.JSONDecodeError:
                    block["input"] = {"_raw": args}

        # _meta.wait_for_user → block 级 _meta.completed (inverted semantics)
        meta = block.get("_meta")
        if meta is not None and "wait_for_user" in meta:
            needs_migration = True
            wfu = meta.pop("wait_for_user")
            # wait_for_user=True → completed=False (waiting)
            # wait_for_user=False → completed=True (answered)
            meta["completed"] = not wfu

        # Recursively migrate tool_result sub-blocks
        if block.get("type") == "tool_result":
            rc = block.get("content", "")
            if isinstance(rc, list):
                for sub in rc:
                    if isinstance(sub, dict):
                        if sub.get("type") == "reasoning":
                            sub["type"] = "thinking"
                            if "text" in sub:
                                sub["thinking"] = sub.pop("text")
                        # 旧格式子块 _valid=False 表示加载失败（如坏图片），
                        # 置消息级 valid=False 而非无条件丢弃，避免"复活"失败内容
                        if sub.pop("_valid", None) is False:
                            message_valid = False
                        sub.pop("_compacted", None)

    # Apply migrated message-level meta
    if needs_migration or message_valid is not None:
        if message_valid is False:
            if "_meta" not in msg:
                msg["_meta"] = {}
            msg["_meta"]["valid"] = False
        return True

    return False


def migrate_todos_from_metadata(metadata: dict, session_id: str) -> bool:
    """Migrate todos from session metadata to independent file.

    Returns True if migration was performed.
    """
    todos = metadata.get("todos", [])
    if not todos:
        return False

    try:
        from core.tools.todo import write_todos
        write_todos(session_id, todos)
        del metadata["todos"]
        logger.info(f"Migrated {len(todos)} todos from session {session_id} to independent file")
        return True
    except Exception as e:
        logger.warning(f"Failed to migrate todos for session {session_id}: {e}")
        return False


def migrate_session_to_new_layout(session_dir: Path) -> bool:
    """Migrate a session dir from old single-file format to the 3-file JSONL layout.

    Old: {session_dir}/index.json 含完整 "messages"。
    New: messages.jsonl（完整历史，UI 数据源）+ index.json（commits 模型视图）
         + meta.json（会话属性）。

    先跑 migrate_session_file（block 级 → 消息级 _meta），再拆 3 文件；
    旧 index.json 备份为 index.json.legacy（copy 而非 rename，保证任意一步
    崩溃后会话仍可读）。已是新布局（含 commits）则直接返回 False。

    Returns:
        True 表示执行了迁移。
    """
    from core.session import (
        MESSAGES_FILE,
        META_FILE,
        SCHEMA_VERSION,
        _commit_for_message,
        _jsonl_line,
        _summary_commit,
        generate_short_id,
        write_jsonl,
    )

    session_dir = Path(session_dir)
    index_file = session_dir / "index.json"
    if not index_file.exists():
        return False

    # 先做 block 级 → 消息级 _meta 迁移，保证拆文件时字段已就位（幂等）
    migrate_session_file(index_file)

    try:
        with open(index_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load session file {index_file}: {e}")
        return False

    if "commits" in data:
        return False  # 已是新布局

    messages = data.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    # 备份旧文件（copy 不 rename：rename 后若中途崩溃会短暂丢失 index.json）
    legacy_file = index_file.with_suffix(".json.legacy")
    if not legacy_file.exists():
        try:
            shutil.copy2(index_file, legacy_file)
        except Exception as e:
            logger.warning(f"Failed to backup legacy file {index_file}: {e}")

    next_seq = 0
    jsonl_lines: list[dict] = []
    commits: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if "_meta" not in msg:
            msg["_meta"] = {}
        meta = msg["_meta"]
        if "id" not in meta:
            meta["id"] = generate_short_id()
        seq = next_seq
        next_seq += 1
        meta["seq"] = seq
        jsonl_lines.append(_jsonl_line(msg, seq))
        # 无效消息从提交视图删除，但 jsonl 完整历史保留（UI 仍可见）
        if meta.get("valid") is False:
            continue
        if meta.get("summary"):
            commits.append(_summary_commit(msg))
        else:
            commits.append(_commit_for_message(msg, seq))

    write_jsonl(session_dir / MESSAGES_FILE, jsonl_lines)
    atomic_write_json(index_file, {
        "schema_version": SCHEMA_VERSION,
        "next_seq": next_seq,
        "commits": commits,
    })
    atomic_write_json(session_dir / META_FILE, {
        "session_id": data.get("session_id") or session_dir.name,
        "name": data.get("name", "New Session"),
        "metadata": data.get("metadata", {}),
    })

    logger.info(f"Migrated session to new layout: {session_dir}")
    return True


def migrate_sessions_dir(sessions_dir: Path) -> int:
    """Migrate all session files in a single sessions directory.

    Returns the number of sessions migrated.
    """
    migrated = 0
    if not sessions_dir.exists():
        return 0

    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue

        session_file = session_dir / "index.json"
        if not session_file.exists():
            continue

        try:
            if migrate_session_file(session_file):
                migrated += 1
            if migrate_session_to_new_layout(session_dir):
                migrated += 1
        except Exception as e:
            logger.warning(f"Failed to migrate session {session_file}: {e}")

    return migrated


def migrate_legacy_data_dir_tools_and_cron() -> None:
    """Migrate old tools/cron.d data from data/cili/ to workspace-local directories.

    Once-off migration: moves {DATA_DIR}/tools/{todo,loop} and {DATA_DIR}/cron.d
    into the system workspace's local directories. Idempotent — skips if already done.
    """
    from core.config import DATA_DIR, get_system_workspace_data_dir

    old_tools_dir = DATA_DIR / "tools"
    old_cron_dir = DATA_DIR / "cron.d"
    system_ws_dir = get_system_workspace_data_dir()

    # Migrate tools (todo, loop)
    if old_tools_dir.exists():
        for tool_name in ["todo", "loop"]:
            src = old_tools_dir / tool_name
            if not src.exists():
                continue
            dst = system_ws_dir / "tools" / tool_name
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                logger.info(f"[migration] Moved {src} → {dst}")
            else:
                # Merge: move files from src to dst
                for f in src.iterdir():
                    dst_file = dst / f.name
                    if not dst_file.exists():
                        shutil.move(str(f), str(dst_file))
                try:
                    src.rmdir()
                except OSError:
                    pass
                logger.info(f"[migration] Merged {src} → {dst}")
        # Try to remove empty old_tools_dir
        try:
            if old_tools_dir.exists() and not any(old_tools_dir.iterdir()):
                old_tools_dir.rmdir()
                logger.info(f"[migration] Removed empty {old_tools_dir}")
        except OSError:
            pass

    # Migrate cron.d
    if old_cron_dir.exists():
        dst_cron = system_ws_dir / "cron.d"
        if not dst_cron.exists():
            shutil.move(str(old_cron_dir), str(dst_cron))
            logger.info(f"[migration] Moved {old_cron_dir} → {dst_cron}")
        else:
            # Merge
            for f in old_cron_dir.iterdir():
                dst_file = dst_cron / f.name
                if not dst_file.exists():
                    shutil.move(str(f), str(dst_file))
            try:
                old_cron_dir.rmdir()
            except OSError:
                pass
            logger.info(f"[migration] Merged {old_cron_dir} → {dst_cron}")


# ─── 接入端多账号迁移（设计 §9）────────────────────────────────────────


def _backup_data_file(path: Path) -> Path | None:
    """备份一个数据文件：``.bak``；已存在则带时间戳后缀，不覆盖更早的回滚副本。"""
    if not path.exists():
        return None
    backup = path.with_name(path.name + ".bak")
    if backup.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
    try:
        shutil.copy2(path, backup)
        return backup
    except OSError as e:
        logger.warning(f"[migration] 备份失败（继续迁移）: {e} ({path})")
        return None


def _legacy_wechat_account_id_from_file() -> str:
    """旧微信单文件凭证里的账号标识：ilink_bot_id → ilink_user_id → "default"。

    与 ``interfaces/wechat/account.py`` 的 ``legacy_account_id()`` 口径一致
    （那边供兼容读用，这边供迁移用；core 不 import interfaces，故各自实现，
    改动时请对照）。
    """
    path = DATA_DIR / "tools" / "wechat" / "account.json"
    if not path.exists():
        return "default"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[migration] 读取微信凭证失败: {e} ({path})")
        return "default"
    if isinstance(data, dict):
        return (
            str(data.get("ilink_bot_id") or "")
            or str(data.get("ilink_user_id") or "")
            or "default"
        )
    return "default"


def _first_account_id(section: dict) -> str:
    """已迁移段里第一个账号的 id（旧扁平状态属于迁移前的唯一账号）。"""
    accounts = section.get("accounts")
    if isinstance(accounts, list):
        for a in accounts:
            if isinstance(a, dict) and str(a.get("id") or ""):
                return str(a.get("id"))
    return ""


def migrate_interfaces_to_multi_account() -> None:
    """接入端多账号一次性迁移（设计 §9）。

    - setting.json 的 qq / wechat 段：扁平对象 → ``{"accounts": [...]}``
    - tools/qq/state.json、tools/wechat/state.json：扁平 → ``{"accounts": {...}}``
    - tools/wechat/account.json → tools/wechat/accounts/{id}.json
    - tools/qq/ws_sessions.json → tools/qq/{app_id}/ws_sessions.json

    幂等：段/文件出现 ``accounts`` 键、或目标文件已存在即跳过。所有被改写
    或删除的文件先备份。

    账号 id（§3.1）：QQ 取旧 ``app_id``；微信取旧凭证里的 ``ilink_bot_id``
    （回退 ``ilink_user_id``，再回退 ``"default"``）。配置段已迁移而状态文件
    还是旧形态时（前次迁移中途失败），沿用第一个账号的 id，保证状态归到
    正确的账号名下。
    """
    config = load_global_config()
    changed = False

    # ---- QQ：账号 id 取旧 app_id ----
    qq_section = config.get("qq")
    qq_account_id = ""
    if isinstance(qq_section, dict):
        if isinstance(qq_section.get("accounts"), list):
            qq_account_id = _first_account_id(qq_section)
        elif qq_section:
            qq_account_id = str(qq_section.get("app_id") or "") or secrets.token_hex(4)
            config["qq"] = {"accounts": [{
                "id": qq_account_id,
                "label": "",
                "enabled": bool(qq_section.get("enabled")),
                "default_workspace_uuid": "",
                "workspace_locked": False,
                "app_id": str(qq_section.get("app_id") or ""),
                "secret": str(qq_section.get("secret") or ""),
                "msg_format": str(qq_section.get("msg_format") or "markdown"),
            }]}
            changed = True
            if qq_section.get("use_sandbox") is False:
                logger.warning(
                    "[migration] 旧配置 use_sandbox=false（正式环境）。API 域名不再按账号配置，"
                    "请在启动环境设置 QQ_API_BASE=https://api.sgroup.qq.com"
                )

    # ---- 微信：账号 id 取旧凭证里的 ilink_bot_id ----
    wechat_account_id = ""
    wechat_section = config.get("wechat")
    if isinstance(wechat_section, dict):
        if isinstance(wechat_section.get("accounts"), list):
            wechat_account_id = _first_account_id(wechat_section)
        elif wechat_section:
            wechat_account_id = _legacy_wechat_account_id_from_file()
            config["wechat"] = {"accounts": [{
                "id": wechat_account_id,
                "label": "",
                "enabled": bool(wechat_section.get("enabled")),
                "default_workspace_uuid": "",
                "workspace_locked": False,
            }]}
            changed = True

    if changed:
        _backup_data_file(GLOBAL_CONFIG_PATH)
        if not save_global_config(config):
            logger.error("[migration] setting.json 多账号迁移写回失败，状态/凭证迁移跳过")
            return
        logger.info(
            f"[migration] setting.json 的 qq/wechat 段已迁移为账号列表"
            f"（qq={qq_account_id or '无'}, wechat={wechat_account_id}）"
        )

    _migrate_qq_state_file(qq_account_id)
    _migrate_wechat_state_file(wechat_account_id)
    _migrate_wechat_account_file(wechat_account_id)
    _migrate_qq_ws_sessions()


def _migrate_qq_state_file(account_id: str) -> None:
    """旧 ``{owner_openid, workspace_uuid, session_id}`` → 两级结构（§9.1）。"""
    if not account_id:
        return
    path = DATA_DIR / "tools" / "qq" / "state.json"
    data = _read_json_if_exists(path)
    if data is None or "accounts" in data:
        return
    owner = str(data.get("owner_openid") or "")
    chats: dict = {}
    if owner:
        chats[owner] = {
            "workspace_uuid": str(data.get("workspace_uuid") or ""),
            "session_id": str(data.get("session_id") or ""),
        }
    _backup_data_file(path)
    atomic_write_json(path, {"accounts": {account_id: {"owner": owner, "chats": chats}}})
    logger.info(f"[migration] QQ 状态已迁移为账号/聊天两级（账号 {account_id}）")


def _migrate_wechat_state_file(account_id: str) -> None:
    """旧 ``{owner_user_id, workspace_uuid, session_id}`` → 两级结构（§9.1）。"""
    if not account_id:
        return
    path = DATA_DIR / "tools" / "wechat" / "state.json"
    data = _read_json_if_exists(path)
    if data is None or "accounts" in data:
        return
    owner = str(data.get("owner_user_id") or "")
    chats: dict = {}
    if owner:
        chats[owner] = {
            "workspace_uuid": str(data.get("workspace_uuid") or ""),
            "session_id": str(data.get("session_id") or ""),
        }
    _backup_data_file(path)
    atomic_write_json(path, {"accounts": {account_id: {"owner": owner, "chats": chats}}})
    logger.info(f"[migration] 微信状态已迁移为账号/聊天两级（账号 {account_id}）")


def _migrate_wechat_account_file(account_id: str) -> None:
    """旧 tools/wechat/account.json（扁平）→ accounts/{id}.json（设计 §5.1）。"""
    if not account_id:
        return
    old_path = DATA_DIR / "tools" / "wechat" / "account.json"
    if not old_path.exists():
        return
    new_path = DATA_DIR / "tools" / "wechat" / "accounts" / f"{account_id}.json"
    if new_path.exists():
        return
    data = _read_json_if_exists(old_path)
    if data is None:
        return
    _backup_data_file(old_path)
    atomic_write_json(new_path, data)
    old_path.unlink()
    logger.info(f"[migration] 微信凭证已迁移：{old_path} → {new_path}")


def _migrate_qq_ws_sessions() -> None:
    """旧 tools/qq/ws_sessions.json（按 app_id 分键的单文件）→ 每账号一个目录。

    SDK 的 WSSessionStore 构造时整读、保存时整写，多账号共用同一文件会
    last-write-wins 互相覆盖。丢失的代价只是不能 Resume（重新 Identify），
    但迁移成本极低，仍然迁。按 app_id 拆分（旧账号 id 恰为 app_id）。
    """
    old_path = DATA_DIR / "tools" / "qq" / "ws_sessions.json"
    if not old_path.exists():
        return
    data = _read_json_if_exists(old_path)
    if not isinstance(data, dict) or not data:
        return
    moved = 0
    for app_id, entry in data.items():
        target = DATA_DIR / "tools" / "qq" / str(app_id) / "ws_sessions.json"
        if target.exists():
            continue
        atomic_write_json(target, {str(app_id): entry})
        moved += 1
    if moved:
        _backup_data_file(old_path)
        old_path.unlink()
        logger.info(f"[migration] QQ ws 会话已按账号分目录（{moved} 个）")


def _read_json_if_exists(path: Path) -> dict | None:
    """读 JSON 文件；不存在返回 None，损坏/非对象也返回 None（由调用方跳过）。"""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[migration] 读取失败，跳过迁移: {e} ({path})")
        return None
    return data if isinstance(data, dict) else None
