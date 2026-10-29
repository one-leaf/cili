"""Workspace Files / Directory Browser / File Manager 路由域。

注意：/api/workspace/files/{path}（具体）必须在 /api/workspace/{path}（catch-all）
之前注册，否则后者会吞掉 files 前缀路径。本文件内顺序即注册顺序。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.config import load_workspace_config, load_workspaces_index
from core.fs_utils import safe_upload_filename
from core.teen_mode import deny_message, get_teen_mode
from core.tools.office2pdf import SUPPORTED_EXTENSIONS as OFFICE_SUPPORTED_EXTENSIONS, ensure_pdf
from web.deps import (
    _LOCALHOST_IPS,
    _csrf_protect,
    _get_workspace_info,
    _validate_workspace_uuid,
)

router = APIRouter()


def _teen_ws_guard(workspace_uuid: str) -> None:
    """文件域路由的受限工作区判定（本域不用 _require_workspace，逐处显式挂）。"""
    get_teen_mode().guard_workspace(workspace_uuid, source="web")

# 单次上传文件大小上限
_MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB
_UPLOAD_CHUNK_SIZE = 1024 * 1024  # 上传分块大小（流式写盘，不整文件入内存）

# 文本预览上限与二进制探测字节数
_PREVIEW_MAX_BYTES = 512 * 1024
_PREVIEW_SNIFF_BYTES = 8192


def _decode_preview_text(data: bytes, truncated: bool) -> tuple[str, str]:
    """解码预览文本：utf-8 → gb18030 → utf-8 替换。

    truncated 时尾部可能截断在多字节字符中间，先尝试去掉末尾 1-3 字节再严格解码，
    避免把合法 UTF-8 误判成 GB18030。
    """
    candidates = [data]
    if truncated:
        for cut in (1, 2, 3):
            if len(data) > cut:
                candidates.append(data[:-cut])
    for encoding, label in (("utf-8-sig", "utf-8"), ("gb18030", "gb18030")):
        for blob in candidates:
            try:
                return blob.decode(encoding), label
            except UnicodeDecodeError:
                continue
    return data.decode("utf-8", errors="replace"), "utf-8"


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


class OpenInExplorerRequest(BaseModel):
    """Request model for opening a workspace path in the system file explorer."""
    workspace_uuid: str
    path: str = ""  # Relative path; empty = workspace root


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
        # 青少年模式：跨工作区扫描跳过受限工作区（读侧锁）
        if get_teen_mode().workspace_access(uuid) == "locked":
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
        _teen_ws_guard(workspace_uuid)
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
    _teen_ws_guard(workspace_uuid)

    workspace_dir = info.get("directory", "")
    if not workspace_dir:
        raise HTTPException(status_code=404, detail="Workspace directory not configured")

    return _serve_workspace_file(workspace_dir, file_path)


# ----- Directory Browser -----

@router.get("/api/browse")
def browse_directory(request: Request, path: str = ""):
    """Browse directories on the server filesystem.

    Args:
        path: Directory path to browse. Empty string returns system drives (Windows) or root (Unix).

    Returns:
        List of directories with their names and full paths.
    """
    # W8: 目录浏览限本机——工作区文件夹选择是本地管理操作，
    # 避免 LAN/网络攻击者枚举服务器任意磁盘目录结构。
    # 注意（P1-25）：反代部署下此守卫会失效——request.client.host 是代理
    # IP（通常为本机回环），所有经代理的请求都会被视为 localhost。
    # 勿把 /api/browse 暴露到反代公网侧；远程使用需在反代层单独封禁该路径
    # （见 docs/design/web-api-design.md「八、安全考虑」）。
    client_ip = request.client.host if request.client else ""
    if client_ip not in _LOCALHOST_IPS:
        raise HTTPException(
            status_code=403,
            detail="Access denied: browse_directory is localhost-only",
        )

    # 青少年模式：浏览范围限制到允许的工作区目录（§3.13）；
    # 空路径不给盘符列表，直接落到第一个允许工作区
    tm = get_teen_mode()
    if tm.is_enabled():
        import os as _os

        allowed_roots = []
        for entry in load_workspaces_index():
            directory = entry.get("directory", "")
            if directory and tm.workspace_access(entry.get("uuid", "")) == "allow":
                allowed_roots.append(_os.path.normcase(_os.path.abspath(directory)))
        if not allowed_roots:
            raise HTTPException(status_code=403,
                                detail="青少年模式已开启：没有允许使用的工作区，无法浏览目录")
        if path:
            resolved = _os.path.normcase(_os.path.abspath(path))
            inside = False
            for root in allowed_roots:
                try:
                    if _os.path.commonpath([resolved, root]) == root:
                        inside = True
                        break
                except ValueError:
                    continue
            if not inside:
                raise HTTPException(status_code=403,
                                    detail="青少年模式已开启：仅可浏览允许使用的工作区目录")
        else:
            path = allowed_roots[0]

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
    _teen_ws_guard(workspace_uuid)

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
            rel_path = item.relative_to(workspace_dir).as_posix()
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
        parent = target_path.parent.relative_to(workspace_dir).as_posix()
        if parent == ".":
            parent = ""

    return {
        "path": path,
        "items": items,
        "parent": parent
    }


# ----- File Preview (只读文本预览) -----
@router.get("/api/file-preview")
def preview_file(workspace_uuid: str, response: Response, path: str = "") -> dict:
    """Read a workspace text file for the read-only preview pane.

    独立路径（而非 /api/files/preview）避开 /api/files/{file_path:path} 的
    注册顺序陷阱；只读、限长、二进制不解码。
    """
    _validate_workspace_uuid(workspace_uuid)  # W9: 防路径穿越
    ws_config = load_workspace_config(workspace_uuid)
    if not ws_config:
        raise HTTPException(status_code=404, detail="Workspace not found")
    _teen_ws_guard(workspace_uuid)

    workspace_dir = Path(ws_config.get("directory", ""))
    if not workspace_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace directory not found")

    if not path or not path.strip():
        raise HTTPException(status_code=400, detail="path is required")

    workspace_root = workspace_dir.resolve()
    file_path = (workspace_dir / path).resolve()
    if not file_path.is_relative_to(workspace_root):
        raise HTTPException(status_code=403, detail="Access denied: path outside workspace")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {path}")

    try:
        stat = file_path.stat()
        with open(file_path, "rb") as f:
            data = f.read(_PREVIEW_MAX_BYTES + 1)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))

    truncated = len(data) > _PREVIEW_MAX_BYTES
    if truncated:
        data = data[:_PREVIEW_MAX_BYTES]

    response.headers["Cache-Control"] = "no-store"
    rel_path = file_path.relative_to(workspace_root).as_posix()

    if b"\x00" in data[:_PREVIEW_SNIFF_BYTES]:
        return {
            "path": rel_path,
            "name": file_path.name,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "kind": "binary",
            "encoding": None,
            "truncated": False,
            "content": None,
        }

    content, encoding = _decode_preview_text(data, truncated)
    return {
        "path": rel_path,
        "name": file_path.name,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "kind": "text",
        "encoding": encoding,
        "truncated": truncated,
        "content": content,
    }


# ----- Office Preview (Office document -> PDF) -----

def _office_preview_source(workspace_uuid: str, path: str) -> tuple[Path, Path]:
    """Resolve and validate an Office source file inside a workspace."""
    _validate_workspace_uuid(workspace_uuid)  # W9: 防路径穿越
    ws_config = load_workspace_config(workspace_uuid)
    if not ws_config:
        raise HTTPException(status_code=404, detail="Workspace not found")
    _teen_ws_guard(workspace_uuid)

    workspace_dir = Path(ws_config.get("directory", ""))
    if not workspace_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace directory not found")

    if not path or not path.strip():
        raise HTTPException(status_code=400, detail="path is required")

    workspace_root = workspace_dir.resolve()
    file_path = (workspace_dir / path).resolve()
    if not file_path.is_relative_to(workspace_root):
        raise HTTPException(status_code=403, detail="Access denied: path outside workspace")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {path}")

    ext = file_path.suffix.lower()
    if ext not in OFFICE_SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported Office file type: {ext}. Supported: {', '.join(sorted(OFFICE_SUPPORTED_EXTENSIONS))}",
        )
    return workspace_root, file_path


def _office_preview_result(source: Path, workspace_root: Path, workspace_uuid: str) -> tuple[Path, str, bool, str]:
    try:
        pdf_path, engine, cached = ensure_pdf(str(source), timeout=120)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=f"Office conversion timed out: {e}")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"File not found: {e}")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Office conversion failed: {e}")

    rel_path = source.relative_to(workspace_root).as_posix()
    pdf_url = "/api/office-preview/pdf?" + urlencode({
        "workspace_uuid": workspace_uuid,
        "path": rel_path,
        "v": int(source.stat().st_mtime),
    })
    return pdf_path, engine, cached, pdf_url


@router.get("/api/office-preview/status")
def office_preview_status(workspace_uuid: str, path: str = "") -> dict:
    """Convert (or reuse cached) an Office document and return its PDF URL."""
    workspace_root, source = _office_preview_source(workspace_uuid, path)
    pdf_path, engine, cached, pdf_url = _office_preview_result(source, workspace_root, workspace_uuid)
    return {
        "status": "ready",
        "pdf_url": pdf_url,
        "engine": engine,
        "cached": cached,
        "size": pdf_path.stat().st_size,
    }


@router.get("/api/office-preview/pdf")
def office_preview_pdf(workspace_uuid: str, path: str = "") -> FileResponse:
    """Serve the cached PDF for an Office document (supports Range)."""
    workspace_root, source = _office_preview_source(workspace_uuid, path)
    pdf_path, _engine, _cached, _pdf_url = _office_preview_result(source, workspace_root, workspace_uuid)
    return FileResponse(
        str(pdf_path),
        media_type="application/pdf",
        headers={"Cache-Control": "no-store"},
    )


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
    _teen_ws_guard(workspace_uuid)

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
    _teen_ws_guard(request.workspace_uuid)

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
            return {"type": "folder", "path": target_path.relative_to(workspace_dir).as_posix(), "name": request.name}
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(request.content, encoding="utf-8")
            return {"type": "file", "path": target_path.relative_to(workspace_dir).as_posix(), "name": request.name}
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
    _teen_ws_guard(request.workspace_uuid)

    # 青少年模式：文件删除可由家长锁定（默认放开——正常写代码需要删文件）
    tm = get_teen_mode()
    if tm.policy().get("deny_file_delete") and not tm.is_unlocked():
        tm.audit("deny", action="file_delete", source="web")
        tm.record_denied("file_delete")
        raise HTTPException(status_code=403, detail=deny_message("file_delete"))

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
    _teen_ws_guard(request.workspace_uuid)

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
            return {"action": "rename", "path": Path(request.new_path).as_posix()}
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
            return {"action": "save", "path": Path(request.path).as_posix()}
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    raise HTTPException(status_code=400, detail="No action specified: provide new_path or content")


# ----- System Explorer -----

@router.post("/api/open-in-explorer")
def open_in_explorer(request: OpenInExplorerRequest, request_raw: Request = None):
    """Open a workspace path in the OS file explorer (localhost only).

    浏览器无法直接拉起系统资源管理器，由本机服务端进程执行 explorer.exe：
    目录直接打开；文件用 /select 定位。仅限本机调用（与 /api/browse 同）；
    青少年模式开启且未全局解锁时拒绝——资源管理器可越出工作区浏览整盘。
    """
    _csrf_protect(request_raw)  # W6: CSRF 防护
    _validate_workspace_uuid(request.workspace_uuid)  # W9: 防路径穿越

    client_ip = request_raw.client.host if request_raw and request_raw.client else ""
    if client_ip not in _LOCALHOST_IPS:
        raise HTTPException(status_code=403, detail="Access denied: open_in_explorer is localhost-only")

    ws_config = load_workspace_config(request.workspace_uuid)
    if not ws_config:
        raise HTTPException(status_code=404, detail="Workspace not found")
    _teen_ws_guard(request.workspace_uuid)

    tm = get_teen_mode()
    if tm.is_enabled() and not tm.is_unlocked():
        tm.audit("deny", action="open_in_explorer", source="web", ip=client_ip)
        tm.record_denied("open_in_explorer")
        raise HTTPException(status_code=403, detail="青少年模式已开启：不允许打开系统资源管理器")

    workspace_dir = Path(ws_config.get("directory", ""))
    if not workspace_dir.exists():
        raise HTTPException(status_code=404, detail="Workspace directory not found")
    workspace_root = workspace_dir.resolve()

    target = (workspace_dir / request.path).resolve() if request.path else workspace_root
    if not target.is_relative_to(workspace_root):
        raise HTTPException(status_code=403, detail="Access denied: path outside workspace")
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {request.path}")

    if sys.platform != "win32":
        raise HTTPException(status_code=501, detail="Opening the system file explorer is only supported on Windows")

    try:
        if target.is_dir():
            subprocess.Popen(["explorer.exe", str(target)])
        else:
            subprocess.Popen(["explorer.exe", f"/select,{target}"])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to open explorer: {e}")

    return {"ok": True, "path": target.relative_to(workspace_root).as_posix()}


def _sanitize_upload_filename(raw: str) -> str | None:
    """净化上传文件名（实现已下沉 ``core/fs_utils.safe_upload_filename``）。

    QQ 入站附件下载要用同一套规则，故下沉共用；此处保留同名薄封装。
    """
    return safe_upload_filename(raw)


@router.post("/api/files/upload")
def upload_files(
    request: Request,
    workspace_uuid: str = Form(...),
    path: str = Form(""),
    files: list[UploadFile] = File(...),
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
    _teen_ws_guard(workspace_uuid)

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
                errors.append({"name": file.filename, "error": "File too large (max 100MB)"})
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

            # 流式写盘：边写边校验大小。此前整文件 read() 入内存（100MB 上限
            # 形同虚设：size 头缺失时先吃满内存才做校验）
            written = 0
            too_large = False
            with open(file_path, "wb") as out:
                while True:
                    chunk = file.file.read(_UPLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > _MAX_UPLOAD_SIZE:
                        too_large = True
                        break
                    out.write(chunk)

            if too_large:
                file_path.unlink(missing_ok=True)
                errors.append({"name": file.filename, "error": "File too large (max 100MB)"})
                continue

            uploaded.append({"name": file.filename, "size": written})
        except Exception as e:
            errors.append({"name": file.filename, "error": str(e)})

    return {"uploaded": uploaded, "errors": errors}
