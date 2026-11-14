"""会话「关于会话」：只读时间线接口。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from core.session import INDEX_FILE
from core.session_timeline import build_timeline
from web.deps import _require_workspace, _validate_session_id

router = APIRouter()


@router.get("/api/workspaces/{workspace_uuid}/sessions/{session_id}/timeline")
async def get_session_timeline(
    workspace_uuid: str,
    session_id: str,
    types: str = "",
    ws_dir: Path = Depends(_require_workspace),
):
    _validate_session_id(session_id)
    session_dir = ws_dir / "sessions" / session_id
    if not (session_dir / INDEX_FILE).exists():
        raise HTTPException(status_code=404, detail="Session not found")

    data = build_timeline(session_dir)
    data["session"]["workspace_uuid"] = workspace_uuid

    wanted = {item.strip() for item in types.split(",") if item.strip()}
    if wanted:
        data["events"] = [
            event for event in data["events"] if event.get("type") in wanted
        ]
    return data
