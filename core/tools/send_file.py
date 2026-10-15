"""send_file 工具 —— 把工作区内的文件登记为「发给用户」。

与聊天类接入端（QQ/微信）配套：agent 生成报告/图片/产物后，显式调用本工具把
它送到用户的聊天窗口。工具本身**不做上传**，只做校验并入队 ``core/outbox``，
由当前接入端在回合末取走发送（QQ/微信走富媒体上传，Web 目前取走后丢弃并记
日志——下载卡片是第二阶段）。

按回合门控
----------
本工具是**接口专属**：只有声明了本工具的接口驱动回合时才出现在模型的工具列表
里（见 ``core/session_runner.py`` 的 ``INTERFACE_TOOLS``）。其他回合里它既不在
``tool_schemas``、也不在 ``tool_search`` 的可激活集合里，硬调会被拒。

描述按接口生成
--------------
``description`` 是 property：基础描述与接口无关，接口相关约束（体积上限、
哪些类型内嵌成图片、交付形态）由当前回合的 ``InterfaceProfile`` 追加。档案由
``SessionRunner.set_turn_interface`` 推送（``set_interface_profile``）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.outbox import enqueue_file
from core.tools.base import Tool, ToolResult

if TYPE_CHECKING:  # 仅类型标注；运行时档案由 set_interface_profile 注入
    from core.interface_profile import InterfaceProfile

logger = logging.getLogger(__name__)

# QQ 富媒体的硬限制是 200MB（图片/视频/语音/文件同值，见官方「文件类型与限制」表）。
# 各类型的软限制更严（图片 20MB / 视频 30MB），超软限制会被降级为文件卡片，
# 那是 QQ 侧 media.py 的事；这里只挡硬限制（作为兜底校验保留在 execute 里）。
MAX_FILE_BYTES = 200 * 1024 * 1024

# 与接口无关的基础描述；接口相关约束由 description property 按 InterfaceProfile 追加
_BASE_DESCRIPTION = (
    "Send a file from the workspace to the user's chat window.\n"
    "Use this when the user asked for a file (report, image, export, archive) "
    "and you have written it to disk. The file is delivered after the current turn.\n"
    "Constraints: the file must exist inside the workspace."
)


class SendFileTool(Tool):
    """把工作区内的文件发送给用户（接口专属，见 INTERFACE_TOOLS）。"""

    name = "send_file"
    # 当前回合的接口能力档案；未声明（None）时 description 只含基础部分
    _profile: "InterfaceProfile | None" = None
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path to the file, relative to the workspace (e.g. 'uploads/report.pdf')."
                ),
            },
            "note": {
                "type": "string",
                "description": "Optional short caption shown alongside the file.",
            },
        },
        "required": ["path"],
    }

    def set_interface_profile(self, profile: "InterfaceProfile | None") -> None:
        """接收当前回合的接口能力档案（见 Tool.set_interface_profile）。"""
        self._profile = profile

    @property
    def description(self) -> str:
        """按当前接口档案生成描述：基础部分 + 接口相关约束。

        ``to_schema()`` 与工具列表段都读本属性；``_apply_tool_visibility``
        在档案推送之后重建 ``tool_schemas``，所以描述变更立即生效。
        """
        parts = [_BASE_DESCRIPTION]
        profile = self._profile
        if profile is not None:
            if profile.file_delivery == "link":
                parts.append(
                    "On this client the user gets a download link in the transcript."
                )
            if profile.inline_image_exts:
                exts = "/".join(
                    e.lstrip(".").upper() for e in sorted(profile.inline_image_exts)
                )
                parts.append(
                    f"Only {exts} arrive as image messages; "
                    "other types arrive as file cards."
                )
            if profile.file_size_limit_mb:
                parts.append(f"Max size on this client: {profile.file_size_limit_mb}MB.")
        return "\n".join(parts)

    def execute(self, **kwargs: Any) -> ToolResult:
        raw_path = (kwargs.get("path") or "").strip()
        note = (kwargs.get("note") or "").strip()
        if not raw_path:
            return ToolResult("Error: path is required", error=True)

        # _resolve_path 已做 realpath（解析 .. 与符号链接）并相对 cwd 展开，
        # 因此裸文件名天然按工作区查找。
        target = Path(self._resolve_path(raw_path))

        # 工作区边界**先于存在性**判定：否则工作区外文件「是否存在」会通过
        # 错误文案泄露出去（存在性 oracle）。两侧都 realpath 后比前缀，防符号链接绕过。
        workspace_real = os.path.realpath(self.cwd)
        try:
            inside = os.path.commonpath([str(target), workspace_real]) == workspace_real
        except ValueError:
            # 不同盘符（Windows）→ 必然不在工作区内
            inside = False
        if not inside:
            return ToolResult(
                f"Error: file is outside the workspace: {raw_path}", error=True
            )

        if not target.exists():
            return ToolResult(f"Error: file not found: {raw_path}", error=True)
        if not target.is_file():
            return ToolResult(f"Error: not a file: {raw_path}", error=True)

        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            return ToolResult(
                f"Error: file too large ({size / 1024 / 1024:.1f}MB, max 200MB): {raw_path}",
                error=True,
            )

        session_key = self._session_key()
        if not session_key:
            return ToolResult(
                "Error: cannot determine the current session; file not queued", error=True
            )

        rel = os.path.relpath(str(target), workspace_real)
        if not enqueue_file(session_key, str(target), note):
            return ToolResult(
                "Error: too many pending files for this turn; try again in a later turn",
                error=True,
            )

        logger.info(f"[send_file] 已登记待发文件: {target} (session={session_key})")
        caption = f"（说明：{note}）" if note else ""
        return ToolResult(f"Queued for delivery to the user: {rel}{caption}")

    def _session_key(self) -> str:
        """当前会话键。session 可能是 SessionStore 或 _SessionRef，二者都有 session_id。"""
        session_id = getattr(self.session, "session_id", "") or ""
        if not session_id:
            return ""
        return f"{self.workspace_uuid}:{session_id}"
