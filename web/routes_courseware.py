"""Courseware quiz-result ingestion.

The courseware HTML runs in a sandboxed iframe (opaque origin), so this route
accepts a simple ``text/plain`` JSON body and returns permissive CORS headers.
It only appends records inside an existing workspace; no reads are exposed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from core.config import find_workspace_entry, load_workspaces_index
from core.courseware_records import record_quiz_result
from core.teen_mode import get_teen_mode

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 32 * 1024
_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Cache-Control": "no-store",
}


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin") or ""
    if not origin or origin == "null":
        return True
    try:
        host = urlparse(origin).hostname or ""
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1")


def _workspace_for_request(payload: dict, courseware_path: Path | None) -> tuple[str, Path]:
    uuid = str(payload.get("workspace_uuid") or "").strip()
    if uuid:
        entry = find_workspace_entry(uuid)
        if not entry:
            raise HTTPException(status_code=404, detail="Workspace not found")
        return uuid, Path(entry.get("directory", "")).resolve()

    if courseware_path is not None:
        target = courseware_path.resolve()
        for entry in load_workspaces_index():
            directory = Path(entry.get("directory", "")).resolve()
            try:
                if target.is_relative_to(directory):
                    return str(entry.get("uuid", "")), directory
            except ValueError:
                continue
    raise HTTPException(status_code=400, detail="workspace_uuid required")


@router.options("/api/courseware/quiz-result")
async def quiz_result_options():
    return JSONResponse({}, headers=_CORS)


@router.post("/api/courseware/quiz-result")
async def quiz_result(request: Request):
    if not _origin_allowed(request):
        raise HTTPException(status_code=403, detail="Cross-site request blocked")

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be an object")

    raw_path = str(payload.get("courseware_path") or "").strip()
    courseware_path = Path(raw_path).resolve() if raw_path else None
    uuid, workspace_root = _workspace_for_request(payload, courseware_path)
    get_teen_mode().guard_workspace(uuid, source="web")

    if courseware_path is not None:
        try:
            courseware_path.relative_to(workspace_root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="courseware_path outside workspace") from exc

    try:
        receipt = record_quiz_result(workspace_root, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("courseware quiz record: %s %s", payload.get("courseware"), payload.get("question_id"))
    return JSONResponse({"success": True, **receipt}, headers=_CORS)
