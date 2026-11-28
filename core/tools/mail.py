"""mail 工具：发信 / 回复 / 转发 + 只读查收件箱。

账号来自 ``setting.json → email.accounts``，与邮件接入端共用；按工作区优先级选择
（绑定当前工作区的账号优先，其次是不限定工作区的账号）。

读侧对邮箱零副作用：``SELECT INBOX (readonly)`` + ``BODY.PEEK``，不标记已读、不
动接入端游标。读侧输出（主题、发件人、正文、附件名）都是外部可控文本，统一包
不可信标记。

发信审批：收件人全部同域名、全部命中 ``allowed_recipients``（支持通配符）或全部
在入站白名单 ``allowed_senders`` 内 → 直接发；否则返回审批占位，用户点「允许本次
会话」后重执行才发送。没有审批通道（worker / 无 store）时失败关闭。
"""

from __future__ import annotations

import hashlib
import os
from email.message import Message
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any

from core.fs_utils import is_path_within
from core.mail.accounts import EmailConfig, address_allowed, load_email_accounts
from core.mail.client import (
    EmailAuthError,
    EmailClientError,
    MailClient,
    MailSummary,
    SearchCriteria,
    imap_date,
)
from core.mail.content import extract_text, safe_filename, truncate_body, unique_path
from core.mail.outbound import (
    ReplyContext,
    build_message,
    build_reply,
    clean_header_value,
)
from core.tools.base import Tool, ToolResult
from core.tools.permission import (
    META_KEY,
    permission_decision_id,
    permission_placeholder_text,
)
from core.untrusted import UNTRUSTED_DATA_BEGIN, UNTRUSTED_DATA_END

# 单次 list/search 的最大返回条数
_MAX_LIST_LIMIT = 50
# 单封邮件最多收件人（防止误传超长列表；To 头不再按 400 字截断）
_MAX_RECIPIENTS = 50

# 公共邮箱域名不做「同域名免审批」：否则 me@gmail.com 发给任意 *@gmail.com 都直接放行
_PUBLIC_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "qq.com", "foxmail.com", "163.com", "126.com",
    "yeah.net", "sina.com", "sina.cn", "sohu.com", "139.com", "189.cn",
    "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
    "icloud.com", "me.com", "protonmail.com", "proton.me", "gmx.com",
    "yandex.com", "mail.ru",
})
# save_attachment 单次最多保存的附件数
_MAX_SAVE_ATTACHMENTS = 20

_UID_ACTIONS = frozenset({"read", "reply", "forward", "save_attachment"})
_ACTIONS = ("send", "reply", "forward", "list", "read", "search", "save_attachment")

_BASE_DESCRIPTION = (
    "Send, reply to, forward, or read email from a configured account.\n"
    "- send: new message (to, subject, body, attachments?)\n"
    "- reply: threaded reply to a message in the inbox (uid, body, attachments?)\n"
    "- forward: forward a message with its original attachments (uid, to, body?)\n"
    "- list: recent inbox headers (limit?, unread_only?)\n"
    "- read: headers, attachment names and body of one message (uid)\n"
    "- search: find messages by sender/subject/date (from?, subject?, since?, before?, limit?)\n"
    "- save_attachment: save attachments to the workspace uploads/ dir (uid, name?)\n"
    "Reads never mark messages as read or change mailbox flags. "
    "Attachment paths for send/reply must be inside the workspace. "
    "When more than one account can perform a uid-based action, 'account' is required."
)


def can_send(acc: EmailConfig) -> bool:
    return bool(acc.smtp_host)


def can_read(acc: EmailConfig) -> bool:
    return bool(acc.imap_host)


def usable_accounts() -> list[EmailConfig]:
    """配置层面能用的账号：已启用、有凭证，且至少配了 SMTP 或 IMAP 之一。"""
    return [
        acc for acc in load_email_accounts()
        if acc.enabled and acc.email_address and acc.username and acc.password
        and (acc.smtp_host or acc.imap_host)
    ]


def accounts_for_workspace(workspace_uuid: str) -> list[EmailConfig]:
    """当前工作区可用的账号：绑定当前工作区的优先，其次是不限定工作区的。"""
    usable = usable_accounts()
    current = [
        acc for acc in usable
        if workspace_uuid and acc.default_workspace_uuid == workspace_uuid
    ]
    unbound = [acc for acc in usable if not acc.default_workspace_uuid]
    return current + unbound


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().lower() if "@" in address else ""


def _parse_recipients(value: Any) -> list[str]:
    """解析收件人：分号/中文逗号归一为英文逗号，取地址、去重（大小写不敏感）。"""
    raw = str(value or "").replace(";", ",").replace("，", ",")
    out: list[str] = []
    seen: set[str] = set()
    for _, addr in getaddresses([raw]):
        text = (addr or "").strip()
        if not text or "@" not in text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _fmt_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _summary_line(item: MailSummary) -> str:
    return (
        f"[uid {item.uid}] {item.date or '未知日期'}  "
        f"{item.sender or '未知发件人'}  {_fmt_size(item.size)}  "
        f"主题：{item.subject or '（无主题）'}"
    )


def _attachment_parts(msg: Message) -> list[tuple[str, str, bytes]]:
    out: list[tuple[str, str, bytes]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        out.append((
            safe_filename(filename),
            part.get_content_type() or "application/octet-stream",
            payload,
        ))
    return out


class MailTool(Tool):
    """收发邮件（只读查信 + 受控发信）。"""

    name = "mail"
    concurrency_safe = False
    mutates_workspace = True     # save_attachment 会写 uploads/

    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": "Operation to perform.",
            },
            "account": {
                "type": "string",
                "description": (
                    "Mail account address to use. Optional for send/list/search "
                    "(defaults to the first account); required for uid-based actions "
                    "when more than one account is available."
                ),
            },
            "to": {
                "type": "string",
                "description": "Recipients, comma or semicolon separated.",
            },
            "subject": {"type": "string", "description": "Subject line."},
            "body": {"type": "string", "description": "Plain-text body."},
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File paths inside the workspace.",
            },
            "uid": {
                "type": "integer",
                "description": "Message UID from list/search output.",
            },
            "limit": {
                "type": "integer",
                "description": f"Max results for list/search (default 20, max {_MAX_LIST_LIMIT}).",
            },
            "unread_only": {
                "type": "boolean",
                "description": "For action='list': only unread messages.",
            },
            "from": {
                "type": "string",
                "description": "For action='search': sender substring.",
            },
            "since": {
                "type": "string",
                "description": "For action='search': start date, YYYY-MM-DD (inclusive).",
            },
            "before": {
                "type": "string",
                "description": "For action='search': end date, YYYY-MM-DD (exclusive).",
            },
            "name": {
                "type": "string",
                "description": (
                    "For action='save_attachment': filename substring to pick; "
                    "omit to save all attachments."
                ),
            },
        },
        "required": ["action"],
    }

    @property
    def description(self) -> str:
        """基础说明 + 本工作区可用账号清单（第一个为默认）。"""
        accounts = accounts_for_workspace(self.workspace_uuid)
        if not accounts:
            if usable_accounts():
                return _BASE_DESCRIPTION + (
                    "\nNo account is available in the current workspace "
                    "(configured accounts are bound to other workspaces)."
                )
            return _BASE_DESCRIPTION + "\nNo mail account is configured."
        parts: list[str] = []
        for index, acc in enumerate(accounts):
            caps = [name for name, ok in (("send", can_send(acc)), ("read", can_read(acc))) if ok]
            label = f"{acc.email_address} ({acc.label})" if acc.label else acc.email_address
            prefix = "default: " if index == 0 else ""
            parts.append(f"{prefix}{label} [{', '.join(caps)}]")
        return _BASE_DESCRIPTION + "\nAccounts in this workspace: " + "; ".join(parts)

    # ── 入口 ────────────────────────────────────────────────────────

    def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action") or "").strip()
        if action not in _ACTIONS:
            return ToolResult(
                f"Error: action 必须是 {', '.join(_ACTIONS)} 之一", error=True
            )

        accounts = accounts_for_workspace(self.workspace_uuid)
        if not accounts:
            return self._no_workspace_account_error()

        capable = [acc for acc in accounts if self._action_capable(acc, action)]
        if not capable:
            if action == "send":
                missing = "SMTP"
            elif action in ("reply", "forward"):
                missing = "SMTP 与 IMAP"
            else:
                missing = "IMAP"
            names = "、".join(acc.email_address for acc in accounts)
            return ToolResult(
                f"Error: 当前工作区的账号（{names}）未配置 {missing}，无法执行 {action}",
                error=True,
            )

        selected = self._select_account(capable, kwargs.get("account"), action)
        if isinstance(selected, ToolResult):
            return selected
        acc = selected

        try:
            if action == "send":
                return self._send(acc, kwargs)
            if action == "reply":
                return self._reply(acc, kwargs)
            if action == "forward":
                return self._forward(acc, kwargs)
            if action == "list":
                return self._list(acc, kwargs)
            if action == "read":
                return self._read(acc, kwargs)
            if action == "search":
                return self._search(acc, kwargs)
            return self._save_attachment(acc, kwargs)
        except EmailAuthError as e:
            return ToolResult(f"Error: {e}\n请检查邮箱授权码/应用专用密码。", error=True)
        except EmailClientError as e:
            return ToolResult(f"Error: {e}", error=True)
        except ValueError as e:
            return ToolResult(f"Error: {e}", error=True)

    # ── 账号选择 ────────────────────────────────────────────────────

    @staticmethod
    def _action_capable(acc: EmailConfig, action: str) -> bool:
        """按动作判断账号能力：reply/forward 既要收（IMAP）也要发（SMTP）。"""
        if action == "send":
            return can_send(acc)
        if action in ("reply", "forward"):
            return can_send(acc) and can_read(acc)
        return can_read(acc)

    def _no_workspace_account_error(self) -> ToolResult:
        usable = usable_accounts()
        if not usable:
            return ToolResult(
                "Error: 系统未配置可用的邮箱账号。请在设置 → 邮箱账号里添加并启用一个账号。",
                error=True,
            )
        lines = ["Error: 当前工作区没有可用的邮箱账号。已配置账号："]
        for acc in usable:
            bound = acc.default_workspace_uuid or "（不限定工作区）"
            lines.append(f"- {acc.email_address} → {bound}")
        lines.append("可在设置里把账号的工作区设为当前工作区，或留空（不限定）。")
        return ToolResult("\n".join(lines), error=True)

    def _select_account(
        self, capable: list[EmailConfig], requested: Any, action: str,
    ) -> EmailConfig | ToolResult:
        text = str(requested or "").strip().lower()
        if text:
            for acc in capable:
                if acc.email_address.lower() == text or acc.id.lower() == text:
                    return acc
            names = "、".join(acc.email_address for acc in capable)
            return ToolResult(f"Error: 未知账号 {requested}。可用账号：{names}", error=True)
        if action in _UID_ACTIONS and len(capable) > 1:
            names = "、".join(acc.email_address for acc in capable)
            return ToolResult(
                "Error: 有多个可用账号，请用 account 显式指定（uid 是每个邮箱独立编号，"
                f"按默认账号读可能读到另一封邮件）。可用账号：{names}",
                error=True,
            )
        return capable[0]

    # ── 发信 ────────────────────────────────────────────────────────

    def _send(self, acc: EmailConfig, kwargs: dict) -> ToolResult:
        recipients = _parse_recipients(kwargs.get("to"))
        if not recipients:
            return ToolResult("Error: to 至少需要一个有效邮箱地址", error=True)
        if len(recipients) > _MAX_RECIPIENTS:
            return ToolResult(
                f"Error: 收件人过多（最多 {_MAX_RECIPIENTS} 个）", error=True
            )
        subject = clean_header_value(kwargs.get("subject")) or "（无主题）"
        body = str(kwargs.get("body") or "")
        attachments = self._resolve_attachments(kwargs.get("attachments"))

        # 先组信（校验附件条数/总量），再请求审批——避免用户批准后才报附件错误
        msg = build_message(acc, ", ".join(recipients), subject, body, attachments)
        pending = self._require_approval(
            acc, "send", recipients, subject, body, attachments,
        )
        if pending is not None:
            return pending

        MailClient(acc).send(msg)
        return ToolResult(self._receipt(acc, recipients, subject, len(attachments)))

    def _reply(self, acc: EmailConfig, kwargs: dict) -> ToolResult:
        uid = self._uid(kwargs)
        if isinstance(uid, ToolResult):
            return uid
        body = str(kwargs.get("body") or "")
        attachments = self._resolve_attachments(kwargs.get("attachments"))

        result = MailClient(acc).read_message(uid, max_bytes=acc.max_inbound_bytes)
        if result.too_large:
            return ToolResult("Error: 原邮件过大，未下载正文，无法回复", error=True)
        if result.message is None:
            return self._missing_message(result, uid)
        original = result.message
        target = self._reply_target(original)
        if not target:
            return ToolResult("Error: 原邮件没有可用的发件人地址", error=True)

        ctx = ReplyContext(
            sender=target,
            subject=str(original.get("Subject") or ""),
            message_id=str(original.get("Message-ID") or ""),
            references=str(original.get("References") or ""),
        )
        subject = ctx.subject or "（无主题）"
        msg = build_reply(acc, ctx, body, attachments, auto_submitted=False)
        pending = self._require_approval(
            acc, "reply", [target], subject, body, attachments,
            original_id=ctx.message_id,
        )
        if pending is not None:
            return pending

        MailClient(acc).send(msg)
        receipt_subject = str(msg.get("Subject") or subject)
        return ToolResult(self._receipt(acc, [target], receipt_subject, len(attachments)))

    def _forward(self, acc: EmailConfig, kwargs: dict) -> ToolResult:
        uid = self._uid(kwargs)
        if isinstance(uid, ToolResult):
            return uid
        recipients = _parse_recipients(kwargs.get("to"))
        if not recipients:
            return ToolResult("Error: to 至少需要一个有效邮箱地址", error=True)
        if len(recipients) > _MAX_RECIPIENTS:
            return ToolResult(
                f"Error: 收件人过多（最多 {_MAX_RECIPIENTS} 个）", error=True
            )
        note = str(kwargs.get("body") or "")

        result = MailClient(acc).read_message(uid, max_bytes=acc.max_inbound_bytes)
        if result.too_large:
            return ToolResult("Error: 原邮件过大，未下载正文，无法转发", error=True)
        if result.message is None:
            return self._missing_message(result, uid)
        original = result.message
        inline = _attachment_parts(original)

        subject = self._forward_subject(str(original.get("Subject") or ""))
        forward_body = self._forward_body(original, note, acc)
        msg = build_message(
            acc, ", ".join(recipients), subject, forward_body, [],
            inline_attachments=inline,
        )
        pending = self._require_approval(
            acc, "forward", recipients, subject, forward_body, [],
            original_id=str(original.get("Message-ID") or ""),
        )
        if pending is not None:
            return pending

        MailClient(acc).send(msg)
        return ToolResult(self._receipt(acc, recipients, subject, len(inline)))

    # ── 读信 ────────────────────────────────────────────────────────

    def _list(self, acc: EmailConfig, kwargs: dict) -> ToolResult:
        limit = self._limit(kwargs)
        unread_only = bool(kwargs.get("unread_only"))
        total, items = MailClient(acc).list_recent(limit, unread_only=unread_only)

        title = "未读" if unread_only else "共"
        lines = [f"账号：{acc.email_address}", f"{title} {total} 封，显示最近 {len(items)} 封"]
        if total > len(items):
            lines.append(f"（仅显示最近 {len(items)} 封，更早的邮件用 search 查找）")
        lines.extend(_summary_line(item) for item in items)
        return ToolResult(self._wrap("\n".join(lines)))

    def _read(self, acc: EmailConfig, kwargs: dict) -> ToolResult:
        uid = self._uid(kwargs)
        if isinstance(uid, ToolResult):
            return uid
        result = MailClient(acc).read_message(uid, max_bytes=acc.max_inbound_bytes)
        if result.message is None and result.headers is None:
            return ToolResult(f"Error: 找不到 uid={uid} 的邮件", error=True)

        if result.too_large:
            head = self._header_block(result.headers)
            return ToolResult(self._wrap(
                f"{head}\n\n邮件大小 {_fmt_size(result.size)}，超过上限，未下载正文。"
            ))
        if result.message is None:
            return ToolResult(
                f"Error: 读取 uid={uid} 的邮件失败（正文下载失败），请稍后重试",
                error=True,
            )

        msg = result.message
        head = self._header_block(msg)
        attachments = _attachment_parts(msg)
        attach_lines = (
            "\n附件：\n" + "\n".join(
                f"- {name}（{_fmt_size(len(payload))}）"
                for name, _, payload in attachments
            )
            if attachments
            else "\n附件：无"
        )
        body = truncate_body(extract_text(msg), acc.max_body_chars)
        content = f"{head}{attach_lines}\n\n正文：\n{body or '（无正文）'}"
        return ToolResult(self._wrap(content))

    def _search(self, acc: EmailConfig, kwargs: dict) -> ToolResult:
        from_text = str(kwargs.get("from") or "").strip()
        subject_text = str(kwargs.get("subject") or "").strip()
        since = str(kwargs.get("since") or "").strip()
        before = str(kwargs.get("before") or "").strip()
        if not any((from_text, subject_text, since, before)):
            return ToolResult(
                "Error: search 至少需要 from / subject / since / before 之一", error=True
            )
        for value in (since, before):
            if not value:
                continue
            try:
                imap_date(value)
            except ValueError as e:
                return ToolResult(f"Error: {e}", error=True)
        criteria = SearchCriteria(
            from_text=from_text, subject_text=subject_text, since=since, before=before,
        )
        limit = self._limit(kwargs)
        items, candidates_truncated = MailClient(acc).search_recent(criteria, limit)

        condition = "；".join(
            part for part in (
                f"发件人含「{from_text}」" if from_text else "",
                f"主题含「{subject_text}」" if subject_text else "",
                f"起始 {since}" if since else "",
                f"至 {before}" if before else "",
            ) if part
        )
        lines = [f"账号：{acc.email_address}", f"检索条件：{condition or '无'}",
                 f"命中 {len(items)} 封"]
        lines.extend(_summary_line(item) for item in items)
        if candidates_truncated:
            lines.append("（只扫描了最近 50 封候选，可能还有更早的匹配）")
        return ToolResult(self._wrap("\n".join(lines)))

    def _save_attachment(self, acc: EmailConfig, kwargs: dict) -> ToolResult:
        uid = self._uid(kwargs)
        if isinstance(uid, ToolResult):
            return uid
        name_filter = str(kwargs.get("name") or "").strip().lower()

        result = MailClient(acc).read_message(uid, max_bytes=acc.max_inbound_bytes)
        if result.too_large:
            return ToolResult("Error: 邮件过大，超过单封上限，未下载附件", error=True)
        if result.message is None:
            return self._missing_message(result, uid)

        parts = _attachment_parts(result.message)
        if not parts:
            return ToolResult("该邮件没有附件")
        names = [name for name, _, _ in parts]
        if name_filter:
            parts = [p for p in parts if name_filter in p[0].lower()]
            if not parts:
                return ToolResult(
                    f"Error: 没有匹配「{kwargs.get('name')}」的附件。现有附件："
                    + self._wrap("、".join(names)),
                    error=True,
                )
        if len(parts) > _MAX_SAVE_ATTACHMENTS:
            return ToolResult(
                f"Error: 附件数量超过上限（最多 {_MAX_SAVE_ATTACHMENTS} 个，"
                f"实际 {len(parts)} 个）",
                error=True,
            )
        oversized = [name for name, _, payload in parts if len(payload) > acc.max_inbound_bytes]
        if oversized:
            return ToolResult(
                "Error: 以下附件超过单件上限：" + "、".join(oversized), error=True
            )

        uploads = Path(self.cwd) / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for filename, _, payload in parts:
            path = unique_path(uploads, filename)
            path.write_bytes(payload)
            try:
                rel = str(path.relative_to(self.cwd)).replace("\\", "/")
            except ValueError:
                rel = str(path)
            saved.append(f"- {rel}（{_fmt_size(len(payload))}）")
        return ToolResult("已保存附件到工作区：\n" + self._wrap("\n".join(saved)))

    # ── 审批 ────────────────────────────────────────────────────────

    def _require_approval(
        self,
        acc: EmailConfig,
        action: str,
        recipients: list[str],
        subject: str,
        body: str,
        attachments: list[str],
        *,
        original_id: str = "",
    ) -> ToolResult | None:
        """返回 None 表示可直接发送；否则返回审批占位或失败结果。"""
        if self._approval_exempt(acc, recipients):
            return None
        decision_id = self._decision_id(
            acc, action, recipients, subject, body, attachments, original_id,
        )
        store = self.permission_store
        if store is None:
            return ToolResult(
                "Error: 该收件人需要用户确认，但当前没有审批通道，已取消发送。",
                error=True,
            )
        if store.is_approved(decision_id):
            return None

        summary = (
            f"发送邮件 {acc.email_address} → {', '.join(recipients)}；"
            f"主题：{subject[:80]}"
        )
        approval = self._pending_permission(
            summary, decision_id, [], [],
            reason="收件人不在同域名或白名单内",
            mode="ask",
        )
        approval["summary"] = "跨域发信需要确认"
        # 该动作无法生成持久规则（工具只认会话批准），卡片不提供「一直允许」
        approval["dynamic_kind"] = "opaque"
        return ToolResult(
            permission_placeholder_text(approval),
            completed=False,
            meta={META_KEY: approval},
        )

    @staticmethod
    def _approval_exempt(acc: EmailConfig, recipients: list[str]) -> bool:
        if not recipients:
            return True
        # 发给自己永远放行（公共域名也不该拦自寄）
        self_key = acc.email_address.strip().lower()
        if all(addr.strip().lower() == self_key for addr in recipients):
            return True
        sender_domain = _domain(acc.email_address)
        if (
            sender_domain
            and sender_domain not in _PUBLIC_MAIL_DOMAINS
            and all(_domain(addr) == sender_domain for addr in recipients)
        ):
            return True
        if all(address_allowed(addr, acc.allowed_recipients) for addr in recipients):
            return True
        senders = {str(s).strip().lower() for s in acc.allowed_senders or ()}
        return all(addr.strip().lower() in senders for addr in recipients)

    @staticmethod
    def _decision_id(
        acc: EmailConfig,
        action: str,
        recipients: list[str],
        subject: str,
        body: str,
        attachments: list[str],
        original_id: str,
    ) -> str:
        digest = hashlib.sha256(body.encode("utf-8"))
        for path in attachments:
            try:
                st = os.stat(path)
            except OSError:
                continue
            digest.update(f"{os.path.realpath(path)}:{st.st_size}:{st.st_mtime_ns}".encode())
        if original_id:
            digest.update(original_id.encode("utf-8"))
        payload = "|".join([
            action,
            acc.email_address.lower(),
            ",".join(sorted(addr.lower() for addr in recipients)),
            subject,
            digest.hexdigest(),
        ])
        return permission_decision_id(payload)

    # ── 辅助 ────────────────────────────────────────────────────────

    def _resolve_attachments(self, raw: Any) -> list[str]:
        """附件路径解析：必须在工作区内、存在且是普通文件；任一不合格整批拒绝。"""
        if isinstance(raw, str):
            items = [raw]
        elif isinstance(raw, (list, tuple)):
            items = [str(item) for item in raw]
        else:
            items = []
        workspace_real = os.path.realpath(self.cwd)
        paths: list[str] = []
        problems: list[str] = []
        for item in items:
            text = item.strip()
            if not text:
                continue
            resolved = self._resolve_path(text)
            if not is_path_within(resolved, workspace_real):
                problems.append(f"{text}: 不在工作区内")
                continue
            if not os.path.exists(resolved):
                problems.append(f"{text}: 不存在")
                continue
            if not os.path.isfile(resolved):
                problems.append(f"{text}: 不是文件")
                continue
            paths.append(resolved)
        if problems:
            raise ValueError("附件校验失败，整批未发送：\n- " + "\n- ".join(problems))
        return paths

    def _uid(self, kwargs: dict) -> int | ToolResult:
        raw = kwargs.get("uid")
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            return ToolResult("Error: uid 必须是整数（从 list/search 输出获取）", error=True)
        if uid <= 0:
            return ToolResult("Error: uid 必须是正整数", error=True)
        return uid

    @staticmethod
    def _missing_message(result, uid: int) -> ToolResult:
        """区分「uid 不存在」与「正文下载失败」——后者不该让模型换 uid 重试。"""
        if result.headers is not None:
            return ToolResult(
                f"Error: 读取 uid={uid} 的邮件失败（正文下载失败），请稍后重试",
                error=True,
            )
        return ToolResult(
            f"Error: 找不到 uid={uid} 的邮件（先用 list 查最新 uid）", error=True
        )

    @staticmethod
    def _limit(kwargs: dict) -> int:
        try:
            limit = int(kwargs.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        return max(1, min(limit, _MAX_LIST_LIMIT))

    @staticmethod
    def _reply_target(msg: Message) -> str:
        for name in ("Reply-To", "From"):
            _, addr = parseaddr(str(msg.get(name) or ""))
            addr = (addr or "").strip()
            if addr and "@" in addr:
                return addr
        return ""

    @staticmethod
    def _forward_subject(subject: str) -> str:
        text = clean_header_value(subject) or "（无主题）"
        if text.lower().startswith(("fwd:", "fw:")):
            return text
        return f"Fwd: {text}"

    @staticmethod
    def _forward_body(original: Message, note: str, acc: EmailConfig) -> str:
        header = "\n".join([
            f"发件人: {original.get('From') or ''}",
            f"日期: {original.get('Date') or ''}",
            f"主题: {original.get('Subject') or ''}",
            f"收件人: {original.get('To') or ''}",
        ])
        text = truncate_body(extract_text(original), acc.max_body_chars)
        separator = "---------- 转发的邮件 ----------"
        body = f"{note.strip()}\n\n{separator}\n{header}\n\n{text}".strip()
        return body

    @staticmethod
    def _receipt(acc: EmailConfig, recipients: list[str], subject: str, count: int) -> str:
        lines = [
            "已发送",
            f"发件人：{acc.email_address}",
            f"收件人：{', '.join(recipients)}",
            f"主题：{subject}",
        ]
        if count:
            lines.append(f"附件：{count} 个")
        return "\n".join(lines)

    @staticmethod
    def _header_block(msg: Message | None) -> str:
        if msg is None:
            return ""
        return "\n".join([
            f"From: {msg.get('From') or ''}",
            f"To: {msg.get('To') or ''}",
            f"Cc: {msg.get('Cc') or ''}",
            f"Date: {msg.get('Date') or ''}",
            f"Subject: {msg.get('Subject') or ''}",
            f"Message-ID: {msg.get('Message-ID') or ''}",
        ])

    @staticmethod
    def _wrap(text: str) -> str:
        """读侧输出统一包不可信标记（主题/发件人/正文/附件名都是外部可控文本）。"""
        return f"{UNTRUSTED_DATA_BEGIN}{text}{UNTRUSTED_DATA_END}"