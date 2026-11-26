"""收件箱通知工具：把用户稍后需要知道的信息写进全局收件箱。"""

from __future__ import annotations

import logging

from core.inbox import (
    MAX_PER_SESSION_PER_MINUTE,
    MAX_TOOL_BODY_CHARS,
    get_inbox_store,
)
from core.tools.background import owner_session_id
from core.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


class SendNotificationTool(Tool):
    name = "send_notification"
    description = (
        "**Send a persistent notification to the user's inbox.**\n"
        "Use this when the user should know something later, but you should not "
        "interrupt the current conversation. The user reads it on the web inbox; "
        "other interfaces do not display it.\n\n"
        "- `title` (required): one-line conclusion, at most 120 characters.\n"
        "- `body` (optional): Markdown details, at most 4000 characters. Keep it "
        "self-contained: the user may open it days later, outside this session.\n"
        "- `level` (optional): info / success / warning / error.\n\n"
        "Do NOT use this to ask the user a question (use `ask_user`), deliver a "
        "file (use `send_user_file`), or message another agent (use `message_bus`). "
        "Do not send duplicates: merge related results into one notification. "
        "At most 10 notifications per session per minute are accepted."
    )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "One-line title for the user (at most 120 chars).",
                },
                "body": {
                    "type": "string",
                    "description": "Optional Markdown details (at most 4000 chars).",
                },
                "level": {
                    "type": "string",
                    "enum": ["info", "success", "warning", "error"],
                    "description": "Visual level; default 'info'.",
                    "default": "info",
                },
            },
            "required": ["title"],
        }

    def execute(
        self,
        title: str = "",
        body: str = "",
        level: str = "info",
        **_kwargs,
    ) -> ToolResult:
        clean_title = str(title or "").strip()
        if not clean_title:
            return ToolResult("Error: 'title' is required for send_notification", error=True)

        store = get_inbox_store()
        # worker/lite 的 SessionIdentity.session_id 是 exec_id（不是可加载的
        # 工作区会话）；通知必须归属根会话，否则「打开会话」会 404。
        session_id = owner_session_id(self.session)

        # 限流：同一会话 60 秒内最多 10 条工具通知，防止模型循环刷屏。
        if session_id and store.count_recent(
            session_id, seconds=60, source_type="tool"
        ) >= MAX_PER_SESSION_PER_MINUTE:
            return ToolResult(
                "Error: too many notifications for this session in the last minute. "
                "Merge related results into one notification, or wait before sending again.",
                error=True,
            )

        workspace_uuid = str(self.workspace_uuid or "")
        workspace_name = ""
        if workspace_uuid:
            try:
                from core.config import get_workspace_meta

                workspace_name = str(
                    get_workspace_meta(workspace_uuid).get("workspace_name") or ""
                )
            except Exception:  # noqa: BLE001 —— 快照失败不影响通知
                logger.debug("[inbox] 读取工作区名称失败: %s", workspace_uuid, exc_info=True)

        session_title = str(getattr(self.session, "name", "") or "")
        if session_title == "New Session":
            session_title = ""

        profile = getattr(self, "interface_profile", None)
        interface = str(getattr(profile, "name", "") or "") if profile is not None else ""
        agent = str(getattr(self, "agent_role", "") or "")

        try:
            item = store.add(
                clean_title,
                body,
                level=level,
                source_type="tool",
                agent=agent,
                interface=interface,
                workspace_uuid=workspace_uuid,
                workspace_name=workspace_name,
                session_id=session_id,
                session_title=session_title,
                max_body_chars=MAX_TOOL_BODY_CHARS,
            )
        except Exception as exc:  # noqa: BLE001 —— 写盘失败必须让模型知道
            logger.exception("[inbox] 工具写通知失败")
            return ToolResult(f"Error: failed to send notification: {exc}", error=True)

        if item is None:
            return ToolResult("Error: failed to send notification", error=True)
        return ToolResult(
            f"Notification sent to inbox (id={item.id}). "
            "The user will see it in the web inbox."
        )
