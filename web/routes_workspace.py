"""Workspace + Session + 子代理执行日志 路由域。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from core.channels import (
    API_CREATE_CHANNEL_IDS,
    MARKER_ACCOUNT,
    MARKER_CHANNEL,
    marker_metadata,
)
from core.compression import truncate_middle, truncate_result
from core.config import (
    PROJECT_ROOT,
    find_workspace_entry,
    get_workspace_data_dir,
    get_workspace_meta,
    remove_workspace_entry,
    save_workspace_meta,
    sessions_dir_for_data_dir,
    validate_workspace_name,
    workspaces_root,
)
from core.fs_utils import atomic_write_json, load_json_or_backup
from core.project_instructions import PROJECT_INSTRUCTION_FILES
from core.session import (
    MESSAGES_FILE,
    META_FILE,
    SessionStore,
    load_history_messages,
    load_history_meta,
    preview_from_messages,
    read_jsonl,
    read_meta,
    session_display_name,
    session_key,
)
from core.teen_mode import deny_message, get_teen_mode
from core.time_utils import now_str
from core.workspace_relocation import (
    MigrationError,
    cancel_workspace_migration,
    get_migration_status,
    start_workspace_migration,
)
from core.workspace_relocation import (
    preflight_workspace_migration as preflight_workspace_relocation,
)
from web.deps import (
    _SAFE_ID_RE,
    _get_workspace_info,
    _list_all_workspaces,
    _new_short_id,
    _sessions_lock,
    csrf_protect,
    registry,
    require_workspace_data_dir,
    teen_guard,
    validate_exec_id,
    validate_session_id,
    validate_workspace_uuid,
)
from web.goal_runner import stop_goal_runner

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------- Models ----------

class CreateSessionRequest(BaseModel):
    name: str = "New Session"
    # 会话归属通道：Web 界面默认 web；CLI 客户端显式传 cli
    channel: str = "web"


class CreateWorkspaceRequest(BaseModel):
    name: str
    directory: str = ""


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    directory: str | None = None


class WorkspaceMigrationRequest(BaseModel):
    target_path: str


class RenameSessionRequest(BaseModel):
    name: str


class SetHiddenRequest(BaseModel):
    hidden: bool


class BatchSessionRequest(BaseModel):
    session_ids: list[str]
    action: str  # "hide", "unhide", "delete"


class GitSettingsRequest(BaseModel):
    git_enabled: bool
    git_remote_url: str | None = None
    git_username: str | None = None
    git_token: str | None = None


# ----- Workspaces -----

@router.get("/api/workspaces")
async def list_workspaces():
    """List all workspaces.

    青少年模式：受限工作区带 ``locked: true``，且不返回目录路径
    （避免暴露家长的文件组织）。
    """
    tm = get_teen_mode()
    workspaces = []
    for ws in _list_all_workspaces():
        entry = dict(ws)
        locked = tm.workspace_access(entry.get("uuid", "")) == "locked"
        entry["locked"] = locked
        if locked:
            entry["directory"] = ""
        workspaces.append(entry)
    return {"workspaces": workspaces}


@router.post("/api/workspaces",
             dependencies=[Depends(teen_guard("config_update"))])
async def create_workspace(request: CreateWorkspaceRequest):
    """Create a new workspace.

    Args:
        name: Display name for the workspace
        directory: Working directory path (optional, defaults to the configured workspaces root)
    """
    # Validate name
    err = validate_workspace_name(request.name)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Generate UUID for data directory
    workspace_uuid = _new_short_id()

    # Default directory: use workspace/ subdir if not specified
    if request.directory:
        workspace_dir = os.path.abspath(request.directory)
    else:
        workspace_dir = str(workspaces_root() / request.name)

    # Create working directory
    os.makedirs(workspace_dir, exist_ok=True)

    # Save workspace config (only metadata, no model config)
    now = now_str()
    ws_config = {
        "workspace_name": request.name,
        "directory": workspace_dir,
        "created_at": now,
        "updated_at": now,
        # 新建工作区默认开启版本管理与记忆
        "git_enabled": True,
        "memory_enabled": True,
    }

    save_workspace_meta(workspace_uuid, ws_config)

    # Create .cili data directory (workspace-local, mirrors directory in index)
    ws_data_dir = get_workspace_data_dir(workspace_uuid)
    ws_data_dir.mkdir(parents=True, exist_ok=True)

    # Create sessions directory
    sessions_dir = sessions_dir_for_data_dir(ws_data_dir)
    sessions_dir.mkdir(exist_ok=True)

    # 默认开启版本管理：立即初始化 Git 仓库（子进程阻塞，放线程执行；
    # 失败不阻断创建——首个回合结束的自动提交会再次尝试）
    try:
        from core.workspace_git import init_workspace_git, is_git_initialized
        if not await asyncio.to_thread(is_git_initialized, workspace_uuid):
            ok, msg = await asyncio.to_thread(init_workspace_git, workspace_uuid)
            if not ok:
                logger.warning(f"Workspace created, git init failed: {workspace_uuid}: {msg}")
    except Exception as e:
        logger.warning(f"Workspace created, git init errored: {workspace_uuid}: {e}")

    return {
        "uuid": workspace_uuid,
        "name": request.name,
        "directory": workspace_dir,
    }


def _cleanup_runners_for_workspace(workspace_uuid: str) -> None:
    """Remove runners belonging to the given workspace from the registry.

    Must be called under _sessions_lock.
    """
    registry.remove_workspace(workspace_uuid)


@router.put("/api/workspaces/{workspace_uuid}",
            dependencies=[Depends(teen_guard("config_update"))])
async def update_workspace(workspace_uuid: str, request: UpdateWorkspaceRequest):
    """Update workspace name and/or directory."""
    validate_workspace_uuid(workspace_uuid)
    if workspace_uuid == "system":
        raise HTTPException(status_code=403, detail="System workspace cannot be modified")
    if not find_workspace_entry(workspace_uuid):
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Load existing config
    config = get_workspace_meta(workspace_uuid)
    if not config:
        raise HTTPException(status_code=404, detail="Workspace config not found")

    # Update name if provided
    if request.name is not None:
        err = validate_workspace_name(request.name)
        if err:
            raise HTTPException(status_code=400, detail=err)
        config["workspace_name"] = request.name

    # 目录变更必须走「数据迁移」：直接改索引会把数据留在旧目录
    if request.directory is not None and request.directory.strip():
        new_dir = os.path.normcase(os.path.abspath(request.directory))
        current_dir = os.path.normcase(os.path.abspath(str(config.get("directory") or "")))
        if new_dir != current_dir:
            raise HTTPException(
                status_code=400,
                detail="移动工作区目录请使用「数据迁移」功能",
            )

    config["updated_at"] = now_str()

    if not save_workspace_meta(workspace_uuid, config):
        raise HTTPException(status_code=500, detail="Failed to save config")

    return {"success": True, "workspace": config}


# ----- Workspace data migration -----

@router.post("/api/workspaces/{workspace_uuid}/migration/preflight",
             dependencies=[Depends(teen_guard("config_update")), Depends(csrf_protect)])
async def workspace_migration_preflight(workspace_uuid: str,
                                        request: WorkspaceMigrationRequest):
    """只读预检：目标合法性、体积、文件数、同/跨卷与可用空间。"""
    validate_workspace_uuid(workspace_uuid)
    get_teen_mode().guard_workspace(workspace_uuid, source="web")
    try:
        return await asyncio.to_thread(
            preflight_workspace_relocation, workspace_uuid, request.target_path
        )
    except MigrationError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/api/workspaces/{workspace_uuid}/migration",
             dependencies=[Depends(teen_guard("config_update")), Depends(csrf_protect)])
async def workspace_migration_start(workspace_uuid: str,
                                    request: WorkspaceMigrationRequest):
    """启动后台迁移；立即返回迁移 ID 与预检结果。"""
    validate_workspace_uuid(workspace_uuid)
    get_teen_mode().guard_workspace(workspace_uuid, source="web")
    try:
        return await asyncio.to_thread(
            start_workspace_migration, workspace_uuid, request.target_path
        )
    except MigrationError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.get("/api/workspaces/{workspace_uuid}/migration")
async def workspace_migration_status(workspace_uuid: str):
    """当前/最近一次迁移状态。"""
    validate_workspace_uuid(workspace_uuid)
    get_teen_mode().guard_workspace(workspace_uuid, source="web")
    return get_migration_status(workspace_uuid)


@router.delete("/api/workspaces/{workspace_uuid}/migration",
               dependencies=[Depends(teen_guard("config_update")), Depends(csrf_protect)])
async def workspace_migration_cancel(workspace_uuid: str):
    """取消迁移（仅复制/校验阶段）。"""
    validate_workspace_uuid(workspace_uuid)
    get_teen_mode().guard_workspace(workspace_uuid, source="web")
    if not cancel_workspace_migration(workspace_uuid):
        raise HTTPException(status_code=409, detail="当前阶段不可取消")
    return {"success": True}


@router.delete("/api/workspaces/{workspace_uuid}",
               dependencies=[Depends(teen_guard("workspace_unregister", hard=True))])
async def unregister_workspace(workspace_uuid: str):
    """从索引注销工作区（保留 .cili 数据目录与工作区文件）。"""
    validate_workspace_uuid(workspace_uuid)
    if workspace_uuid == "system":
        raise HTTPException(status_code=403, detail="System workspace cannot be modified")
    if not find_workspace_entry(workspace_uuid):
        raise HTTPException(status_code=404, detail="Workspace not found")
    if (get_workspace_meta(workspace_uuid) or {}).get("org_managed"):
        raise HTTPException(
            status_code=403,
            detail="组织托管工作区不能在本地注销，请先在设置中退出组织",
        )
    with _sessions_lock:
        _cleanup_runners_for_workspace(workspace_uuid)
    remove_workspace_entry(workspace_uuid)
    logger.info(f"Workspace config removed: {workspace_uuid} (data and files kept)")
    return {"success": True}


# ----- Git Version Control -----

@router.put("/api/workspaces/{workspace_uuid}/git/settings")
async def update_git_settings(workspace_uuid: str, request: GitSettingsRequest):
    """更新工作区 Git 版本管理设置（开关 + 远程仓库）。"""
    validate_workspace_uuid(workspace_uuid)
    if workspace_uuid == "system":
        raise HTTPException(status_code=403, detail="System workspace cannot be modified")
    if not find_workspace_entry(workspace_uuid):
        raise HTTPException(status_code=404, detail="Workspace not found")
    if (get_workspace_meta(workspace_uuid) or {}).get("org_managed"):
        raise HTTPException(
            status_code=403,
            detail="组织托管工作区的 Git 设置由组织统一管理",
        )

    config = get_workspace_meta(workspace_uuid)
    config["git_enabled"] = request.git_enabled
    if not save_workspace_meta(workspace_uuid, config):
        raise HTTPException(status_code=500, detail="Failed to save config")

    # 如果启用且未初始化，自动初始化 Git 仓库
    init_result = None
    if request.git_enabled:
        from core.workspace_git import init_workspace_git, is_git_initialized
        if not await asyncio.to_thread(is_git_initialized, workspace_uuid):
            ok, msg = await asyncio.to_thread(init_workspace_git, workspace_uuid)
            init_result = {"success": ok, "message": msg}

    # 设置远程仓库（三个字段任一变化则更新）
    remote_result = None
    if request.git_remote_url is not None or request.git_username is not None or request.git_token is not None:
        from core.workspace_git import get_git_remote_info, set_git_remote
        remote_url = request.git_remote_url or ""
        username = request.git_username or ""
        token = request.git_token or ""
        ok, msg = await asyncio.to_thread(
            set_git_remote, workspace_uuid, remote_url, username, token
        )
        remote_result = {"success": ok, "message": msg}
        if ok:
            remote_result["remote_info"] = await asyncio.to_thread(
                get_git_remote_info, workspace_uuid
            )

    logger.info(f"Git settings updated: {workspace_uuid} enabled={request.git_enabled}")
    return {
        "success": True,
        "git_enabled": request.git_enabled,
        "init_result": init_result,
        "remote_result": remote_result,
    }


@router.get("/api/workspaces/{workspace_uuid}/git/status")
async def get_git_status(workspace_uuid: str):
    """获取工作区 Git 状态（含远程仓库信息）。"""
    validate_workspace_uuid(workspace_uuid)
    if not find_workspace_entry(workspace_uuid):
        raise HTTPException(status_code=404, detail="Workspace not found")

    from core.workspace_git import get_git_remote_info, get_workspace_git_status
    status = await asyncio.to_thread(get_workspace_git_status, workspace_uuid)
    status["remote_info"] = await asyncio.to_thread(get_git_remote_info, workspace_uuid)
    return status


@router.post("/api/workspaces/{workspace_uuid}/git/pull")
async def git_pull(workspace_uuid: str):
    """从远程拉取变更。"""
    validate_workspace_uuid(workspace_uuid)
    if not find_workspace_entry(workspace_uuid):
        raise HTTPException(status_code=404, detail="Workspace not found")

    from core.workspace_git import git_pull as do_pull
    ok, msg = await asyncio.to_thread(do_pull, workspace_uuid)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg}


@router.post("/api/workspaces/{workspace_uuid}/git/push")
async def git_push(workspace_uuid: str):
    """推送到远程仓库。"""
    validate_workspace_uuid(workspace_uuid)
    if not find_workspace_entry(workspace_uuid):
        raise HTTPException(status_code=404, detail="Workspace not found")

    from core.workspace_git import git_push as do_push
    ok, msg = await asyncio.to_thread(do_push, workspace_uuid)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg}


# ----- Sessions -----

def _scan_session_brief(session_dir: Path) -> tuple[str, int]:
    """回退扫描 jsonl：取最后一条含文本的 user 消息预览 + 消息数。

    仅用于升级前写入的 meta.json（缺 preview/message_count 字段）时一次性回退，
    不回写磁盘，由下一次 checkpoint save() 自然收敛。
    """
    preview = ""
    message_count = 0
    for line in read_jsonl(session_dir / MESSAGES_FILE):
        message_count += 1
        if line.get("role") == "user":
            content = line.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        preview = block.get("text", "")[:200]
                        break
            elif isinstance(content, str):
                preview = content[:200]
    return preview, message_count


@router.get("/api/workspaces/{workspace_uuid}/sessions")
async def list_sessions(workspace_uuid: str, ws_dir: Path = Depends(require_workspace_data_dir)):
    """List all sessions in a workspace (纯 meta 读，不读消息正文、不触发迁移)。"""
    sessions_dir = sessions_dir_for_data_dir(ws_dir)
    if not sessions_dir.exists():
        return {"sessions": []}

    # 逐会话读 meta 属阻塞 IO（会话多时可达数百 ms），放线程里避免卡住事件循环
    session_list = await asyncio.to_thread(_collect_session_list, sessions_dir)

    # 先排序再剥离内部字段：把 pop 写进 sort 的 key 里带副作用，难读
    session_list.sort(key=lambda s: s.get("_mtime", 0), reverse=True)
    for s in session_list:
        s.pop("_mtime", None)
    return {"sessions": session_list}


def _collect_session_list(sessions_dir: Path) -> list[dict]:
    """遍历会话目录收集 meta 摘要（同步实现，供 list_sessions 丢进线程执行）。"""
    session_list = []
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue
        index_file = session_dir / "index.json"
        if not index_file.exists():
            continue
        meta_file = session_dir / META_FILE
        try:
            if meta_file.exists():
                # 新格式：纯 meta 读（preview/message_count 已由 checkpoint 下沉）
                meta = read_meta(session_dir)
                metadata = meta.get("metadata", {})
                mtime = meta_file.stat().st_mtime
                preview = meta.get("preview", "")
                message_count = meta.get("message_count", 0)
                if "preview" not in meta or "message_count" not in meta:
                    preview, message_count = _scan_session_brief(session_dir)
                session_list.append({
                    "session_id": meta.get("session_id") or session_dir.name,
                    "name": meta.get("name", "Unnamed"),
                    "created_at": metadata.get("created_at", ""),
                    "updated_at": metadata.get("updated_at", ""),
                    "message_count": message_count,
                    "agent_count": metadata.get("agent_count", metadata.get("subagent_count", 0)),
                    "preview": preview,
                    "display_name": session_display_name(meta.get("name", ""), preview),
                    "hidden": metadata.get("hidden", False),
                    "channel": metadata.get(MARKER_CHANNEL, ""),
                    "channel_account": metadata.get(MARKER_ACCOUNT, ""),
                    "_mtime": mtime,
                })
            else:
                # 旧单文件格式（无 meta.json）：直读 index.json 的 name/metadata，只读不迁移
                old = load_json_or_backup(index_file, {}) or {}
                old_messages = old.get("messages", [])
                if not isinstance(old_messages, list):
                    old_messages = []
                metadata = old.get("metadata", {})
                session_list.append({
                    "session_id": old.get("session_id") or session_dir.name,
                    "name": old.get("name", "Unnamed"),
                    "created_at": metadata.get("created_at", ""),
                    "updated_at": metadata.get("updated_at", ""),
                    "message_count": len(old_messages),
                    "agent_count": metadata.get("agent_count", metadata.get("subagent_count", 0)),
                    "preview": preview_from_messages(old_messages),
                    "display_name": session_display_name(
                        old.get("name", ""), preview_from_messages(old_messages)
                    ),
                    "hidden": metadata.get("hidden", False),
                    "channel": metadata.get(MARKER_CHANNEL, ""),
                    "channel_account": metadata.get(MARKER_ACCOUNT, ""),
                    "_mtime": index_file.stat().st_mtime,
                })
        except Exception as e:
            logger.error(f"Failed to read session {index_file}: {e}")

    return session_list


@router.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}")
async def get_session(
    workspace_uuid: str,
    session_id: str,
    limit: int | None = None,
    offset: int = 0,
    ws_dir: Path = Depends(require_workspace_data_dir)
):
    """Get a specific session with messages.

    Args:
        limit: 返回消息数量限制，None 表示返回全部
        offset: 从末尾向前偏移的消息数（0 = 从末尾开始）

    默认行为：limit=None 返回所有消息（向后兼容）
    分页加载：limit=50, offset=0 返回最近 50 条消息
    """
    validate_session_id(session_id)
    session_dir = sessions_dir_for_data_dir(ws_dir) / session_id
    index_file = session_dir / "index.json"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        # UI 直接读 messages.jsonl 完整历史（含压缩前所有消息），合并 _meta
        meta = load_history_meta(session_dir)
        messages = load_history_messages(session_dir)

        total_count = len(messages)

        # 分页处理
        if limit is not None:
            # offset=0 表示最后 limit 条，offset=limit 表示再往前 limit 条
            end_idx = total_count - offset
            start_idx = max(0, end_idx - limit)
            messages = messages[start_idx:end_idx]
            has_more = start_idx > 0
        else:
            has_more = False

        # 从外部文件按需注入工具结果内容（前端渲染需要）；
        # 同步读多文件，放线程池避免阻塞事件循环（审查 P2）
        await asyncio.to_thread(_resolve_tool_results_for_session, messages, session_dir)

        # 任务清单存放在独立的 todo 文件里（不随会话 metadata 落盘）：
        # 切换会话/刷新页面后前端要从这里恢复，不能只靠 metadata.todos（旧格式）
        from core.todo_store import read_todos
        todos = read_todos(session_id, workspace_uuid)

        return {
            "session_id": meta.get("session_id", session_id),
            "name": meta.get("name", "New Session"),
            "metadata": meta.get("metadata", {}),
            "todos": todos,
            "messages": messages,
            "total_count": total_count,
            "has_more": has_more,
        }
    except Exception as e:
        logger.error(f"Failed to read session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to read session")


@router.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/export")
async def export_session(workspace_uuid: str, session_id: str,
                         ws_dir: Path = Depends(require_workspace_data_dir)):
    """导出会话为可再次导入的 JSON（完整历史，工具结果内容内联）。"""
    validate_session_id(session_id)
    session_dir = sessions_dir_for_data_dir(ws_dir) / session_id
    if not (session_dir / "index.json").exists():
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        meta = await asyncio.to_thread(load_history_meta, session_dir)
        messages = await asyncio.to_thread(load_history_messages, session_dir)
        await asyncio.to_thread(_resolve_tool_results_for_session, messages, session_dir)
    except Exception as e:
        logger.error(f"Failed to export session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="导出会话失败")
    # 工具结果已内联；去掉外部文件引用，避免导入后指向新会话目录里不存在的文件
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("_meta"), dict):
                block["_meta"].pop("output_path", None)
    return {
        "format": "cili-session",
        "version": 1,
        "name": meta.get("name", "会话"),
        "created_at": (meta.get("metadata") or {}).get("created_at", ""),
        "messages": messages,
    }


@router.post("/api/workspaces/{workspace_uuid}/sessions/import")
async def import_session(workspace_uuid: str, payload: dict = Body(...),
                         ws_dir: Path = Depends(require_workspace_data_dir)):
    """从导出文件导入会话：新建会话并写回历史消息。"""
    raw_messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(raw_messages, list):
        raise HTTPException(status_code=400, detail="导入文件缺少 messages")
    if len(raw_messages) > 5000:
        raise HTTPException(status_code=400, detail="消息过多（最多 5000 条）")
    cleaned: list[tuple[str, object, dict | None]] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="消息格式非法")
        role = str(item.get("role") or "")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, (str, list)):
            raise HTTPException(status_code=400, detail="消息格式非法（role/content）")
        if isinstance(content, list):
            # 外部工具结果文件不随导入复制；内容已内联在 block 里
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("_meta"), dict):
                    block["_meta"].pop("output_path", None)
        meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else None
        if meta is not None:
            # jsonl 序号属于原会话文件，导入后要重新分配，否则新消息不会落盘
            meta = {k: v for k, v in meta.items() if k != "seq"}
        cleaned.append((role, content, meta))
    name = str(payload.get("name") or "").strip()[:100] or "导入的会话"
    sessions_dir = sessions_dir_for_data_dir(ws_dir)

    def _do_import() -> SessionStore:
        session = SessionStore.create_new_session(
            sessions_dir, name, metadata=marker_metadata("web"))
        for role, content, meta in cleaned:
            session.add_message(role, content, meta=meta)
        session.save(force=True)
        return session

    try:
        session = await asyncio.to_thread(_do_import)
    except Exception as e:
        logger.error(f"Failed to import session: {e}")
        raise HTTPException(status_code=500, detail="导入会话失败")
    return session.to_dict()


def _is_workflow_step_prompt(content) -> bool:
    """工作流步骤的 user 提示词（由 WorkflowRunner 生成）；兼容旧版前缀。"""
    from core.text_extract import message_text
    text = message_text(content, strip=False).strip()
    return (text.startswith("# 工作流「") or text.startswith("# 工作流\"")
            or text.startswith("# 任务「") or text.startswith("# 任务\""))


def _step_session_execution(exec_id: str, sessions_dir: Path) -> dict | None:
    """回落：exec_id 即工作流步骤会话 id，取该会话中本步骤的轨迹。"""
    step_session = SessionStore.load_session(exec_id, sessions_dir)
    if step_session is None:
        return None
    meta = step_session.metadata or {}
    step_meta = meta.get("workflow") or meta.get("task")
    if not isinstance(step_meta, dict):
        return None
    messages = step_session.get_valid_messages()
    start = -1
    for index, message in enumerate(messages):
        if message.get("role") == "user" and _is_workflow_step_prompt(message.get("content")):
            start = index
    if start < 0:
        return None
    return {
        "exec_id": exec_id,
        "task": "工作流步骤",
        "summary": "",
        "status": "completed",
        "stats": {},
        "epochs": [],
        "messages": messages[start:],
        "metadata": {
            "step_session_id": exec_id,
            "workflow_step": True,
            "session_id": exec_id,
            "status": "completed",
            "phase": "done",
            "stats": {},
        },
    }


def _resolve_tool_results_for_session(messages: list[dict], session_dir: Path) -> None:
    """从外部文件按需读取工具结果内容，注入到消息中。

    用于前端渲染历史会话。Session 中只存元信息，内容保存在外部文件。
    此函数从文件读取内容并注入到消息中，供前端渲染。
    同时给已回答的 ask_user tool_use 块添加 _meta.answered 标记。
    """
    # 第一遍：收集已回答的 ask_user tool_use_id
    answered_ask_user_ids = set()
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            # 已回答 = completed == True（用户已提交答案）
            if block.get("type") == "tool_result" and block.get("_meta", {}).get("completed") is True:
                tool_use_id = block.get("tool_use_id") or block.get("tool_call_id")
                if tool_use_id:
                    answered_ask_user_ids.add(tool_use_id)

    # 第二遍：处理工具结果 + 标记已回答的 ask_user
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue

        # 标记已回答的 ask_user tool_use/tool_call
        if msg.get("role") == "assistant":
            for block in content:
                # 同时检查 tool_use 和 tool_call 两种类型
                if block.get("type") in ("tool_use", "tool_call") and block.get("id") in answered_ask_user_ids:
                    if "_meta" not in block:
                        block["_meta"] = {}
                    block["_meta"]["answered"] = True

        # 处理工具结果
        if msg.get("role") == "user":
            for block in content:
                if block.get("type") != "tool_result":
                    continue

                # 从 block 级别的 _meta 读取内部元数据
                block_meta = block.get("_meta", {})
                output_path = block_meta.get("output_path", "")
                truncated = block_meta.get("truncated", False)

                # 已有内容（如错误信息）跳过；例外：已回答的 ask_user
                # （jsonl 保留占位、答案在外部文件，必须读文件恢复）
                answered = block_meta.get("completed") is True and bool(output_path)
                if block.get("content") and not answered:
                    continue
                file_size = block_meta.get("file_size", 0)

                # 从外部文件读取
                if not output_path:
                    block["content"] = "[工具输出文件路径缺失]"
                    continue

                file_path = (session_dir / output_path).resolve()
                # W10: output_path 来自会话文件 _meta，篡改可能穿越 session 目录 → 拒绝
                if not file_path.is_relative_to(session_dir.resolve()):
                    block["content"] = f"[非法 output_path: {output_path}]"
                    continue
                if not file_path.exists():
                    # 尝试在子代理执行目录中查找（exec_* 位于 session 目录内）
                    exec_dirs = list(session_dir.glob("exec_*"))
                    found = False
                    for exec_dir in exec_dirs:
                        candidate = (exec_dir / output_path).resolve()
                        if candidate.is_relative_to(exec_dir.resolve()) and candidate.exists():
                            file_path = candidate
                            found = True
                            break
                    if not found:
                        block["content"] = f"[工具输出文件不存在: {output_path}]"
                        continue

                try:
                    file_content = file_path.read_text(encoding='utf-8', errors='replace')
                except Exception as e:
                    block["content"] = f"[读取工具输出失败: {e}]"
                    continue

                # 截断显示（前端不需要完整内容）
                if truncated:
                    truncated_content = truncate_middle(file_content, 8000)
                    if not file_size:
                        file_size = len(file_content)
                    guide = (
                        f"\n\n---\n"
                        f"[提示] 工具输出过长（{file_size:,} 字符），已截断显示。\n"
                        f"完整输出保存在文件: {output_path}。"
                    )
                    block["content"] = truncated_content + guide
                else:
                    # 正常输出：限制到 100K 字符
                    block["content"] = truncate_result(file_content, 100_000)


@router.post("/api/workspaces/{workspace_uuid}/sessions")
async def create_session(workspace_uuid: str, request: CreateSessionRequest, ws_dir: Path = Depends(require_workspace_data_dir)):
    """Create a new session."""
    sessions_dir = sessions_dir_for_data_dir(ws_dir)
    channel = (request.channel or "web").strip().lower()
    if channel not in API_CREATE_CHANNEL_IDS:
        raise HTTPException(status_code=400, detail=f"不支持的会话通道：{channel}")
    try:
        session = SessionStore.create_new_session(
            sessions_dir, request.name,
            metadata=marker_metadata(channel),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create session: {e}")

    return session.to_dict()


def _purge_session(workspace_uuid: str, session_id: str, sessions_dir: Path) -> None:
    """停止并摘除会话编排/队列/runner，再删目录（实现见 core.session_purge）。"""
    from core.session_purge import purge_session

    purge_session(
        workspace_uuid, session_id, sessions_dir, stop_goal=stop_goal_runner,
    )


@router.delete("/api/workspaces/{workspace_uuid}/sessions/{session_id}",
               dependencies=[Depends(teen_guard("session_delete", hard=True))])
async def delete_session(workspace_uuid: str, session_id: str, ws_dir: Path = Depends(require_workspace_data_dir)):
    """Delete a session."""
    validate_session_id(session_id)
    session_dir = sessions_dir_for_data_dir(ws_dir) / session_id
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    # cleanup 会 save，必须在目录还在时摘除 runner；GoalRunner 也先停掉。
    # 磁盘 cleanup/save 属阻塞操作，丢线程池执行。
    await asyncio.to_thread(_purge_session, workspace_uuid, session_id, sessions_dir_for_data_dir(ws_dir))
    return {"success": True}


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/rename")
async def rename_session(workspace_uuid: str, session_id: str, request: RenameSessionRequest, ws_dir: Path = Depends(require_workspace_data_dir)):
    """Rename a session (atomic write)."""
    validate_session_id(session_id)
    session_dir = sessions_dir_for_data_dir(ws_dir) / session_id
    if not (session_dir / "index.json").exists():
        raise HTTPException(status_code=404, detail="Session not found")

    now = now_str()
    key = session_key(workspace_uuid, session_id)

    try:
        runner = registry.get(key)
        if runner:
            # runner 已加载：经 rename() 改内存并置脏（递增版本号），落盘不短路
            runner.session.rename(request.name)
            return {"success": True}

        meta = read_meta(session_dir)
        meta["name"] = request.name
        meta.setdefault("metadata", {})["updated_at"] = now
        atomic_write_json(session_dir / META_FILE, meta)
        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to rename session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to rename session")


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/hidden")
async def set_session_hidden(workspace_uuid: str, session_id: str, request: SetHiddenRequest, ws_dir: Path = Depends(require_workspace_data_dir)):
    """Set session hidden status.

    青少年模式 deny_hide：隐藏会让会话在列表中消失（家长看不到），默认禁止。
    """
    tm = get_teen_mode()
    if request.hidden and tm.policy().get("deny_hide") and not tm.is_unlocked():
        tm.audit("deny", action="session_hide", target=session_id, source="web")
        tm.record_denied("session_hide")
        raise HTTPException(status_code=403, detail=deny_message("session_hide"))
    validate_session_id(session_id)
    session_dir = sessions_dir_for_data_dir(ws_dir) / session_id
    if not (session_dir / "index.json").exists():
        raise HTTPException(status_code=404, detail="Session not found")

    now = now_str()
    key = session_key(workspace_uuid, session_id)

    try:
        runner = registry.get(key)
        if runner:
            runner.session.set_hidden(request.hidden)
            return {"success": True}

        meta = read_meta(session_dir)
        meta.setdefault("metadata", {})["hidden"] = request.hidden
        meta["metadata"]["updated_at"] = now
        atomic_write_json(session_dir / META_FILE, meta)
        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to set hidden status for session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to set hidden status")


@router.post("/api/workspaces/{workspace_uuid}/sessions/batch")
async def batch_session_operation(workspace_uuid: str, request: BatchSessionRequest, ws_dir: Path = Depends(require_workspace_data_dir)):
    """Batch operations on sessions: hide, unhide, or delete.

    青少年模式：delete 为硬禁（解锁也不放行）；hide 受 deny_hide 策略约束。
    """
    tm = get_teen_mode()
    if request.action == "delete":
        tm.ensure_allowed("session_delete", hard=True)
    elif request.action == "hide" and tm.policy().get("deny_hide") and not tm.is_unlocked():
        tm.audit("deny", action="session_hide", source="web")
        tm.record_denied("session_hide")
        raise HTTPException(status_code=403, detail=deny_message("session_hide"))

    sessions_dir = sessions_dir_for_data_dir(ws_dir)
    results = []

    for session_id in request.session_ids:
        if not _SAFE_ID_RE.match(session_id):
            results.append({"session_id": session_id, "success": False, "error": "Invalid session_id format"})
            continue
        session_dir = sessions_dir / session_id
        index_file = session_dir / "index.json"
        if not index_file.exists():
            results.append({"session_id": session_id, "success": False, "error": "Not found"})
            continue

        try:
            if request.action == "delete":
                await asyncio.to_thread(
                    _purge_session, workspace_uuid, session_id, sessions_dir
                )
                results.append({"session_id": session_id, "success": True})
            elif request.action in ("hide", "unhide"):
                # Set hidden status in meta.json
                meta = read_meta(session_dir)
                meta.setdefault("metadata", {})["hidden"] = (request.action == "hide")
                atomic_write_json(session_dir / META_FILE, meta)
                results.append({"session_id": session_id, "success": True})
            else:
                results.append({"session_id": session_id, "success": False, "error": f"Unknown action: {request.action}"})
        except Exception as e:
            logger.error(f"Batch operation failed for session {session_id}: {e}")
            results.append({"session_id": session_id, "success": False, "error": str(e)})

    return {"success": True, "results": results}


# ----- 子代理执行日志 -----

@router.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/executions")
async def list_executions(workspace_uuid: str, session_id: str, ws_dir: Path = Depends(require_workspace_data_dir)):
    """List all sub-agent executions for a session."""
    validate_session_id(session_id)
    sessions_dir = sessions_dir_for_data_dir(ws_dir)
    if not (sessions_dir / session_id / "index.json").exists():
        raise HTTPException(status_code=404, detail="Session not found")
    sm = await asyncio.to_thread(SessionStore.load_session, session_id, sessions_dir)
    if not sm:
        raise HTTPException(status_code=404, detail="Session not found")

    logs = sm.agent_logs.list_agent_logs()
    for log in logs:
        log["stats"] = (log.get("metadata") or {}).get("stats") or {}
    return {"executions": logs}


@router.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/executions/{exec_id}")
async def get_execution(workspace_uuid: str, session_id: str, exec_id: str, ws_dir: Path = Depends(require_workspace_data_dir)):
    """Get a specific sub-agent execution log with full messages."""
    validate_session_id(session_id)
    validate_exec_id(exec_id)
    sessions_dir = sessions_dir_for_data_dir(ws_dir)
    if not (sessions_dir / session_id / "index.json").exists():
        raise HTTPException(status_code=404, detail="Session not found")
    sm = await asyncio.to_thread(SessionStore.load_session, session_id, sessions_dir)
    if not sm:
        raise HTTPException(status_code=404, detail="Session not found")

    payload = sm.agent_logs.load_execution(exec_id)
    if not payload:
        # 工作流旧运行：没有写 exec 日志，但 exec_id 就是步骤会话 id
        payload = _step_session_execution(exec_id, sessions_dir)
    if not payload:
        raise HTTPException(status_code=404, detail="Execution not found")

    # 从外部文件按需注入工具结果内容（前端渲染需要）；
    # 同步读多文件，放线程池避免阻塞事件循环（审查 P2）
    exec_dir = sessions_dir / session_id / exec_id
    # 工作流步骤：消息与工具输出文件都在步骤自己的会话目录，exec_id 即步骤会话 id
    meta = payload.get("metadata") or {}
    step_session_id = str(meta.get("step_session_id") or "")
    if step_session_id:
        step_dir = sessions_dir / step_session_id
        if step_dir.is_dir():
            exec_dir = step_dir
    await asyncio.to_thread(
        _resolve_tool_results_for_session, payload["messages"], exec_dir
    )

    return payload


# ----- Sensitive words (上行请求敏感词替换) -----

class SensitiveWordsRequest(BaseModel):
    enabled: bool = False
    fixed: dict = {}
    rules: list[dict] = []


@router.get("/api/workspaces/{workspace_uuid}/sensitive-words")
async def get_sensitive_words(ws_dir: Path = Depends(require_workspace_data_dir)):
    """读取当前工作区的敏感词替换规则（配置文件 .cili/sensitive_words.json）。"""
    from core.sensitive_words import read_config
    return read_config(ws_dir)


@router.put("/api/workspaces/{workspace_uuid}/sensitive-words",
            dependencies=[Depends(teen_guard("config_update"))])
async def save_sensitive_words(request: SensitiveWordsRequest,
                               ws_dir: Path = Depends(require_workspace_data_dir)):
    """保存规则；只影响发给模型的副本，会话与文件保留原文。"""
    from core.sensitive_words import write_config
    return write_config(ws_dir, request.model_dump())


# ----- Project Instructions (AGENTS.md / CLAUDE.md) -----

# 模板目录（项目指令文件清单见 core.project_instructions.PROJECT_INSTRUCTION_FILES）
_TEMPLATES_DIR = PROJECT_ROOT / "core" / "templates" / "prompts"


class SaveInstructionsRequest(BaseModel):
    filename: str  # 文件名（AGENTS.md / CLAUDE.md 等）
    content: str


@router.get("/api/workspaces/{workspace_uuid}/instructions")
async def get_instructions(workspace_uuid: str, ws_dir: Path = Depends(require_workspace_data_dir)):
    """读取工作区的项目指令文件（AGENTS.md / CLAUDE.md）。

    按优先级查找，返回第一个找到的文件内容和文件名。
    """
    info = _get_workspace_info(workspace_uuid)
    if not info:
        raise HTTPException(status_code=404, detail="Workspace not found")
    workspace_dir = info.get("directory", "")
    if not workspace_dir:
        raise HTTPException(status_code=400, detail="Workspace directory not set")

    for filename in PROJECT_INSTRUCTION_FILES:
        filepath = os.path.join(workspace_dir, filename)
        if os.path.isfile(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                return {"filename": filename, "content": content, "found": True}
            except Exception as e:
                logger.warning(f"Failed to read {filepath}: {e}")
                continue

    # 未找到任何文件，返回空内容（默认用 AGENTS.md）
    return {"filename": "AGENTS.md", "content": "", "found": False}


@router.put("/api/workspaces/{workspace_uuid}/instructions",
            dependencies=[Depends(teen_guard("config_update"))])
async def save_instructions(workspace_uuid: str, request: SaveInstructionsRequest, ws_dir: Path = Depends(require_workspace_data_dir)):
    """保存项目指令文件到工作区。

    保持原文件名，若指定新文件名则写入对应文件。
    """
    info = _get_workspace_info(workspace_uuid)
    if not info:
        raise HTTPException(status_code=404, detail="Workspace not found")
    workspace_dir = info.get("directory", "")
    if not workspace_dir:
        raise HTTPException(status_code=400, detail="Workspace directory not set")

    # 验证文件名（只允许已知指令文件名）
    filename = request.filename
    # 先拒绝路径成分：白名单/后缀校验挡不住 "../../x.md" 或 "C:/x.md" 逃出工作区
    if (
        not filename
        or filename != os.path.basename(filename)
        or filename in (".", "..")
        or "/" in filename
        or "\\" in filename
    ):
        raise HTTPException(status_code=400, detail=f"Invalid filename: {filename}")
    if filename not in PROJECT_INSTRUCTION_FILES and not filename.endswith(".md"):
        raise HTTPException(status_code=400, detail=f"Invalid filename: {filename}")

    filepath = os.path.join(workspace_dir, filename)
    # 纵深防御：解析后仍须落在工作区内（防符号链接等）
    if not Path(filepath).resolve().is_relative_to(Path(workspace_dir).resolve()):
        raise HTTPException(status_code=400, detail=f"Invalid filename: {filename}")
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(request.content)
        return {"success": True, "filename": filename}
    except Exception as e:
        logger.error(f"Failed to save instructions: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save: {e}")


@router.post("/api/workspaces/{workspace_uuid}/instructions/generate",
             dependencies=[Depends(teen_guard("config_update"))])
async def generate_instructions(workspace_uuid: str, ws_dir: Path = Depends(require_workspace_data_dir)):
    """创建 worker agent 后台扫描代码并生成 AGENTS.md。

    返回任务 ID 供前端轮询状态。
    """
    info = _get_workspace_info(workspace_uuid)
    if not info:
        raise HTTPException(status_code=404, detail="Workspace not found")
    workspace_dir = info.get("directory", "")
    if not workspace_dir:
        raise HTTPException(status_code=400, detail="Workspace directory not set")

    from core.instruction_generation import start_generation

    try:
        exec_id = start_generation(workspace_uuid, workspace_dir)
        return {"task_id": exec_id, "status": "running"}
    except Exception as e:
        logger.error(f"Failed to start generate runner: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start: {e}")


@router.get("/api/workspaces/{workspace_uuid}/instructions/generate/{task_id}")
async def get_generate_status(workspace_uuid: str, task_id: str, ws_dir: Path = Depends(require_workspace_data_dir)):
    """查询 AGENTS.md 生成任务状态。"""
    from core.instruction_generation import generation_status

    status = generation_status(task_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return status


@router.get("/api/workspaces/{workspace_uuid}/instructions/templates")
async def list_instruction_templates(workspace_uuid: str, ws_dir: Path = Depends(require_workspace_data_dir)):
    """列出可用的项目指令模板。

    模板存放在 core/templates/prompts/ 目录，为 .md 文件。
    """
    templates = []
    if _TEMPLATES_DIR.exists():
        for f in sorted(_TEMPLATES_DIR.iterdir()):
            if f.is_file() and f.suffix == ".md":
                templates.append({
                    "name": f.stem,
                    "filename": f.name,
                })
    return {"templates": templates}


@router.get("/api/workspaces/{workspace_uuid}/instructions/templates/{template_name}")
async def load_instruction_template(workspace_uuid: str, template_name: str, ws_dir: Path = Depends(require_workspace_data_dir)):
    """加载指定模板的内容。"""
    # 安全检查：防止路径穿越，支持中文文件名
    # 只允许字母、数字、中文、下划线、连字符
    if not re.match(r'^[\w-]+$', template_name):
        raise HTTPException(status_code=400, detail="Invalid template name")

    template_file = _TEMPLATES_DIR / f"{template_name}.md"
    if not template_file.exists():
        raise HTTPException(status_code=404, detail="Template not found")

    try:
        with open(template_file, "r", encoding="utf-8") as f:
            content = f.read()
        return {"name": template_name, "content": content}
    except Exception as e:
        logger.error(f"Failed to read template: {e}")
        raise HTTPException(status_code=500, detail="Failed to read template")
