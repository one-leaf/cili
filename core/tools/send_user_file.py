"""send_user_file 工具 —— 把工作区内的文件登记为「发给用户」。

与接入端配套：agent 生成报告/图片/产物后，显式调用本工具把它们送到用户面前。
工具本身**不做上传**，只做校验并入队 ``core/outbox``，由当前接入端在回合末取走
交付（QQ/微信走富媒体上传，Web 落下载卡片，TUI 不可见——终端形态决定）。

一次调用提交**一批**（``paths``）：交付意图天然是批的，逐件调用只会让模型在
条数受限的接口上反复试错。任一件不合格则**整批拒绝**——部分接受会让模型难以
推断实际交付了什么。

按回合门控
----------
本工具是**接口专属**：只有声明了本工具的接口驱动回合时才出现在模型的工具列表
里（见 ``core/session_runner.py`` 的 ``INTERFACE_TOOLS``）。其他回合里它既不在
``tool_schemas``、也不在 ``tool_search`` 的可激活集合里，硬调会被拒。

描述按接口生成
--------------
``description`` 是 property：基础描述与接口无关，接口相关约束（条数上限、体积
上限、哪些类型内嵌成图片、交付形态）由当前回合的 ``InterfaceProfile`` 追加。
档案由 ``SessionRunner.set_turn_interface`` 推送（``set_interface_profile``）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.outbox import enqueue_files, pending_count
from core.tools.base import Tool, ToolResult

if TYPE_CHECKING:  # 仅类型标注；运行时档案由 set_interface_profile 注入
    from core.interface_profile import InterfaceProfile

logger = logging.getLogger(__name__)

# 体积与条数上限都不在工具里写死：各接口的约束在 InterfaceProfile
# （file_size_limit_mb / max_files_per_turn），execute 按当前回合的档案校验，
# 无档案不设限。QQ 各类型的软限制更严（图片 20MB / 视频 30MB），超软限制会被
# 降级为文件卡片，那是 QQ 侧 media.py 的事，这里不重复。

# 与接口无关的基础描述；接口相关约束由 description property 按 InterfaceProfile 追加
_BASE_DESCRIPTION = (
    "Send files from the workspace to the user.\n"
    "Use this when the user asked for files (reports, images, exports, archives) "
    "and you have written them to disk. Delivery happens automatically after the "
    "current turn — do not also link or inline them in your reply.\n"
    "Constraints: the files must exist inside the workspace."
)


class SendUserFileTool(Tool):
    """把工作区内的文件发送给用户（接口专属，见 INTERFACE_TOOLS）。"""

    name = "send_user_file"
    # 当前回合的接口能力档案；未声明（None）时 description 只含基础部分
    _profile: "InterfaceProfile | None" = None
    parameters = {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Paths to the files, relative to the workspace "
                    "(e.g. ['uploads/report.pdf', 'charts/trend.png'])."
                ),
            },
            "note": {
                "type": "string",
                "description": "Optional short caption shown alongside the files.",
            },
        },
        "required": ["paths"],
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
            if profile.max_files_per_turn:
                parts.append(
                    f"At most {profile.max_files_per_turn} files per turn on this client."
                )
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
                parts.append(
                    f"Max size per file on this client: {profile.file_size_limit_mb}MB."
                )
        return "\n".join(parts)

    def execute(self, **kwargs: Any) -> ToolResult:
        raw_paths = kwargs.get("paths")
        note = (kwargs.get("note") or "").strip()
        # 容忍单件形态：模型偶尔传裸字符串而不是数组
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        paths = [str(p).strip() for p in (raw_paths or []) if str(p).strip()]
        if not paths:
            return ToolResult("Error: paths is required (a non-empty list)", error=True)

        workspace_real = os.path.realpath(self.cwd)
        profile = self._profile

        # ── 逐件校验：工作区边界 → 存在性 → 普通文件 → 体积 ──
        # 边界**先于存在性**判定：否则工作区外文件「是否存在」会通过错误文案泄露
        # 出去（存在性 oracle）。两侧都 realpath 后比前缀，防符号链接绕过。
        targets: list[Path] = []
        problems: list[str] = []
        for raw in paths:
            # _resolve_path 已做 realpath（解析 .. 与符号链接）并相对 cwd 展开，
            # 因此裸文件名天然按工作区查找。
            target = Path(self._resolve_path(raw))
            try:
                inside = (
                    os.path.commonpath([str(target), workspace_real]) == workspace_real
                )
            except ValueError:
                # 不同盘符（Windows）→ 必然不在工作区内
                inside = False
            if not inside:
                problems.append(f"{raw}: outside the workspace")
                continue
            if not target.exists():
                problems.append(f"{raw}: not found")
                continue
            if not target.is_file():
                problems.append(f"{raw}: not a file")
                continue
            limit_mb = profile.file_size_limit_mb if profile else None
            size = target.stat().st_size
            if limit_mb and size > limit_mb * 1024 * 1024:
                problems.append(
                    f"{raw}: too large ({size / 1024 / 1024:.1f}MB, max {limit_mb}MB)"
                )
                continue
            targets.append(target)

        session_key = self._session_key()
        if not session_key:
            return ToolResult(
                "Error: cannot determine the current session; files not queued",
                error=True,
            )

        if problems:
            return ToolResult(
                "Error: nothing was queued. Deliver fewer files, or offer the rest in "
                "a later turn.\n- " + "\n- ".join(problems),
                error=True,
            )

        # ── 登记：条数与去重都在 outbox 里判 ──
        # 条数上限在**登记时**判，而不是等交付层发现配额不够再截断——那样模型已经
        # 收到「已登记」的回执，却永远不知道文件没发出去。容量只计**新增**条目，
        # 重复登记同一路径不占额度（模型重发同一产物不该被判超限）。
        limit = profile.max_files_per_turn if profile else None
        result = enqueue_files(
            session_key, [(str(t), note) for t in targets], limit=limit
        )

        def _rel(p: str) -> str:
            return os.path.relpath(p, workspace_real).replace("\\", "/")

        caption = f" Note: {note}" if note else ""
        if result.overflow:
            detail = (
                f"this client delivers at most {limit} per turn"
                if limit
                else "the delivery queue is full"
            )
            return ToolResult(
                f"Error: nothing was queued — {detail} "
                f"({pending_count(session_key)} already queued this turn). "
                f"Deliver fewer files, or offer the rest in a later turn.",
                error=True,
            )
        if not result.accepted:
            # 全部已在队列里：不是失败，只是没有新增（同回合重复登记同一产物）
            return ToolResult(
                f"already queued for delivery (nothing new): "
                f"{', '.join(_rel(p) for p in result.duplicates)}.{caption}"
            )

        rels = [_rel(p) for p in result.accepted]
        logger.info(
            f"[send_user_file] 已登记 {len(result.accepted)} 个待发文件: {rels} "
            f"(session={session_key})"
        )
        skipped = (
            f" Already queued, skipped: {', '.join(_rel(p) for p in result.duplicates)}."
            if result.duplicates
            else ""
        )
        return ToolResult(
            f"Queued {len(result.accepted)} file(s) for delivery: {', '.join(rels)}."
            f"{skipped} They are delivered automatically after this turn — do not "
            f"link or inline them in your reply.{caption}"
        )

    def _session_key(self) -> str:
        """当前会话键。session 可能是 SessionStore 或 _SessionRef，二者都有 session_id。"""
        session_id = getattr(self.session, "session_id", "") or ""
        if not session_id:
            return ""
        return f"{self.workspace_uuid}:{session_id}"
