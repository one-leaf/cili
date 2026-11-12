"""权限裁决层：把配置、规则、会话批准合成一个 allow / ai / ask / deny 结论。

优先级（与设计文档一致）：

1. 青少年 / 执行层不变式（由调用方先处理）
2. 内置 deny
3. 规则引擎 deny（任一段命中）
4. 规则引擎 ask（任一段命中）
5. 规则引擎 allow（命令每段覆盖 / 路径命中）或内置只读白名单
6. 安全区（工作区 / 系统临时目录）
7. 分项策略（default_mode / outside.read|write|delete|navigate）
8. ai → lite 判定；ask → 卡片；deny → 拒绝
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

from core.config import PermissionConfig
from core.fs_utils import is_path_within, os_temp_root
from core.security.permission_rules import (
    PermissionRules,
    is_readonly_command,
    is_safe_create_command,
    match_builtin_deny,
)

_RANK = {"allow": 0, "ai": 1, "ask": 2, "deny": 3}

_ABS_PATH_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/][^\s\"'|;&<>]*|\\\\[^\s\"'|;&<>]*|/[^\s\"'|;&<>]*)"
)

# 设备/管道伪路径，不视为外部文件读取
_PSEUDO_PATHS = frozenset({
    "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin",
    "/dev/zero", "/dev/random", "/dev/urandom",
})


@dataclass(frozen=True)
class PermissionVerdict:
    """一次动作的权限裁决。"""

    action: str  # allow | ai | ask | deny
    reason: str = ""
    rule: dict | None = None
    approved: bool = False
    force_ask: bool = False


class PermissionPolicy:
    """配置 + 规则 + 会话批准 → 统一裁决。"""

    def __init__(
        self,
        config: PermissionConfig | None,
        rules: PermissionRules | None,
        store=None,
        *,
        workspace_root: str = "",
        cwd: str = "",
    ) -> None:
        self.config = config if isinstance(config, PermissionConfig) else PermissionConfig()
        self.rules = rules
        self.store = store
        self.cwd = os.path.abspath(cwd or workspace_root or os.getcwd())
        self.workspace_root = os.path.realpath(workspace_root or self.cwd)
        self.temp_root = os_temp_root()

    # ── 基础 ──────────────────────────────────────────────────────

    def is_inside(self, resolved: str | None) -> bool:
        if not resolved:
            return False
        return is_path_within(resolved, self.workspace_root) or is_path_within(resolved, self.temp_root)

    def path_decision_id(self, op: str, resolved: str | None, raw: str = "",
                         *, static_prefix: str | None = None,
                         code_hash: str = "", target_index: int = 0) -> str:
        """生成路径审批键。

        - static: 绝对路径；
        - prefixed / opaque: 代码哈希 + 目标序号，避免不同动态目标碰撞；
        - 无代码哈希时退回 raw + 序号（非 Python 工具的兜底）。
        """
        if resolved:
            key = f"{op}:{os.path.normcase(os.path.abspath(resolved))}"
        elif static_prefix:
            prefix = os.path.normcase(os.path.abspath(static_prefix))
            if code_hash:
                key = f"dyn:{op}:{code_hash}:{target_index}:{prefix}"
            else:
                key = f"dyn:{op}:{(raw or '').strip()}:{target_index}:{prefix}"
        elif code_hash:
            key = f"dyn:{op}:{code_hash}:{target_index}"
        else:
            key = f"dyn:{op}:{(raw or '').strip()}:{target_index}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _mode_verdict(mode: str, reason: str = "") -> PermissionVerdict:
        if mode not in _RANK:
            mode = "ask"
        return PermissionVerdict(action=mode, reason=reason, force_ask=(mode == "ask"))

    def _mode_or_judge(self, mode: str, reason: str = "") -> PermissionVerdict:
        """分项策略的 ai：判定器关闭时退化为 ask。"""
        if mode == "ai" and not self.config.judge.enabled:
            return self._mode_verdict("ask", reason or "判定器未启用，转为人工询问")
        return self._mode_verdict(mode, reason)

    @staticmethod
    def _combine(current: PermissionVerdict | None, new: PermissionVerdict) -> PermissionVerdict:
        if current is None:
            return new
        if _RANK[new.action] > _RANK[current.action]:
            return new
        return current

    # ── 命令 ──────────────────────────────────────────────────────

    def command_verdict(
        self,
        tool: str,
        command: str,
        targets: list | None = None,
        findings: list | None = None,
        decision_id: str = "",
    ) -> PermissionVerdict:
        if not self.config.enabled:
            return PermissionVerdict("allow", "权限系统已关闭")

        tool = tool or "*"
        command = command or ""
        targets = list(targets or [])
        findings = list(findings or [])

        # 内置 deny（仅命令文本工具；python 代码不按 shell 语法切段）
        if tool in ("bash", "pwsh"):
            builtin = match_builtin_deny(tool, command)
            if builtin:
                return PermissionVerdict("deny", builtin.get("note", "命中内置保护规则"), builtin)

        verdict: PermissionVerdict | None = None
        if self.rules is not None:
            decision, rule = self.rules.evaluate_command(tool, command)
            if decision == "deny":
                return PermissionVerdict("deny", (rule or {}).get("note", "命中黑名单"), rule)
            if decision == "ask":
                return PermissionVerdict("ask", (rule or {}).get("note", "命中灰名单"), rule, force_ask=True)
            if decision == "allow":
                verdict = PermissionVerdict("allow", (rule or {}).get("display", "命中白名单"), rule, approved=True)

        store_hit = bool(
            decision_id and self.store is not None and self.store.is_approved(decision_id)
        )

        # 命令放行也要检查写/删目标：越界目标可能把结论提升为 ask/ai/deny
        for target in targets:
            verdict = self._combine(verdict, self._target_verdict(target, tool))
            if verdict.action == "deny":
                return verdict

        if verdict is not None and verdict.action in ("ask", "ai", "deny"):
            return verdict
        if store_hit:
            return PermissionVerdict("allow", "会话已批准", approved=True)
        if verdict is not None and verdict.action == "allow":
            return verdict

        # 内置只读白名单
        has_write_target = any(getattr(t, "op", "") in ("write", "delete") for t in targets)
        if (
            self.config.readonly_bypass
            and not findings
            and not has_write_target
            and is_readonly_command(tool, command)
            and self._readonly_external_ok(tool, command)
        ):
            return PermissionVerdict("allow", "内置只读白名单")

        # 安全区创建（touch/mkdir 等）：目标全部在安全区内
        if not findings and is_safe_create_command(tool, command) and targets:
            if all(getattr(t, "resolved", None) and self.is_inside(t.resolved) for t in targets):
                return PermissionVerdict("allow", "安全区内创建")

        # 默认策略
        mode = self.config.default_mode
        reason = "默认策略"
        if findings and mode == "allow":
            mode = "ai"
            reason = "静态证据需判定"
        return self._mode_or_judge(mode, reason)

    def _target_verdict(self, target, tool: str) -> PermissionVerdict:
        op = getattr(target, "op", "write")
        resolved = getattr(target, "resolved", None)
        raw = getattr(target, "raw", "")
        kind = "path:delete" if op == "delete" else ("path:read" if op == "read" else "path:write")
        decision_id = self.path_decision_id(op, resolved, raw)
        return self.path_verdict(kind, resolved, decision_id=decision_id, tool=tool, raw=raw)

    def _readonly_external_ok(self, tool: str, command: str) -> bool:
        """只读白名单的外部读取启发式。

        outside.read=allow 时不限制；否则命令中出现安全区外的绝对路径就跳过白名单，
        交给分项策略（ai/ask/deny）。完整读目标提取见 P3。
        """
        if self.config.outside.read == "allow":
            return True
        from core.security.shell_paths import normalize_shell_path

        mode = "pwsh" if tool == "pwsh" else "bash"
        for match in _ABS_PATH_RE.finditer(command or ""):
            token = match.group(0).strip("\"'")
            if not token or token in _PSEUDO_PATHS or token.startswith("//"):
                continue
            try:
                resolved = normalize_shell_path(token, self.cwd, mode)
            except Exception:  # noqa: BLE001 —— 解析失败按外部处理
                return False
            if not self.is_inside(resolved):
                return False
        return True

    # ── 路径 ──────────────────────────────────────────────────────

    def path_verdict(
        self,
        kind: str,
        resolved: str | None,
        *,
        decision_id: str = "",
        tool: str = "*",
        raw: str = "",
        static_prefix: str | None = None,
    ) -> PermissionVerdict:
        if not self.config.enabled:
            return PermissionVerdict("allow", "权限系统已关闭")

        if kind not in ("path:read", "path:write", "path:delete"):
            return PermissionVerdict("allow")

        op = "read" if kind == "path:read" else ("delete" if kind == "path:delete" else "write")
        mode = getattr(self.config.outside, op, "ask")

        # 1. 规则：static 用 resolved，prefixed 用 static_prefix。
        subject = resolved or static_prefix
        if subject and self.rules is not None:
            decision, rule = self.rules.evaluate_path(kind, subject, tool)
            if decision == "deny":
                return PermissionVerdict("deny", (rule or {}).get("note", "命中黑名单"), rule)
            if decision == "ask":
                return PermissionVerdict("ask", (rule or {}).get("note", "命中灰名单"), rule, force_ask=True)
            if decision == "allow":
                return PermissionVerdict("allow", (rule or {}).get("display", "命中白名单"), rule, approved=True)

        # 2. 会话批准：必须在“动态路径无法解析”早退之前，否则动态路径永远批不掉。
        if decision_id and self.store is not None and self.store.is_approved(decision_id):
            return PermissionVerdict("allow", "会话已批准", approved=True)

        # 3. 静态路径在安全区内直接放行。
        if resolved:
            if self.is_inside(resolved):
                return PermissionVerdict("allow", "安全区")
            return self._mode_or_judge(
                mode,
                f"工作区外{('读取' if op == 'read' else ('删除' if op == 'delete' else '写入'))}",
            )

        # 4. 动态路径：即使前缀在安全区也首次询问，防止运行时后缀逃逸。
        effective = mode if mode in ("ask", "deny", "ai") else "ask"
        if static_prefix:
            return self._mode_or_judge(effective, "动态路径，前缀已确认，等待批准")
        return self._mode_or_judge(effective, "动态路径，无法静态解析")

    # ── URL ───────────────────────────────────────────────────────

    def url_verdict(
        self,
        url: str,
        *,
        decision_id: str = "",
        tool: str = "browser",
    ) -> PermissionVerdict:
        if not self.config.enabled:
            return PermissionVerdict("allow", "权限系统已关闭")
        if self.rules is not None:
            decision, rule = self.rules.evaluate_url(url, tool)
            if decision == "deny":
                return PermissionVerdict("deny", (rule or {}).get("note", "命中黑名单"), rule)
            if decision == "ask":
                return PermissionVerdict("ask", (rule or {}).get("note", "命中灰名单"), rule, force_ask=True)
            if decision == "allow":
                return PermissionVerdict("allow", (rule or {}).get("display", "命中白名单"), rule, approved=True)
        if decision_id and self.store is not None and self.store.is_approved(decision_id):
            return PermissionVerdict("allow", "会话已批准", approved=True)
        return self._mode_or_judge(self.config.outside.navigate, "非公网导航")
