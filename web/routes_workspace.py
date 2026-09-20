"""Workspace + Session + Agent Execution 路由域。"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.config import (
    validate_workspace_name, PROJECT_ROOT, load_workspace_config,
    save_workspace_config,
)
from core.fs_utils import atomic_write_json, load_json_or_backup
from core.session import (
    MESSAGES_FILE, META_FILE, SessionManager, _drop_session_lock,
    load_history_messages, load_history_meta, preview_from_messages,
    read_jsonl, read_meta,
)
from core.tools.base import Tool

from web.deps import (
    agents, _agent_access, _agents_lock, _list_all_workspaces,
    _new_short_id, _require_workspace, _SAFE_ID_RE, _validate_exec_id,
    _validate_session_id, _validate_workspace_uuid, WORKSPACE_DATA_DIR,
    _get_workspace_info,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------- Models ----------

class CreateSessionRequest(BaseModel):
    name: str = "New Session"


class CreateWorkspaceRequest(BaseModel):
    name: str
    directory: str = ""


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    directory: str | None = None


class RenameSessionRequest(BaseModel):
    name: str


class SetHiddenRequest(BaseModel):
    hidden: bool


class BatchSessionRequest(BaseModel):
    session_ids: list[str]
    action: str  # "hide", "unhide", "delete"


# ----- Workspaces -----

@router.get("/api/workspaces")
async def list_workspaces():
    """List all workspaces."""
    return {"workspaces": _list_all_workspaces()}


@router.post("/api/workspaces")
async def create_workspace(request: CreateWorkspaceRequest):
    """Create a new workspace.

    Args:
        name: Display name for the workspace
        directory: Working directory path (optional, defaults to workspace/ inside data)
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
        workspace_dir = str(PROJECT_ROOT / "workspace" / request.name)

    # Create workspace data directory
    ws_data_dir = WORKSPACE_DATA_DIR / workspace_uuid
    ws_data_dir.mkdir(parents=True, exist_ok=True)

    # Create sessions directory
    sessions_dir = ws_data_dir / "sessions"
    sessions_dir.mkdir(exist_ok=True)

    # Create working directory
    os.makedirs(workspace_dir, exist_ok=True)

    # Save workspace config (only metadata, no model config)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws_config = {
        "workspace_name": request.name,
        "directory": workspace_dir,
        "created_at": now,
        "updated_at": now,
    }

    save_workspace_config(workspace_uuid, ws_config)

    return {
        "uuid": workspace_uuid,
        "name": request.name,
        "directory": workspace_dir,
    }


def _remove_workspace_data(workspace_uuid: str) -> None:
    """Remove workspace data directory (filesystem only, does not touch agents dict)."""
    ws_data_dir = WORKSPACE_DATA_DIR / workspace_uuid
    if not ws_data_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")
    # Delete workspace data directory
    shutil.rmtree(ws_data_dir)


def _cleanup_agents_for_workspace(workspace_uuid: str) -> None:
    """Remove agents belonging to the given workspace from the agents dict.

    Must be called under _agents_lock.
    """
    keys_to_remove = [k for k in agents if k.startswith(f"{workspace_uuid}:")]
    for key in keys_to_remove:
        evicted = agents.pop(key)
        _agent_access.pop(key, None)
        try:
            evicted.cleanup()
        except Exception as e:
            logger.warning(f"[master Agent] 清理 master Agent {key} 失败: {e}")


@router.delete("/api/workspaces/{workspace_uuid}")
async def delete_workspace(workspace_uuid: str):
    """Delete a workspace and all its data."""
    _validate_workspace_uuid(workspace_uuid)
    if workspace_uuid == "system":
        raise HTTPException(status_code=403, detail="System workspace cannot be deleted")
    async with _agents_lock:
        _cleanup_agents_for_workspace(workspace_uuid)
    _remove_workspace_data(workspace_uuid)
    return {"success": True}


@router.put("/api/workspaces/{workspace_uuid}")
async def update_workspace(workspace_uuid: str, request: UpdateWorkspaceRequest):
    """Update workspace name and/or directory."""
    _validate_workspace_uuid(workspace_uuid)
    if workspace_uuid == "system":
        raise HTTPException(status_code=403, detail="System workspace cannot be modified")
    ws_data_dir = WORKSPACE_DATA_DIR / workspace_uuid
    if not ws_data_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Load existing config
    config = load_workspace_config(workspace_uuid)
    if not config:
        raise HTTPException(status_code=404, detail="Workspace config not found")

    # Update name if provided
    if request.name is not None:
        err = validate_workspace_name(request.name)
        if err:
            raise HTTPException(status_code=400, detail=err)
        config["workspace_name"] = request.name

    # Update directory if provided
    if request.directory is not None:
        new_dir = os.path.abspath(request.directory)
        config["directory"] = new_dir

    config["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not save_workspace_config(workspace_uuid, config):
        raise HTTPException(status_code=500, detail="Failed to save config")

    return {"success": True, "workspace": config}


@router.post("/api/workspaces/{workspace_uuid}/reset")
async def reset_workspace(workspace_uuid: str):
    """Reset workspace config and sessions, but keep workspace files intact."""
    _validate_workspace_uuid(workspace_uuid)
    if workspace_uuid == "system":
        raise HTTPException(status_code=403, detail="System workspace cannot be modified")
    async with _agents_lock:
        _cleanup_agents_for_workspace(workspace_uuid)
    _remove_workspace_data(workspace_uuid)
    logger.info(f"Workspace reset: {workspace_uuid} (data removed, user files kept)")
    return {"success": True}


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
async def list_sessions(workspace_uuid: str, ws_dir: Path = Depends(_require_workspace)):
    """List all sessions in a workspace (纯 meta 读，不读消息正文、不触发迁移)。"""
    sessions_dir = ws_dir / "sessions"
    if not sessions_dir.exists():
        return {"sessions": []}

    sessions = []
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
                sessions.append({
                    "session_id": meta.get("session_id") or session_dir.name,
                    "name": meta.get("name", "Unnamed"),
                    "created_at": metadata.get("created_at", ""),
                    "updated_at": metadata.get("updated_at", ""),
                    "message_count": message_count,
                    "agent_count": metadata.get("agent_count", metadata.get("subagent_count", 0)),
                    "preview": preview,
                    "hidden": metadata.get("hidden", False),
                    "_mtime": mtime,
                })
            else:
                # 旧单文件格式（无 meta.json）：直读 index.json 的 name/metadata，只读不迁移
                old = load_json_or_backup(index_file, {}) or {}
                old_messages = old.get("messages", [])
                if not isinstance(old_messages, list):
                    old_messages = []
                metadata = old.get("metadata", {})
                sessions.append({
                    "session_id": old.get("session_id") or session_dir.name,
                    "name": old.get("name", "Unnamed"),
                    "created_at": metadata.get("created_at", ""),
                    "updated_at": metadata.get("updated_at", ""),
                    "message_count": len(old_messages),
                    "agent_count": metadata.get("agent_count", metadata.get("subagent_count", 0)),
                    "preview": preview_from_messages(old_messages),
                    "hidden": metadata.get("hidden", False),
                    "_mtime": index_file.stat().st_mtime,
                })
        except Exception as e:
            logger.error(f"Failed to read session {index_file}: {e}")

    sessions.sort(key=lambda s: s.pop("_mtime"), reverse=True)
    return {"sessions": sessions}


@router.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}")
async def get_session(workspace_uuid: str, session_id: str, ws_dir: Path = Depends(_require_workspace)):
    """Get a specific session with all messages."""
    _validate_session_id(session_id)
    session_dir = ws_dir / "sessions" / session_id
    index_file = session_dir / "index.json"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        # UI 直接读 messages.jsonl 完整历史（含压缩前所有消息），合并 _meta
        meta = load_history_meta(session_dir)
        messages = load_history_messages(session_dir)

        # 从外部文件按需注入工具结果内容（前端渲染需要）
        _resolve_tool_results_for_session(messages, session_dir)

        return {
            "session_id": meta.get("session_id", session_id),
            "name": meta.get("name", "New Session"),
            "metadata": meta.get("metadata", {}),
            "messages": messages,
        }
    except Exception as e:
        logger.error(f"Failed to read session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to read session")


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
                compacted = block_meta.get("compacted", False)
                output_path = block_meta.get("output_path", "")
                truncated = block_meta.get("truncated", False)

                # 已有内容（如错误信息）跳过；例外：已回答的 ask_user
                # （jsonl 保留占位、答案在外部文件，必须读文件恢复）
                answered = block_meta.get("completed") is True and bool(output_path)
                if block.get("content") and not answered:
                    continue
                file_size = block_meta.get("file_size", 0)

                # 处理压缩标记
                if compacted:
                    if output_path:
                        tool_use_id = output_path.replace(".txt", "").replace(".json", "")
                        block["content"] = f"[Compacted: use `read_tool_result` tool with tool_use_id=\"{tool_use_id}\" to retrieve original content]"
                    else:
                        # 内联压缩结果：无外置文件，原文保留在 messages.jsonl（会话历史）
                        block["content"] = "[Compacted: original content preserved in session history]"
                    continue

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
                    # 尝试在 Agent 执行目录中查找（exec_* 位于 session 目录内）
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
                    truncated_content = Tool.truncate_middle(file_content, 8000)
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
                    block["content"] = Tool.truncate_result(file_content, 100_000)


@router.post("/api/workspaces/{workspace_uuid}/sessions")
async def create_session(workspace_uuid: str, request: CreateSessionRequest, ws_dir: Path = Depends(_require_workspace)):
    """Create a new session."""
    sessions_dir = ws_dir / "sessions"
    try:
        session = SessionManager.create_new_session(sessions_dir, request.name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create session: {e}")

    return session.to_dict()


@router.delete("/api/workspaces/{workspace_uuid}/sessions/{session_id}")
async def delete_session(workspace_uuid: str, session_id: str, ws_dir: Path = Depends(_require_workspace)):
    """Delete a session."""
    _validate_session_id(session_id)
    session_dir = ws_dir / "sessions" / session_id
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    # 删除整个 session 目录
    shutil.rmtree(session_dir)
    _drop_session_lock(session_dir)

    # Remove from agents dict (under lock)
    key = f"{workspace_uuid}:{session_id}"
    async with _agents_lock:
        if key in agents:
            evicted = agents.pop(key)
            _agent_access.pop(key, None)
            try:
                evicted.cleanup()
            except Exception:
                pass

    return {"success": True}


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/rename")
async def rename_session(workspace_uuid: str, session_id: str, request: RenameSessionRequest, ws_dir: Path = Depends(_require_workspace)):
    """Rename a session (atomic write)."""
    _validate_session_id(session_id)
    session_dir = ws_dir / "sessions" / session_id
    if not (session_dir / "index.json").exists():
        raise HTTPException(status_code=404, detail="Session not found")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    key = f"{workspace_uuid}:{session_id}"

    try:
        agent = agents.get(key)
        if agent:
            # agent 已加载：经 rename() 改内存并置脏（递增版本号），落盘不短路
            agent.session_manager.rename(request.name)
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
async def set_session_hidden(workspace_uuid: str, session_id: str, request: SetHiddenRequest, ws_dir: Path = Depends(_require_workspace)):
    """Set session hidden status."""
    _validate_session_id(session_id)
    session_dir = ws_dir / "sessions" / session_id
    if not (session_dir / "index.json").exists():
        raise HTTPException(status_code=404, detail="Session not found")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    key = f"{workspace_uuid}:{session_id}"

    try:
        agent = agents.get(key)
        if agent:
            agent.session_manager.set_hidden(request.hidden)
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
async def batch_session_operation(workspace_uuid: str, request: BatchSessionRequest, ws_dir: Path = Depends(_require_workspace)):
    """Batch operations on sessions: hide, unhide, or delete."""
    sessions_dir = ws_dir / "sessions"
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
                # Delete session
                shutil.rmtree(session_dir, ignore_errors=True)
                _drop_session_lock(session_dir)
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


# ----- Agent Executions -----

@router.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/executions")
async def list_executions(workspace_uuid: str, session_id: str, ws_dir: Path = Depends(_require_workspace)):
    """List all sub-agent executions for a session."""
    sessions_dir = ws_dir / "sessions"
    sm = SessionManager.load_session(session_id, sessions_dir)
    if not sm:
        raise HTTPException(status_code=404, detail="Session not found")

    logs = sm.agent_logs.list_agent_logs()
    return {"executions": logs}


@router.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/executions/{exec_id}")
async def get_execution(workspace_uuid: str, session_id: str, exec_id: str, ws_dir: Path = Depends(_require_workspace)):
    """Get a specific sub-agent execution log with full messages."""
    _validate_session_id(session_id)
    _validate_exec_id(exec_id)
    sessions_dir = ws_dir / "sessions"
    sm = SessionManager.load_session(session_id, sessions_dir)
    if not sm:
        raise HTTPException(status_code=404, detail="Session not found")

    log = sm.agent_logs.load_agent_log(exec_id)
    if not log:
        raise HTTPException(status_code=404, detail="Execution not found")

    # 从外部文件按需注入工具结果内容（前端渲染需要）
    messages = log.get("messages", [])
    exec_dir = sessions_dir / session_id / exec_id
    _resolve_tool_results_for_session(messages, exec_dir)

    return log


@router.delete("/api/workspaces/{workspace_uuid}/sessions/{session_id}/executions/{exec_id}")
async def delete_execution(workspace_uuid: str, session_id: str, exec_id: str, ws_dir: Path = Depends(_require_workspace)):
    """Delete a specific sub-agent execution log."""
    _validate_session_id(session_id)
    _validate_exec_id(exec_id)
    sessions_dir = ws_dir / "sessions"
    sm = SessionManager.load_session(session_id, sessions_dir)
    if not sm:
        raise HTTPException(status_code=404, detail="Session not found")

    if sm.agent_logs.delete_agent_log(exec_id):
        return {"success": True}
    raise HTTPException(status_code=404, detail="Execution not found")


# ----- Project Instructions (AGENTS.md / CLAUDE.md) -----

# 支持的项目指令文件（按优先级排序，与 core/prompts.py 保持一致）
_PROJECT_INSTRUCTION_FILES = ["AGENTS.md", "agent.md", "CLAUDE.md", "claude.md"]

# 模板目录
_TEMPLATES_DIR = PROJECT_ROOT / "core" / "templates" / "prompts"


class SaveInstructionsRequest(BaseModel):
    filename: str  # 文件名（AGENTS.md / CLAUDE.md 等）
    content: str


@router.get("/api/workspaces/{workspace_uuid}/instructions")
async def get_instructions(workspace_uuid: str, ws_dir: Path = Depends(_require_workspace)):
    """读取工作区的项目指令文件（AGENTS.md / CLAUDE.md）。

    按优先级查找，返回第一个找到的文件内容和文件名。
    """
    info = _get_workspace_info(workspace_uuid)
    if not info:
        raise HTTPException(status_code=404, detail="Workspace not found")
    workspace_dir = info.get("directory", "")
    if not workspace_dir:
        raise HTTPException(status_code=400, detail="Workspace directory not set")

    for filename in _PROJECT_INSTRUCTION_FILES:
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


@router.put("/api/workspaces/{workspace_uuid}/instructions")
async def save_instructions(workspace_uuid: str, request: SaveInstructionsRequest, ws_dir: Path = Depends(_require_workspace)):
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
    if filename not in _PROJECT_INSTRUCTION_FILES and not filename.endswith(".md"):
        raise HTTPException(status_code=400, detail=f"Invalid filename: {filename}")

    filepath = os.path.join(workspace_dir, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(request.content)
        return {"success": True, "filename": filename}
    except Exception as e:
        logger.error(f"Failed to save instructions: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save: {e}")


@router.post("/api/workspaces/{workspace_uuid}/instructions/generate")
async def generate_instructions(workspace_uuid: str, ws_dir: Path = Depends(_require_workspace)):
    """创建 worker agent 后台扫描代码并生成 AGENTS.md。

    返回任务 ID 供前端轮询状态。
    """
    info = _get_workspace_info(workspace_uuid)
    if not info:
        raise HTTPException(status_code=404, detail="Workspace not found")
    workspace_dir = info.get("directory", "")
    if not workspace_dir:
        raise HTTPException(status_code=400, detail="Workspace directory not set")

    # 构造任务描述
    task = (
        "Scan the project in the current working directory and generate an AGENTS.md file.\n\n"
        "First, use skill(action='read', skill_id='generate-agents-md') to get the detailed instructions.\n"
        "Then follow the skill instructions to scan the project and generate AGENTS.md.\n\n"
        "Write the generated AGENTS.md to the workspace root directory using the write tool."
    )

    try:
        from core.config import load_config
        from core.agent import Agent
        import secrets

        config = load_config()
        exec_id = f"gen-{secrets.token_hex(4)}"

        # 创建 worker agent
        agent = Agent(
            config=config,
            role="worker",
            task=task,
            workspace_uuid=workspace_uuid,
            cwd=workspace_dir,
            exec_id=exec_id,
        )

        # 在后台线程运行
        import threading
        result_holder = {"status": "running", "summary": ""}

        def run_agent():
            try:
                result = agent.run()
                result_holder["status"] = result.get("status", "completed")
                result_holder["summary"] = result.get("summary", "")
            except Exception as e:
                result_holder["status"] = "error"
                result_holder["summary"] = str(e)

        thread = threading.Thread(target=run_agent, daemon=True)
        thread.start()

        # 存储任务状态（简单实现：用全局字典）
        _generate_tasks[exec_id] = {
            "status": "running",
            "thread": thread,
            "result": result_holder,
            "workspace_dir": workspace_dir,
        }

        return {"task_id": exec_id, "status": "running"}

    except Exception as e:
        logger.error(f"Failed to start generate agent: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start: {e}")


# 存储生成任务状态（简单实现）
_generate_tasks: dict[str, dict] = {}


@router.get("/api/workspaces/{workspace_uuid}/instructions/generate/{task_id}")
async def get_generate_status(workspace_uuid: str, task_id: str, ws_dir: Path = Depends(_require_workspace)):
    """查询 AGENTS.md 生成任务状态。"""
    task = _generate_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    result = task["result"]
    return {
        "task_id": task_id,
        "status": result.get("status", "running"),
        "summary": result.get("summary", ""),
    }


@router.get("/api/workspaces/{workspace_uuid}/instructions/templates")
async def list_instruction_templates(workspace_uuid: str, ws_dir: Path = Depends(_require_workspace)):
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
async def load_instruction_template(workspace_uuid: str, template_name: str, ws_dir: Path = Depends(_require_workspace)):
    """加载指定模板的内容。"""
    # 安全检查：防止路径穿越，支持中文文件名
    # 只允许字母、数字、中文、下划线、连字符
    if not re.match(r'^[\w一-鿿-]+$', template_name):
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
