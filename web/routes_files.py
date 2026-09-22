"""Workspace Files / Directory Browser / File Manager 路由域。

注意：/api/workspace/files/{path}（具体）必须在 /api/workspace/{path}（catch-all）
之前注册，否则后者会吞掉 files 前缀路径。本文件内顺序即注册顺序。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.config import load_workspace_config, load_workspaces_index

from web.deps import (
    _csrf_protect, _get_workspace_info, _LOCALHOST_IPS,
    _validate_workspace_uuid,
)

router = APIRouter()

# 单次上传文件大小上限
_MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB


# ---------- Models ----------

class FileCreateRequest(BaseModel):
    """Request model for creating a file or folder."""
    workspace_uuid: str
    path: str = ""  # Parent directory path
    name: str  # File/folder name
    type: str = "file"  # "file" or "folder"
    content: str = ""  # Initial content for files


class FileDeleteRequest(BaseModel):
    """Request model for deleting files."""
    workspace_uuid: str
    paths: list[str]  # List of relative paths to delete


class FileUpdateRequest(BaseModel):
    """Request model for updating (rename/move/save) a file."""
    workspace_uuid: str
    path: str  # Current relative path
    new_path: str = ""  # New relative path (for rename/move)
    content: str | None = None  # New content (for save)


# ----- Workspace Files -----

def _find_workspace_for_file(file_path: str) -> tuple[str, Path] | None:
    """Scan all workspaces (from workspaces.json) and find which one contains the given relative file path.

    Returns (workspace_uuid, full_file_path) or None if not found in any workspace.
    """
    for entry in load_workspaces_index():
        uuid = entry.get("uuid", "")
        workspace_dir = entry.get("directory", "")
        if not workspace_dir:
            continue
        workspace_path = Path(workspace_dir).resolve()
        file_full_path = (workspace_path / file_path).resolve()
        # Security: ensure file is within the workspace directory
        if not file_full_path.is_relative_to(workspace_path):
            continue
        if file_full_path.exists() and file_full_path.is_file():
            return (uuid, file_full_path)
    return None


def _serve_workspace_file(workspace_dir: str, file_path: str) -> FileResponse:
    """Serve a file from a workspace directory with path-traversal protection."""
    workspace_path = Path(workspace_dir).resolve()
    file_full_path = (workspace_path / file_path).resolve()
    if not file_full_path.is_relative_to(workspace_path):
        raise HTTPException(status_code=403, detail="Access denied: path traversal not allowed")
    if not file_full_path.exists() or not file_full_path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
    return FileResponse(str(file_full_path))


@router.get("/api/workspace/files/{file_path:path}")
async def get_workspace_file_short(file_path: str, workspace_uuid: str = ""):
    """Serve a file from a workspace (no UUID in URL path).

    If workspace_uuid query parameter is provided, look in that workspace only.
    Otherwise, scan all workspaces to find the file.
    """
    if workspace_uuid:
        _validate_workspace_uuid(workspace_uuid)  # W9: 防路径穿越
        info = _get_workspace_info(workspace_uuid)
        if not info:
            raise HTTPException(status_code=404, detail="Workspace not found")
        workspace_dir = info.get("directory", "")
        if not workspace_dir:
            raise HTTPException(status_code=404, detail="Workspace directory not configured")
        return _serve_workspace_file(workspace_dir, file_path)

    # No UUID: scan all workspaces
    result = _find_workspace_for_file(file_path)
    if not result:
        raise HTTPException(status_code=404, detail=f"File not found in any workspace: {file_path}")

    _, file_full_path = result
    return FileResponse(str(file_full_path))


@router.get("/api/workspace/{file_path:path}")
async def get_workspace_file_compat(file_path: str, workspace_uuid: str = ""):
    """Compatibility route: /api/workspace/{file} also works."""
    return await get_workspace_file_short(file_path, workspace_uuid)


@router.get("/api/workspaces/{workspace_uuid}/files/{file_path:path}")
async def get_workspace_file(workspace_uuid: str, file_path: str):
    """Serve a file from the workspace directory (for images, etc.)."""
    _validate_workspace_uuid(workspace_uuid)  # W9: 防路径穿越
    info = _get_workspace_info(workspace_uuid)
    if not info:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_dir = info.get("directory", "")
    if not workspace_dir:
        raise HTTPException(status_code=404, detail="Workspace directory not configured")

    return _serve_workspace_file(workspace_dir, file_path)


# ----- Directory Browser -----

@router.get("/api/browse")
def browse_directory(path: str = "", request: Request = None):
    """Browse directories on the server filesystem.

    Args:
        path: Directory path to browse. Empty string returns system drives (Windows) or root (Unix).

    Returns:
        List of directories with their names and full paths.
    """
    # W8: 目录浏览限本机——工作区文件夹选择是本地管理操作，
    # 避免 LAN/网络攻击者枚举服务器任意磁盘目录结构
    client_ip = request.client.host if request and request.client else ""
    if client_ip not in _LOCALHOST_IPS:
        raise HTTPException(
            status_code=403,
            detail="Access denied: browse_directory is localhost-only",
        )
    if not path:
        if sys.platform == "win32":
            # Get available drives on Windows
            import string
            drives = []
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    drives.append({"name": drive, "path": drive})
            return {"path": "", "directories": drives, "files": [], "parent": None}
        else:
            path = "/"

    # Validate path exists and is a directory
    # If path doesn't exist, try to find the nearest existing parent
    try:
        path_obj = Path(path)
        if not path_obj.exists():
            # Auto-fallback: find nearest existing parent directory
            original_path = path
            search_path = path_obj
            while search_path and str(search_path) != str(search_path.parent):
                search_path = search_path.parent
                if search_path.exists():
                    path_obj = search_path
                    break
            else:
                # Fallback to system root or drives
                if sys.platform == "win32":
                    return {"path": "", "directories": [], "parent": None,
                            "fallback": f"原路径不存在: {original_path}，请从磁盘列表选择"}
                else:
                    return {"path": "/", "directories": [], "parent": None,
                            "fallback": f"原路径不存在: {original_path}，已跳转到根目录"}
        if not path_obj.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {path}")
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied: {path}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # List directories and files
    directories = []
    files = []
    try:
        for item in sorted(path_obj.iterdir(), key=lambda x: x.name.lower()):
            if item.name.startswith('.'):
                continue
            try:
                if item.is_dir():
                    directories.append({"name": item.name, "path": str(item.resolve())})
                elif item.is_file() or item.is_symlink():
                    stat = item.stat()
                    files.append({
                        "name": item.name,
                        "path": str(item.resolve()),
                        "is_file": True,
                        "size": stat.st_size,
                        "modified": stat.st_mtime
                    })
            except (OSError, PermissionError):
                continue
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied: {path}")

    # Get parent directory
    resolved_path = str(path_obj.resolve())

    # Check if we're at a drive root (e.g., C:\)
    # A drive root has the pattern X:\ where X is a letter
    is_drive_root = sys.platform == "win32" and re.match(r'^[A-Z]:\\$', resolved_path, re.IGNORECASE)

    if is_drive_root:
        # At drive root, parent is drive list (empty string)
        parent = ""
    elif resolved_path == str(Path(resolved_path).anchor):
        # At root directory (e.g., / on Unix), no parent
        parent = None
    else:
        parent = str(path_obj.parent.resolve())

    return {
        "path": resolved_path,
        "directories": directories,
        "files": files,
        "parent": parent
    }


@router.get("/api/files")
def list_files(workspace_uuid: str, path: str = ""):
    """List files in workspace directory for file selection.

    Args:
        workspace_uuid: Workspace UUID to restrict file listing
        path: Relative path within workspace. Empty string lists workspace root.

    Returns:
        List of files and directories with relative paths.
    """
    _validate_workspace_uuid(workspace_uuid)  # W9: 防路径穿越
    # Get workspace directory
    ws_config = load_workspace_config(workspace_uuid)
    if not ws_config:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_dir = Path(ws_config.get("directory", ""))
    if not workspace_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace directory not found")

    # Build target path
    if path and path.strip():
        target_path = (workspace_dir / path).resolve()
        # Security: ensure path is within workspace
        try:
            target_path.relative_to(workspace_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied: path outside workspace")
    else:
        target_path = workspace_dir.resolve()
        path = ""  # 确保空路径

    if not target_path.exists() or not target_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {path}")

    # List files and directories
    items = []
    try:
        for item in sorted(target_path.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if item.name.startswith('.'):
                continue
            # 跳过非法路径（Windows 特殊设备名等 is_file/is_dir/is_symlink 均为 False）
            if not item.is_file() and not item.is_dir() and not item.is_symlink():
                continue
            rel_path = str(item.relative_to(workspace_dir))
            try:
                stat = item.stat()
                items.append({
                    "name": item.name,
                    "path": rel_path,
                    "is_file": item.is_file(),
                    "size": stat.st_size if item.is_file() else None,
                    "modified": stat.st_mtime
                })
            except (OSError, PermissionError):
                # 无法访问的文件/目录，跳过
                continue
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied: {path}")

    # Get parent path
    if target_path == workspace_dir.resolve():
        parent = None
    else:
        parent = str(target_path.parent.relative_to(workspace_dir))
        if parent == ".":
            parent = ""

    return {
        "path": path,
        "items": items,
        "parent": parent
    }


# ----- File Manager API (统一 /api/files 路径) -----

@router.get("/api/files/{file_path:path}")
def read_file(file_path: str, workspace_uuid: str):
    """Read a file from workspace.

    Args:
        file_path: Relative path within workspace
        workspace_uuid: Workspace UUID

    Returns:
        File content as text or binary
    """
    _validate_workspace_uuid(workspace_uuid)  # W9: 防路径穿越
    ws_config = load_workspace_config(workspace_uuid)
    if not ws_config:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_dir = Path(ws_config.get("directory", ""))
    if not workspace_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace directory not found")

    # Security: ensure path is within workspace
    file_full_path = (workspace_dir / file_path).resolve()
    if not file_full_path.is_relative_to(workspace_dir.resolve()):
        raise HTTPException(status_code=403, detail="Access denied: path outside workspace")

    if not file_full_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    if not file_full_path.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {file_path}")

    return FileResponse(str(file_full_path))


@router.post("/api/files")
def create_file(request: FileCreateRequest, request_raw: Request = None):
    """Create a new file or folder in workspace.

    Args:
        request: FileCreateRequest with workspace_uuid, path, name, type, content

    Returns:
        Created file/folder info
    """
    _csrf_protect(request_raw)  # W6: CSRF 防护
    _validate_workspace_uuid(request.workspace_uuid)  # W9: 防路径穿越
    ws_config = load_workspace_config(request.workspace_uuid)
    if not ws_config:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_dir = Path(ws_config.get("directory", ""))
    if not workspace_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace directory not found")

    # Build target path
    if request.path:
        target_dir = (workspace_dir / request.path).resolve()
    else:
        target_dir = workspace_dir.resolve()

    # Security: ensure path is within workspace
    if not target_dir.is_relative_to(workspace_dir.resolve()):
        raise HTTPException(status_code=403, detail="Access denied: path outside workspace")

    # Check for path traversal in name
    target_path = (target_dir / request.name).resolve()
    if not target_path.is_relative_to(workspace_dir.resolve()):
        raise HTTPException(status_code=403, detail="Access denied: invalid file name")

    if target_path.exists():
        raise HTTPException(status_code=400, detail=f"Already exists: {request.name}")

    try:
        if request.type == "folder":
            target_path.mkdir(parents=True, exist_ok=True)
            return {"type": "folder", "path": str(target_path.relative_to(workspace_dir)), "name": request.name}
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(request.content, encoding="utf-8")
            return {"type": "file", "path": str(target_path.relative_to(workspace_dir)), "name": request.name}
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/files")
def delete_files(request: FileDeleteRequest, request_raw: Request = None):
    """Delete files or folders from workspace.

    Args:
        request: FileDeleteRequest with workspace_uuid and paths list

    Returns:
        Deletion result
    """
    _csrf_protect(request_raw)  # W6: CSRF 防护
    _validate_workspace_uuid(request.workspace_uuid)  # W9: 防路径穿越
    ws_config = load_workspace_config(request.workspace_uuid)
    if not ws_config:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_dir = Path(ws_config.get("directory", ""))
    if not workspace_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace directory not found")

    deleted = []
    errors = []

    for path in request.paths:
        file_full_path = (workspace_dir / path).resolve()

        # Security check
        if not file_full_path.is_relative_to(workspace_dir.resolve()):
            errors.append({"path": path, "error": "Access denied: path outside workspace"})
            continue

        if not file_full_path.exists():
            errors.append({"path": path, "error": "File not found"})
            continue

        try:
            if file_full_path.is_dir():
                shutil.rmtree(file_full_path)
            else:
                file_full_path.unlink()
            deleted.append(path)
        except PermissionError:
            errors.append({"path": path, "error": "Permission denied"})
        except Exception as e:
            errors.append({"path": path, "error": str(e)})

    return {"deleted": deleted, "errors": errors}


@router.put("/api/files")
def update_file(request: FileUpdateRequest, request_raw: Request = None):
    """Update a file: rename/move or save content.

    Args:
        request: FileUpdateRequest with workspace_uuid, path, new_path, content

    Returns:
        Update result
    """
    _csrf_protect(request_raw)  # W6: CSRF 防护
    _validate_workspace_uuid(request.workspace_uuid)  # W9: 防路径穿越
    ws_config = load_workspace_config(request.workspace_uuid)
    if not ws_config:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_dir = Path(ws_config.get("directory", ""))
    if not workspace_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace directory not found")

    file_full_path = (workspace_dir / request.path).resolve()

    # Security check for source
    if not file_full_path.is_relative_to(workspace_dir.resolve()):
        raise HTTPException(status_code=403, detail="Access denied: path outside workspace")

    if not file_full_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {request.path}")

    # Handle rename/move
    if request.new_path:
        new_full_path = (workspace_dir / request.new_path).resolve()

        # Security check for destination
        if not new_full_path.is_relative_to(workspace_dir.resolve()):
            raise HTTPException(status_code=403, detail="Access denied: destination outside workspace")

        try:
            new_full_path.parent.mkdir(parents=True, exist_ok=True)
            file_full_path.rename(new_full_path)
            return {"action": "rename", "path": request.new_path}
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # Handle content save
    if request.content is not None:
        if not file_full_path.is_file():
            raise HTTPException(status_code=400, detail="Cannot save content to a directory")

        try:
            file_full_path.write_text(request.content, encoding="utf-8")
            return {"action": "save", "path": request.path}
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    raise HTTPException(status_code=400, detail="No action specified: provide new_path or content")


def _sanitize_upload_filename(raw: str) -> str | None:
    """净化上传文件名：取 basename，拒绝 NTFS ADS 与 Windows 保留名/字符（W14）。

    返回净化后的安全文件名；非法时返回 None（调用方记为错误）。
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
    if (stem.startswith("COM") and stem[3:].isdigit()) or \
       (stem.startswith("LPT") and stem[3:].isdigit()):
        return None
    return name


@router.post("/api/files/upload")
def upload_files(
    workspace_uuid: str = Form(...),
    path: str = Form(""),
    files: list[UploadFile] = File(...),
    request: Request = None,
):
    """Upload files to workspace.

    Args:
        workspace_uuid: Workspace UUID
        path: Relative directory path within workspace
        files: List of uploaded files

    Returns:
        Upload result
    """
    _csrf_protect(request)  # W6: CSRF 防护（multipart 属简单请求，须单独校验）
    _validate_workspace_uuid(workspace_uuid)  # W9: 防路径穿越
    ws_config = load_workspace_config(workspace_uuid)
    if not ws_config:
        raise HTTPException(status_code=404, detail="Workspace not found")

    workspace_dir = Path(ws_config.get("directory", ""))
    if not workspace_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace directory not found")

    # Build target directory
    if path:
        target_dir = (workspace_dir / path).resolve()
    else:
        target_dir = workspace_dir.resolve()

    # Security check
    if not target_dir.is_relative_to(workspace_dir.resolve()):
        raise HTTPException(status_code=403, detail="Access denied: path outside workspace")

    target_dir.mkdir(parents=True, exist_ok=True)

    uploaded = []
    errors = []

    for file in files:
        try:
            # Check file size before reading
            if file.size is not None and file.size > _MAX_UPLOAD_SIZE:
                errors.append({"name": file.filename, "error": f"File too large (max 100MB)"})
                continue

            safe_name = _sanitize_upload_filename(file.filename or "")
            if not safe_name:
                errors.append({"name": file.filename, "error": "Invalid filename"})
                continue

            file_path = (target_dir / safe_name).resolve()

            # Security check for filename
            if not file_path.is_relative_to(workspace_dir.resolve()):
                errors.append({"name": file.filename, "error": "Invalid filename"})
                continue

            # Write file（同步端点中 UploadFile 用底层 SpooledTemporaryFile 同步读取）
            content = file.file.read()

            # Double-check size after reading (in case size header was missing)
            if len(content) > _MAX_UPLOAD_SIZE:
                errors.append({"name": file.filename, "error": f"File too large (max 100MB)"})
                continue

            file_path.write_bytes(content)
            uploaded.append({"name": file.filename, "size": len(content)})
        except Exception as e:
            errors.append({"name": file.filename, "error": str(e)})

    return {"uploaded": uploaded, "errors": errors}
