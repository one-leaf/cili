"""suggest 路由域：输入框推荐输入生成。

与会话加载/发消息共用 ``_get_or_create_runner``——建议用的就是下一轮对话的那个
runner；不 claim 运行权、不写会话。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.suggest_service import SuggestError, generate_suggestions
from web.deps import (
    _get_or_create_runner,
    _require_workspace,
    _validate_session_id,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# 生成总超时：cold prefill（含切换/压缩后的历史）可能较慢
_SUGGEST_TIMEOUT_S = 60.0


class SuggestRequest(BaseModel):
    refresh: bool = False
    exclude: list[str] = Field(default_factory=list)  # 上一批建议（换一批去重）


@router.post("/api/workspaces/{workspace_uuid}/sessions/{session_id}/suggest")
async def suggest(workspace_uuid: str, session_id: str, request: SuggestRequest,
                  ws_dir=Depends(_require_workspace)):
    _validate_session_id(session_id)
    session_dir = ws_dir / "sessions" / session_id
    if not session_dir.is_dir():
        raise HTTPException(status_code=404, detail="会话不存在")

    runner = await _get_or_create_runner(workspace_uuid, session_id)

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                generate_suggestions, runner, request.refresh, request.exclude,
            ),
            timeout=_SUGGEST_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("[suggest] 生成超时（%ss）", _SUGGEST_TIMEOUT_S)
        raise HTTPException(status_code=503, detail="生成超时，请稍后再试")
    except SuggestError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return result
