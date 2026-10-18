"""审批规则引擎：规则 JSON 的加载 / 匹配 / 落盘（确定性，无 LLM）。

设计文档：workspace/approval-flow-design.md §3.2 / §3.5。核心语义：

- 规则集里**只有 allow 规则**，没有 deny 规则；未命中一律进入 lite 判定
  （core/security/approval_judge.py）。
- 命令匹配在**段级**进行：命令按顶层 ``; && || |`` 切段（复用
  shell_paths._split_segments），每一段都命中 allow 规则才放行——防复合命令搭车
  （``git status && rm -rf /`` 不会被 ``git status`` 前缀规则放行）。
- 含命令替换（``$(...)``、反引号、``<(...)``）的命令禁用 prefix/argv_glob 匹配，
  只允许**整条 exact**——解析不了的东西不能被模式规则放行（整条 exact 意味着
  逐字节相同的命令此前被批准过）。
- 落盘统一走 :meth:`ApprovalRules.add_granted`：BANNED_PREFIXES（入口前缀黑名单）
  与 DESTRUCTIVE_HEADS（破坏性动词头降级 exact）双约束。AI 判定自动落盘与用户
  「一直允许」共用同一管道（granted_by 区分来源）。
- 旧 approvals.json（精确命令哈希）在首次加载时自动迁移为 exact 规则。

线程模型：工具线程并发读（match_*）、循环/答案注入线程写（add_granted / bump_hits），
全部经 ``_lock`` 串行化；写盘用 atomic_write_json。
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from core.fs_utils import atomic_write_json, load_json_or_backup
from core.security.shell_paths import _split_segments

logger = logging.getLogger(__name__)

RULES_VERSION = 1
_MATCH_TYPES = ("exact", "prefix", "argv_glob")
_KINDS = ("command", "path:write", "path:delete", "browser:navigate")

# ─── 黑名单与降级清单（设计 §3.5 第 3/4 步） ────────────────────────────

# 入口前缀黑名单：这些词开头的 prefix/argv_glob 规则等于「换任意命令都能跑」，
# 禁止落盘。exact 规则不受限（精确命令在批准/判定时可见，粒度不放大）。
# 先例：codex BANNED_PREFIX_SUGGESTIONS、Claude-Code BARE_SHELL_PREFIXES。
BANNED_PREFIX_HEADS = frozenset({
    "bash", "sh", "zsh", "sudo", "doas", "env", "xargs", "nohup", "stdbuf", "setsid",
    "python", "python3", "pythonw", "py",
    "pwsh", "pwsh.exe", "powershell", "powershell.exe",
    "cmd", "cmd.exe", "wsl", "wsl.exe",
    "eval", "exec", "iex", "invoke-expression", "invoke-command",
    "osascript", "at", "batch", "screen", "tmux",
    "perl", "ruby", "node", "deno", "bun", "php", "lua",
})

# 破坏性动词头：这些词开头的 prefix/argv_glob 规则一律降级为 exact——
# `rm -rf ./build` 放行时只落 exact，绝不落 `rm -rf` / `rm` 级别的规则
# （否则后续 `rm -rf C:\Users` 会无判定无询问直接执行）。mkfs.* 按前缀单独判断。
DESTRUCTIVE_HEADS = frozenset({
    "rm", "del", "erase", "rd", "rmdir", "unlink", "remove-item", "ri",
    "dd", "format", "format-volume", "clear-disk", "initialize-disk", "diskpart",
    "shutdown", "reboot", "stop-computer", "restart-computer", "stop-process",
})

# 内置只读命令头（builtin 快速通道，免判定）：避免 ls/cat 级命令也吃一次 LLM 往返。
# 只匹配首个 token（链式命令须每段只读）；重定向/越界写删仍由路径权限门拦截。
_READONLY_SIMPLE_HEADS = frozenset({
    "ls", "pwd", "cat", "head", "tail", "wc", "file", "stat", "du", "df", "free",
    "ps", "uname", "whoami", "hostname", "date", "which", "whereis", "where",
    "type", "printenv", "id", "tree", "rg", "grep", "diff", "comm", "sort",
    "uniq", "cut", "seq", "echo", "printf", "true", "false", "realpath",
    "dirname", "basename", "cygpath", "readlink", "groups", "tty",
    # 创建类：风险只在路径作用域，越界由路径权限门独立拦截
    "touch", "mkdir",
})

_READONLY_GIT_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "branch", "remote", "rev-parse", "tag",
    "describe", "shortlog", "ls-files", "ls-remote", "blame", "config",
})

_READONLY_PKG_SUBCOMMANDS = {
    "npm": frozenset({"ls", "outdated", "view", "search", "config"}),
    "pip": frozenset({"list", "show", "freeze", "check"}),
    "git": _READONLY_GIT_SUBCOMMANDS,
}

_READONLY_PWSH_HEADS = frozenset({
    "get-childitem", "get-content", "get-item", "get-itemproperty", "get-process",
    "get-date", "get-location", "get-command", "get-help", "get-member",
    "get-psdrive", "get-service", "get-volume", "test-path", "measure-object",
    "select-string", "compare-object", "write-output", "write-host",
    "new-item", "mkdir", "md",
})

# 目录导航段：不参与只读判定（cd 的写风险由 extract_shell_targets 的 cwd 跟踪 +
# 路径权限门独立把关；读取类操作本就不受限）
_READONLY_NAV_HEADS = frozenset({
    "cd", "pushd", "popd", "set-location", "push-location", "pop-location", "sl",
})

# bash 侧 $(( 算术展开不执行命令，不算命令替换
_SUBSTITUTION_RE = re.compile(r"\$\((?!\()|`|<\(|>\(")

# hits 落盘节流（秒）：命中计数高频更新，避免每次匹配都写盘
_HIT_FLUSH_INTERVAL = 5.0


def canonical_text(text: str) -> str:
    """规范化匹配文本：折叠连续空白（与 approval.canonicalize_command 同语义）。"""
    return " ".join((text or "").split())


def _new_rule_id() -> str:
    return f"r-{secrets.token_hex(4)}"


def _head_token(text: str) -> str:
    """取命令串首个 token，去路径与 .exe 后缀、转小写（黑名单/只读判定用）。"""
    token = (text or "").strip().split()[:1]
    if not token:
        return ""
    head = token[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    return head[:-4] if head.endswith(".exe") else head


def _has_substitution(command: str) -> bool:
    """命令是否含可执行替换（$()、反引号、进程替换）。$(( 算术展开不算。"""
    return bool(_SUBSTITUTION_RE.search(command or ""))


def _norm_head(value: str) -> str:
    """规则匹配串的头部词（prefix 的第一个 token / argv_glob 的第一个元素）。"""
    if isinstance(value, list):
        return _head_token(" ".join(str(v) for v in value[:1]))
    return _head_token(str(value))


def _path_prefix_covers(value: str, resolved: str) -> bool:
    """路径前缀匹配：normcase 后整等或目录边界前缀（防 /a/b 误配 /a/bc）。"""
    nval = os.path.normcase(os.path.abspath(value))
    nres = os.path.normcase(os.path.abspath(resolved))
    if nres == nval:
        return True
    if not nval.endswith(os.sep):
        nval += os.sep
    return nres.startswith(nval)


def is_builtin_readonly(tool: str, command: str) -> bool:
    """内置只读快速通道（source=builtin 规则的代码形态）：链式命令每段只读才免判定。

    只做放行豁免，不做拒绝；重定向与越界写删由路径权限门独立拦截。
    含命令替换的命令不可信（只读段可能夹带执行段），不走快道。
    """
    command = command or ""
    if _has_substitution(command):
        return False
    mode = "pwsh" if tool == "pwsh" else "bash"
    segments = [s for s in _split_segments(command, mode) if s.strip()]
    if not segments:
        return False
    for seg in segments:
        tokens = seg.split()
        if not tokens:
            return False
        head = _head_token(tokens[0])
        if head in _READONLY_NAV_HEADS:
            continue
        sub = _head_token(tokens[1]) if len(tokens) > 1 else ""
        if head in _READONLY_SIMPLE_HEADS or (mode == "pwsh" and head in _READONLY_PWSH_HEADS):
            continue
        if head in _READONLY_PKG_SUBCOMMANDS and sub in _READONLY_PKG_SUBCOMMANDS[head]:
            continue
        return False
    return True


def build_granted_rule(
    *,
    kind: str,
    tool: str,
    action: str,
    suggested: dict | None = None,
    granted_by: str,
    summary: str = "",
    decision_id: str = "",
) -> dict | None:
    """按 §3.5 管道把一次放行固化为规则；不可落盘时返回 None。

    - suggested 优先（判定器建议 / 卡片粒度），否则兜底推导；
    - 兜底推导：命令类两词前缀（argv[1] 为旗标时降级 exact）；路径类取父目录
      prefix；导航类取 host（含端口）prefix；
    - BANNED_PREFIXES：prefix/argv_glob 头部命中 → 拒绝生成（返回 None）；
    - DESTRUCTIVE_HEADS：prefix/argv_glob 头部命中 → 降级为 exact。
    """
    if kind not in _KINDS:
        return None
    tool = tool or "*"
    action = action or ""
    suggested = suggested if isinstance(suggested, dict) else None
    match: dict = {}

    if kind == "command":
        stype = (suggested or {}).get("type")
        svalue = (suggested or {}).get("value")
        if stype in _MATCH_TYPES and isinstance(svalue, str) and svalue.strip():
            match = {"type": stype, "value": canonical_text(svalue)}
            if stype == "argv_glob" and isinstance(svalue, list):
                match = {"type": "argv_glob", "value": [str(v) for v in svalue]}
        else:
            tokens = action.split()
            if len(tokens) >= 2 and not tokens[1].startswith("-"):
                match = {"type": "prefix", "value": canonical_text(" ".join(tokens[:2]))}
            else:
                match = {"type": "exact", "value": canonical_text(action)}
        if match["type"] in ("prefix", "argv_glob"):
            head = _norm_head(match["value"])
            if head in BANNED_PREFIX_HEADS:
                logger.info(f"[approval-rules] 拒绝落盘入口前缀规则：{head!r}（{action[:60]}）")
                return None
            if head in DESTRUCTIVE_HEADS or head.startswith("mkfs"):
                match = {"type": "exact", "value": canonical_text(action)}
    elif kind in ("path:write", "path:delete"):
        stype = (suggested or {}).get("type")
        svalue = (suggested or {}).get("value")
        if stype in ("prefix", "exact") and isinstance(svalue, str) and svalue.strip():
            match = {"type": stype, "value": svalue}
        else:
            target = action.strip()
            parent = os.path.dirname(target) if target else ""
            if not parent:
                return None
            match = {"type": "prefix", "value": parent}
    elif kind == "browser:navigate":
        stype = (suggested or {}).get("type")
        svalue = (suggested or {}).get("value")
        if stype in ("prefix", "exact") and isinstance(svalue, str) and svalue.strip():
            match = {"type": stype, "value": svalue.strip()}
        else:
            try:
                parts = urlsplit(action)
                host = parts.netloc or parts.path.split("/")[0]
            except ValueError:
                host = ""
            if not host:
                return None
            match = {"type": "prefix", "value": host}

    value_for_display = match["value"]
    if isinstance(value_for_display, list):
        value_for_display = " ".join(value_for_display)
    type_label = {"exact": "精确匹配", "prefix": "以 {v} 开头", "argv_glob": "匹配模式 {v}"}
    display = (suggested or {}).get("display") or type_label[match["type"]].format(v=value_for_display)

    return {
        "id": _new_rule_id(),
        "enabled": True,
        "source": "granted",
        "granted_by": granted_by if granted_by in ("judge", "user") else "user",
        "tool": tool,
        "kind": kind,
        "match": match,
        "note": (summary or "")[:200],
        "display": str(display)[:200],
        "granted_from": {"decision_id": decision_id, "summary": (summary or "")[:200]},
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hits": 0,
        "last_used_at": "",
    }


def implied_path_rules(targets: list[dict], granted_by: str, decision_id: str) -> list[dict]:
    """命令放行时对其越界写/删目标派生的路径规则（使后续重执行通过路径门）。"""
    rules: list[dict] = []
    for t in targets or []:
        if not isinstance(t, dict):
            continue
        resolved = t.get("resolved")
        if not resolved or t.get("inside_workspace"):
            continue
        kind = "path:delete" if t.get("op") == "delete" else "path:write"
        rule = build_granted_rule(
            kind=kind, tool="*", action=os.path.dirname(resolved) or resolved,
            suggested={"type": "prefix", "value": os.path.dirname(resolved) or resolved},
            granted_by=granted_by, summary=f"命令放行隐含（{decision_id}）", decision_id=decision_id,
        )
        if rule:
            rules.append(rule)
    return rules


# 默认 constraints（规则文件未提供/缺条目时补齐；文件条目按 id 覆盖默认）
DEFAULT_CONSTRAINTS: list[dict] = [
    {"id": "python-tool-routing", "severity": "deny",
     "text": "Python 代码必须通过 python 工具执行。在 bash/pwsh 里直接调用 python 解释器会绕过 python 工具的安全校验。"},
    {"id": "cross-tool-isolation", "severity": "deny",
     "text": "bash 不得调用 pwsh/powershell，pwsh 不得调用 bash/sh；两者都不得调用 cmd/wsl。这是工具路由架构约束，用户批准也不能改变。"},
    {"id": "py-launcher", "severity": "deny",
     "text": "py 启动器与 python 解释器同样绕过 python 工具的安全校验。"},
    {"id": "code-from-string", "severity": "deny",
     "text": "eval / Invoke-Expression / iex 属于字符串代码执行，载荷藏在字符串里无法静态审计，应拒绝并建议直接执行等价命令。"},
    {"id": "python-dynamic-exec", "severity": "deny",
     "text": "python 代码中的 eval/exec/compile/__import__、os.system/popen、subprocess.*、importlib.import_module、from x import * 属于动态执行或 shell 逃逸，应拒绝。"},
    {"id": "workspace-boundary", "severity": "warn",
     "text": "文件写入/删除应限制在工作区与系统临时目录内；越界写删需要用户确认。"},
    {"id": "system-destructive", "severity": "warn",
     "text": "磁盘格式化/清盘、关机重启、系统进程终止等破坏性系统操作需要用户明确确认。"},
    {"id": "ssrf-navigation", "severity": "deny",
     "text": "file://、data: 等 scheme 与云元数据端点（169.254.169.254 等）任何时候都不允许导航，用户批准也不能解锁。"},
    {"id": "ssrf-navigation-approve", "severity": "warn",
     "text": "非公网地址（localhost、私网 IP、环回）导航是合法的本地开发场景，可放行但需用户确认。"},
]


class ApprovalRules:
    """规则 JSON 的加载 / 匹配 / 落盘（workspace 层；可选 mtime 热重载）。

    文件格式见设计 §3.2.2：{"version", "migrated_from"?, "rules", "constraints"}。
    旧 approvals.json 在新文件缺失时自动迁移（exact 规则，条数校验 + .bak 备份）。
    """

    def __init__(self, rules_path: str | Path | None = None, *, workspace_root: str = "",
                 hot_reload: bool = True) -> None:
        self._path = Path(rules_path) if rules_path else None
        self._hot = hot_reload
        self._lock = threading.RLock()
        self._rules: list[dict] = []
        self._file_constraints: list[dict] = []
        self._migrated_from: str = ""
        self._sig: tuple | None = None
        self._last_hit_flush = 0.0
        self.load()

    # ── 加载 / 迁移 / 校验 ────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if not self._path or not self._hot:
            return
        try:
            st = self._path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if sig != self._sig:
            self.load()

    def load(self) -> None:
        with self._lock:
            self._rules = []
            self._file_constraints = []
            self._sig = None
            if self._path is None:
                return
            self._maybe_migrate_legacy()
            data = load_json_or_backup(self._path, {})
            if not isinstance(data, dict):
                data = {}
            for raw in data.get("rules") or []:
                rule = self._validate_rule(raw)
                if rule is None:
                    logger.warning(f"[approval-rules] 跳过非法/越权规则条目：{raw!r}")
                    continue
                self._rules.append(rule)
            cs = data.get("constraints")
            if isinstance(cs, list):
                self._file_constraints = [c for c in cs if isinstance(c, dict) and c.get("id") and c.get("text")]
            self._migrated_from = str(data.get("migrated_from") or "")
            try:
                st = self._path.stat()
                self._sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                self._sig = None
            logger.info(f"[approval-rules] 已加载 {len(self._rules)} 条规则（{self._path}）")

    def _maybe_migrate_legacy(self) -> None:
        """旧 approvals.json → approval_rules.json（设计 §七）：exact 映射、.bak、条数校验。"""
        if self._path is None or self._path.exists():
            return
        legacy = self._path.parent / "approvals.json"
        if not legacy.exists():
            return
        data = load_json_or_backup(legacy, {})
        old_rules = data.get("rules") or [] if isinstance(data, dict) else []
        mapped: list[dict] = []
        for old in old_rules:
            if not isinstance(old, dict):
                continue
            command = str(old.get("command") or "")
            kind = old.get("kind", "command")
            # §7.2：迁移一律 exact（不静默放宽），路径/导航同理（command 存的是
            # resolved 路径 / URL 本身）
            exact = {"type": "exact", "value": canonical_text(command)}
            rule = build_granted_rule(
                kind=kind if kind in _KINDS else "command",
                tool="browser" if kind == "browser:navigate" else "*",
                action=command, suggested=exact, granted_by="user",
                summary=str(old.get("reason") or "")[:120],
                decision_id=str(old.get("decision_id") or ""))
            if rule is None:
                rule = {  # 兜底：任何情况都保底为 exact，绝不静默放宽
                    "id": _new_rule_id(), "enabled": True, "source": "granted", "granted_by": "user",
                    "tool": "*", "kind": kind if kind in _KINDS else "command",
                    "match": {"type": "exact", "value": canonical_text(command)},
                    "note": str(old.get("reason") or "")[:120], "display": f"精确匹配：{command[:80]}",
                    "granted_from": {"decision_id": old.get("decision_id"), "summary": ""},
                    "created_at": old.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "hits": 0, "last_used_at": "",
                }
            rule["legacy_decision_id"] = old.get("decision_id")
            if old.get("created_at"):
                rule["created_at"] = old["created_at"]
            mapped.append(rule)
        if len(mapped) != len(old_rules):
            logger.warning(f"[approval-rules] 迁移中止：旧 {len(old_rules)} 条 → 新 {len(mapped)} 条不一致")
            return
        try:
            backup = legacy.with_suffix(".json.bak")
            backup.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
            atomic_write_json(self._path, {
                "version": RULES_VERSION,
                "migrated_from": "approvals.json",
                "rules": mapped,
                "constraints": [],
            })
            logger.info(f"[approval-rules] 已迁移 {len(mapped)} 条旧审批规则 → {self._path}（备份 {backup}）")
        except OSError as e:
            logger.warning(f"[approval-rules] 迁移写盘失败（旧文件保留）：{e}")

    @staticmethod
    def _validate_rule(raw: dict) -> dict | None:
        """加载时校验（schema + 防篡改）：非法条目返回 None 由调用方跳过。"""
        if not isinstance(raw, dict):
            return None
        kind = raw.get("kind", "command")
        if kind not in _KINDS:
            return None
        match = raw.get("match")
        if not isinstance(match, dict) or match.get("type") not in _MATCH_TYPES:
            return None
        value = match.get("value")
        if match["type"] == "argv_glob":
            if not (isinstance(value, list) and value and all(isinstance(v, str) for v in value)):
                return None
        elif not (isinstance(value, str) and value.strip()):
            return None
        if raw.get("enabled") is False:
            pass  # 保留停用规则（设置页可见），匹配时跳过
        if match["type"] in ("prefix", "argv_glob") and kind == "command":
            if _norm_head(value) in BANNED_PREFIX_HEADS:
                return None  # 防篡改：入口前缀规则即使写进文件也不生效
        rule = dict(raw)
        rule.setdefault("id", _new_rule_id())
        rule.setdefault("enabled", True)
        rule.setdefault("source", "user")
        rule.setdefault("tool", "*")
        rule.setdefault("display", f"{match['type']}: {value_for_display(match['value'])}")
        return rule

    # ── 匹配 ─────────────────────────────────────────────────────

    def _candidates(self, kind: str, tool: str) -> list[dict]:
        return [r for r in self._rules
                if r.get("enabled", True) and r.get("kind") == kind and r.get("tool", "*") in ("*", tool)]

    def _bump(self, rule: dict) -> None:
        rule["hits"] = int(rule.get("hits") or 0) + 1
        rule["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now = time.monotonic()
        if now - self._last_hit_flush >= _HIT_FLUSH_INTERVAL:
            self._last_hit_flush = now
            self.save()

    def match_command(self, tool: str, command: str) -> dict | None:
        """命令规则匹配（§3.2.3）：整条 exact 或段级 prefix/argv_glob（每段命中才放行）。

        含命令替换的命令禁用段级模式匹配（只允许整条 exact）。
        命中即返回规则（并更新 hits）；未命中返回 None（进入判定）。
        """
        command = command or ""
        canon = canonical_text(command)
        if not canon:
            return None
        self._ensure_loaded()
        with self._lock:
            candidates = self._candidates("command", tool)
            for rule in candidates:
                m = rule["match"]
                if m["type"] == "exact" and canonical_text(m["value"]) == canon:
                    self._bump(rule)
                    return rule
            if _has_substitution(command):
                return None
            mode = "pwsh" if tool == "pwsh" else "bash"
            segments = [canonical_text(s) for s in _split_segments(command, mode) if canonical_text(s)]
            if not segments:
                return None
            covered: list[dict] = []
            for seg in segments:
                seg_tokens = seg.split()
                hit = None
                for rule in candidates:
                    m = rule["match"]
                    if m["type"] == "exact":
                        if canonical_text(m["value"]) == seg:
                            hit = rule
                            break
                    elif m["type"] == "prefix":
                        value = canonical_text(m["value"])
                        if seg == value or seg.startswith(value + " "):
                            hit = rule
                            break
                    else:  # argv_glob
                        pattern = m["value"]
                        if self._glob_match(pattern, seg_tokens):
                            hit = rule
                            break
                if hit is None:
                    return None
                covered.append(hit)
            for rule in covered:
                self._bump(rule)
            return covered[0]

    @staticmethod
    def _glob_match(pattern: list[str], tokens: list[str]) -> bool:
        """argv_glob 逐 token glob；末位 `*` 表示其后任意 token。"""
        if not pattern:
            return False
        tail_any = pattern[-1] == "*" and len(pattern) > 1
        core = pattern[:-1] if tail_any else pattern
        if tail_any and len(tokens) < len(core):
            return False
        if not tail_any and len(tokens) != len(core):
            return False
        for ptok, tok in zip(core, tokens):
            if not fnmatch.fnmatch(tok.lower(), ptok.lower()):
                return False
        return True

    def match_path(self, kind: str, resolved: str, tool: str = "*") -> dict | None:
        """路径规则匹配（path:write / path:delete）：normcase 目录边界前缀或整等。"""
        if kind not in ("path:write", "path:delete") or not resolved:
            return None
        self._ensure_loaded()
        with self._lock:
            for rule in self._candidates(kind, tool):
                m = rule["match"]
                if m["type"] == "prefix" and _path_prefix_covers(m["value"], resolved):
                    self._bump(rule)
                    return rule
                if m["type"] == "exact" and os.path.normcase(os.path.abspath(m["value"])) == os.path.normcase(os.path.abspath(resolved)):
                    self._bump(rule)
                    return rule
        return None

    def match_url(self, url: str, tool: str = "browser") -> dict | None:
        """导航规则匹配：exact 整等或 prefix 前缀（host 级）。"""
        url = (url or "").strip()
        if not url:
            return None
        self._ensure_loaded()
        with self._lock:
            for rule in self._candidates("browser:navigate", tool):
                m = rule["match"]
                value = str(m["value"]).strip()
                if m["type"] == "exact" and value == url:
                    self._bump(rule)
                    return rule
                if m["type"] == "prefix" and url.startswith(value):
                    self._bump(rule)
                    return rule
        return None

    # ── 落盘 / 约束 ───────────────────────────────────────────────

    def add_granted(self, rule: dict) -> dict | None:
        """落盘一条 granted 规则（已过 build_granted_rule 约束）。同 match 去重：保留首次来源。"""
        if not rule or rule.get("kind") not in _KINDS:
            return None
        with self._lock:
            self._ensure_loaded()
            key = (rule.get("tool", "*"), rule["kind"], rule["match"]["type"],
                   str(rule["match"]["value"]).lower())
            for existing in self._rules:
                ekey = (existing.get("tool", "*"), existing["kind"], existing["match"]["type"],
                        str(existing["match"]["value"]).lower())
                if ekey == key:
                    self._bump(existing)
                    return existing
            self._rules.append(rule)
            self.save()
            return rule

    def save(self) -> None:
        with self._lock:
            if self._path is None:
                return
            data: dict = {
                "version": RULES_VERSION,
                "rules": self._rules,
                "constraints": self._file_constraints,
            }
            if self._migrated_from:
                data["migrated_from"] = self._migrated_from
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(self._path, data)
            except OSError as e:
                logger.warning(f"[approval-rules] 写盘失败：{e}")

    def constraints(self) -> list[dict]:
        """判定输入约束：默认集 + 文件覆盖（按 id）。"""
        self._ensure_loaded()
        merged: dict[str, dict] = {c["id"]: dict(c) for c in DEFAULT_CONSTRAINTS}
        with self._lock:
            for c in self._file_constraints:
                merged[c["id"]] = {"severity": c.get("severity", "warn"), **c}
        return list(merged.values())

    def granted_displays(self, limit: int = 20) -> list[str]:
        """落盘规则的 display 列表（下放给子代理的提示段落用）。"""
        self._ensure_loaded()
        with self._lock:
            return [r.get("display", "") for r in self._rules if r.get("enabled", True)][:limit]

    def all_rules(self) -> list[dict]:
        with self._lock:
            self._ensure_loaded()
            return [dict(r) for r in self._rules]


def value_for_display(value: str | list[str]) -> str:
    if isinstance(value, list):
        return " ".join(value)
    return str(value)
