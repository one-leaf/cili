"""统一权限规则引擎（allow / deny / ask）。

与旧 ``approval_rules.py`` 的 allow-only 模型不同，本模块的每条规则带
``decision``：``allow``（白名单）/ ``deny``（黑名单）/ ``ask``（灰名单）。
同一动作命中多条规则时按 **deny > ask > allow** 取最严。

规则分两层：全局层 ``data/cili/approval_rules.json`` 与工作区层
``<workspace>/.cili/approval_rules.json``；匹配时工作区层优先。

设计文档：workspace/default/permission-settings-plan-2026-10-26.md。
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

logger = logging.getLogger(__name__)

RULES_VERSION = 1
_MATCH_TYPES = ("exact", "prefix", "argv_glob")
_DECISIONS = ("allow", "deny", "ask")
_KINDS = ("command", "path:read", "path:write", "path:delete", "browser:navigate")

# ─── 内置保护（不可编辑） ────────────────────────────────────────────────

# 入口前缀黑名单：这些词开头的 allow/ask 规则等于「换任意命令都能跑」，禁止落盘。
# deny 规则不受限制（deny 只会缩小权限）。
BANNED_PREFIX_HEADS = frozenset({
    "bash", "sh", "zsh", "sudo", "doas", "env", "xargs", "nohup", "stdbuf", "setsid",
    "python", "python3", "pythonw", "py",
    "pwsh", "pwsh.exe", "powershell", "powershell.exe",
    "cmd", "cmd.exe", "wsl", "wsl.exe",
    "eval", "exec", "iex", "invoke-expression", "invoke-command",
    "osascript", "at", "batch", "screen", "tmux",
    "perl", "ruby", "node", "deno", "bun", "php", "lua",
})

# 破坏性动词头：allow/ask 的 prefix/argv_glob 规则降级为 exact。
DESTRUCTIVE_HEADS = frozenset({
    "rm", "del", "erase", "rd", "rmdir", "unlink", "remove-item", "ri",
    "dd", "format", "format-volume", "clear-disk", "initialize-disk", "diskpart",
    "shutdown", "reboot", "stop-computer", "restart-computer", "stop-process",
})

# 内置 deny 规则（代码维护，设置页只读展示）。
_BUILTIN_DENY_SPECS: tuple[tuple[str, str, str], ...] = (
    ("exact", "rm -rf /", "禁止递归删除系统盘根目录"),
    ("exact", "rm -rf /*", "禁止递归删除系统盘根目录"),
    ("exact", "rm -rf ~", "禁止递归删除用户主目录"),
    ("exact", "rm -rf *", "禁止递归删除当前目录全部内容"),
    ("prefix", "mkfs", "禁止格式化文件系统（mkfs）"),
    ("prefix", "format", "禁止格式化磁盘（format）"),
    ("prefix", "diskpart", "禁止磁盘分区操作（diskpart）"),
    ("prefix", "shutdown", "禁止关机命令"),
    ("prefix", "reboot", "禁止重启命令"),
    ("prefix", "Clear-Disk", "禁止清盘（Clear-Disk）"),
    ("prefix", "Initialize-Disk", "禁止初始化磁盘（Initialize-Disk）"),
    ("prefix", "Stop-Computer", "禁止关机（Stop-Computer）"),
    ("prefix", "Restart-Computer", "禁止重启（Restart-Computer）"),
    ("argv_glob", ["dd", "*", "of=/dev/*"], "禁止向块设备写入（dd of=/dev/…）"),
)

# ─── 只读白名单（内置，代码维护） ────────────────────────────────────────

_READONLY_SIMPLE_HEADS = frozenset({
    "ls", "pwd", "cat", "head", "tail", "wc", "file", "stat", "du", "df", "free",
    "ps", "uname", "whoami", "hostname", "date", "which", "whereis", "where",
    "type", "printenv", "id", "tree", "rg", "grep", "diff", "comm", "sort",
    "uniq", "cut", "seq", "echo", "printf", "true", "false", "realpath",
    "dirname", "basename", "cygpath", "readlink", "groups", "tty",
})

_READONLY_GIT_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "branch", "remote", "rev-parse", "tag",
    "describe", "shortlog", "ls-files", "ls-remote", "blame", "config",
})

_READONLY_PKG_SUBCOMMANDS = {
    "npm": frozenset({"ls", "outdated", "view", "search", "config"}),
    "pip": frozenset({"list", "show", "freeze", "check"}),
}

_READONLY_PWSH_HEADS = frozenset({
    "get-childitem", "get-content", "get-item", "get-itemproperty", "get-process",
    "get-date", "get-location", "get-command", "get-help", "get-member",
    "get-psdrive", "get-service", "get-volume", "test-path", "measure-object",
    "select-string", "compare-object", "write-output", "write-host",
})

_READONLY_NAV_HEADS = frozenset({
    "cd", "pushd", "popd", "set-location", "push-location", "pop-location", "sl",
})

# 安全区创建：不是纯只读，但目标在工作区/临时目录内时可免判定。
_SAFE_CREATE_HEADS = frozenset({
    "touch", "mkdir", "new-item", "md",
})

_SUBSTITUTION_RE = re.compile(r"\$\((?!\()|`|<\(|>\(")

# 只读白名单分组（设置页展示）
READONLY_GROUPS: dict[str, tuple[str, ...]] = {
    "bash": tuple(sorted(_READONLY_SIMPLE_HEADS | _READONLY_NAV_HEADS)),
    "git": tuple(sorted(_READONLY_GIT_SUBCOMMANDS)),
    "npm": tuple(sorted(_READONLY_PKG_SUBCOMMANDS["npm"])),
    "pip": tuple(sorted(_READONLY_PKG_SUBCOMMANDS["pip"])),
    "pwsh": tuple(sorted(_READONLY_PWSH_HEADS)),
    "safe_create": tuple(sorted(_SAFE_CREATE_HEADS)),
}

# ─── 通用工具 ──────────────────────────────────────────────────────────


def canonical_text(text: str) -> str:
    """规范化匹配文本：折叠连续空白。"""
    return " ".join((text or "").split())


def new_rule_id() -> str:
    return f"r-{secrets.token_hex(4)}"


def _head_token(text: str) -> str:
    """取命令串首个 token，去路径与 .exe 后缀、转小写。"""
    token = (text or "").strip().split()[:1]
    if not token:
        return ""
    head = token[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    return head[:-4] if head.endswith(".exe") else head


def _has_substitution(command: str) -> bool:
    return bool(_SUBSTITUTION_RE.search(command or ""))


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


def _path_prefix_covers(value: str, resolved: str) -> bool:
    """路径前缀匹配：normcase 后整等或目录边界前缀（防 /a/b 误配 /a/bc）。"""
    nval = os.path.normcase(os.path.abspath(value))
    nres = os.path.normcase(os.path.abspath(resolved))
    if nres == nval:
        return True
    if not nval.endswith(os.sep):
        nval += os.sep
    return nres.startswith(nval)


def _norm_head(value: str | list[str]) -> str:
    if isinstance(value, list):
        return _head_token(" ".join(str(v) for v in value[:1]))
    return _head_token(str(value))


def _split_command(command: str, mode: str) -> list[str]:
    """延迟导入 shell_paths，避免 permission_rules ↔ path_policy 循环依赖。"""
    from core.security.shell_paths import _split_segments
    return _split_segments(command, mode)


# ─── 只读 / 安全创建判定 ────────────────────────────────────────────────


def _split_git_args(tokens: list[str]) -> list[str]:
    """git 子命令后的非 flag token（用于区分 config/branch 读写形态）。"""
    return [t for t in tokens[1:] if not t.startswith("-")]


def _readonly_git_segment(tokens: list[str]) -> bool:
    sub = _head_token(tokens[1]) if len(tokens) > 1 else ""
    if sub not in _READONLY_GIT_SUBCOMMANDS:
        return False
    if sub == "config":
        # 只允许读取：无参数 / --get / --list / --get-all；`config key value` 是写。
        flags = {t.lower() for t in tokens[2:] if t.startswith("-")}
        operands = [t for t in tokens[2:] if not t.startswith("-")]
        if flags & {"--unset", "--unset-all", "--add", "--replace-all", "--rename-section", "--remove-section"}:
            return False
        if any(f in flags for f in ("--get", "--list", "--get-all", "-l")):
            return True
        return len(operands) <= 1
    if sub == "branch":
        return not ({t.lower() for t in tokens[2:]} & {
            "-d", "-D", "-m", "-M", "--delete", "--move", "--copy", "-c", "-C", "-f", "--force",
        })
    if sub == "remote":
        args = [t.lower() for t in tokens[2:]]
        if not args:
            return True
        return all(a in ("-v", "--verbose", "show", "get-url") for a in args)
    if sub == "tag":
        return not ({t.lower() for t in tokens[2:]} & {"-d", "--delete", "-a", "-f", "--force", "-s", "--sign"})
    return True


def _readonly_segment(tool: str, seg: str) -> bool:
    tokens = seg.split()
    if not tokens:
        return False
    head = _head_token(tokens[0])
    if head in _READONLY_NAV_HEADS:
        return True
    if tool == "pwsh" and head in _READONLY_PWSH_HEADS:
        return True
    if head == "git":
        return _readonly_git_segment(tokens)
    if head in _READONLY_PKG_SUBCOMMANDS:
        sub = _head_token(tokens[1]) if len(tokens) > 1 else ""
        return sub in _READONLY_PKG_SUBCOMMANDS[head]
    if head in _READONLY_SIMPLE_HEADS:
        return True
    return False


def is_readonly_command(tool: str, command: str) -> bool:
    """纯只读命令判定（不含结果/目标检查；由调用方补 findings/targets 条件）。

    - 含命令替换 → False；
    - 链式命令每一段都必须是只读；
    - touch/mkdir 不算纯只读（由 :func:`is_safe_create_command` 单独判断）。
    """
    command = command or ""
    if not command.strip() or _has_substitution(command):
        return False
    mode = "pwsh" if tool == "pwsh" else "bash"
    segments = [s for s in _split_command(command, mode) if s.strip()]
    if not segments:
        return False
    return all(_readonly_segment(tool, seg) for seg in segments)


def is_safe_create_command(tool: str, command: str) -> bool:
    """安全区创建命令（touch/mkdir 等）：每段都是只读或安全创建。"""
    command = command or ""
    if not command.strip() or _has_substitution(command):
        return False
    mode = "pwsh" if tool == "pwsh" else "bash"
    segments = [s for s in _split_command(command, mode) if s.strip()]
    if not segments:
        return False
    for seg in segments:
        tokens = seg.split()
        head = _head_token(tokens[0])
        if _readonly_segment(tool, seg) or head in _SAFE_CREATE_HEADS:
            continue
        return False
    return True


# ─── 内置规则 ──────────────────────────────────────────────────────────


def builtin_deny_rules() -> list[dict]:
    """内置 deny 规则（只读展示 + 运行时优先检查）。"""
    rules: list[dict] = []
    for idx, (match_type, value, note) in enumerate(_BUILTIN_DENY_SPECS):
        rules.append({
            "id": f"builtin-deny-{idx}",
            "enabled": True,
            "decision": "deny",
            "source": "builtin",
            "tool": "*",
            "kind": "command",
            "match": {"type": match_type, "value": value},
            "note": note,
            "display": note,
            "builtin": True,
        })
    return rules


def match_builtin_deny(tool: str, command: str) -> dict | None:
    """内置 deny 规则匹配（供 PermissionPolicy 调用）。"""
    return _match_builtin(builtin_deny_rules(), tool, command)


def _match_builtin(rules: list[dict], tool: str, command: str) -> dict | None:
    """内置 deny 匹配：命令段级（exact 整条或整段）。"""
    mode = "pwsh" if tool == "pwsh" else "bash"
    canon = canonical_text(command)
    if not canon:
        return None
    segments = [canonical_text(s) for s in _split_command(command, mode) if canonical_text(s)]
    for rule in rules:
        match = rule["match"]
        if match["type"] == "exact":
            if canonical_text(match["value"]) == canon or canonical_text(match["value"]) in segments:
                return rule
        elif match["type"] == "prefix":
            value = canonical_text(match["value"])
            if any(seg == value or seg.startswith(value + " ") for seg in segments):
                return rule
        else:
            for seg in segments:
                if _glob_match(match["value"], seg.split()):
                    return rule
    return None


# ─── 规则生成 ──────────────────────────────────────────────────────────


def build_granted_rule(
    *,
    kind: str,
    tool: str,
    action: str,
    decision: str = "allow",
    suggested: dict | None = None,
    granted_by: str = "user",
    summary: str = "",
    decision_id: str = "",
) -> dict | None:
    """把一次放行/拒绝固化为规则；不可落盘时返回 None。

    allow/ask 规则受 BANNED_PREFIX_HEADS 与 DESTRUCTIVE_HEADS 约束；
    deny 规则不受限（只会缩小权限）。
    """
    if kind not in _KINDS or decision not in _DECISIONS:
        return None
    tool = tool or "*"
    action = action or ""
    suggested = suggested if isinstance(suggested, dict) else None
    match: dict = {}

    if kind == "command":
        stype = (suggested or {}).get("type")
        svalue = (suggested or {}).get("value")
        if stype in _MATCH_TYPES and svalue:
            match = {"type": stype, "value": svalue}
            if stype == "argv_glob":
                if isinstance(svalue, list) and svalue and all(isinstance(v, str) for v in svalue):
                    match = {"type": "argv_glob", "value": [str(v) for v in svalue]}
                else:
                    match = {}
        if not match:
            tokens = action.split()
            if len(tokens) >= 2 and not tokens[1].startswith("-"):
                match = {"type": "prefix", "value": canonical_text(" ".join(tokens[:2]))}
            else:
                match = {"type": "exact", "value": canonical_text(action)}
        if match["type"] in ("prefix", "argv_glob") and decision in ("allow", "ask"):
            head = _norm_head(match["value"])
            if head in BANNED_PREFIX_HEADS:
                logger.info(f"[permission-rules] 拒绝落盘入口前缀规则：{head!r}（{action[:60]}）")
                return None
            if head in DESTRUCTIVE_HEADS or head.startswith("mkfs"):
                match = {"type": "exact", "value": canonical_text(action)}
    elif kind in ("path:read", "path:write", "path:delete"):
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
        "id": new_rule_id(),
        "enabled": True,
        "decision": decision,
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
    """命令放行时对其越界写/删目标派生的 allow 路径规则。"""
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
            decision="allow", suggested={"type": "prefix", "value": os.path.dirname(resolved) or resolved},
            granted_by=granted_by, summary=f"命令放行隐含（{decision_id}）", decision_id=decision_id,
        )
        if rule:
            rules.append(rule)
    return rules


# ─── 约束 ──────────────────────────────────────────────────────────────

DEFAULT_CONSTRAINTS: list[dict] = [
    {"id": "cross-tool-isolation", "severity": "deny",
     "text": "bash 不得调用 pwsh/powershell，pwsh 不得调用 bash/sh；两者都不得调用 cmd/wsl。这是工具路由架构约束，用户批准也不能改变。"},
    {"id": "code-from-string", "severity": "deny",
     "text": "eval / Invoke-Expression / iex 属于字符串代码执行，载荷藏在字符串里无法静态审计，应拒绝并建议直接执行等价命令。"},
    {"id": "python-dynamic-exec", "severity": "deny",
     "text": "python 代码中的 eval/exec/compile/__import__、os.system/popen、subprocess.*、importlib.import_module、from x import * 属于动态执行或 shell 逃逸，应拒绝。"},
    {"id": "workspace-boundary", "severity": "warn",
     "text": "文件写入/删除应限制在工作区与系统临时目录内；越界写删按权限设置处理。"},
    {"id": "system-destructive", "severity": "warn",
     "text": "磁盘格式化/清盘、关机重启、系统进程终止等破坏性系统操作需要用户明确确认。"},
    {"id": "ssrf-navigation", "severity": "deny",
     "text": "file://、data: 等 scheme 与云元数据端点（169.254.169.254 等）任何时候都不允许导航，用户批准也不能解锁。"},
    {"id": "ssrf-navigation-approve", "severity": "warn",
     "text": "非公网地址（localhost、私网 IP、环回）导航是合法的本地开发场景，可放行但需用户确认。"},
    {"id": "outside-read", "severity": "warn",
     "text": "读取工作区外文件时只评估路径元数据（路径、文件名、目录、是否敏感），不读取文件内容。"},
]


# ─── 规则存储 ──────────────────────────────────────────────────────────

_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path | None) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(str(path))) if path else ""
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


class PermissionRules:
    """统一规则存储与匹配（全局层 + 工作区层）。"""

    def __init__(
        self,
        workspace_path: str | Path | None = None,
        global_path: str | Path | None = None,
        *,
        workspace_root: str = "",
        hot_reload: bool = True,
    ) -> None:
        self._workspace_path = Path(workspace_path) if workspace_path else None
        self._global_path = Path(global_path) if global_path else None
        self._workspace_root = workspace_root
        self._hot = hot_reload
        self._lock = threading.RLock()
        self._workspace_rules: list[dict] = []
        self._global_rules: list[dict] = []
        self._workspace_constraints: list[dict] = []
        self._global_constraints: list[dict] = []
        self._sigs: dict[str, tuple | None] = {"workspace": None, "global": None}
        self._last_hit_flush = 0.0
        self.load()

    # ── 加载 ──────────────────────────────────────────────────────

    def _paths(self) -> dict[str, Path | None]:
        return {"workspace": self._workspace_path, "global": self._global_path}

    @staticmethod
    def _sig(path: Path | None) -> tuple | None:
        if path is None:
            return None
        try:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _ensure_loaded(self) -> None:
        if not self._hot:
            return
        sigs = {scope: self._sig(path) for scope, path in self._paths().items()}
        if sigs != self._sigs:
            self.load()

    def load(self) -> None:
        with self._lock:
            for scope, path in self._paths().items():
                if scope == "workspace":
                    self._workspace_rules, self._workspace_constraints = self._load_one(path, scope)
                else:
                    self._global_rules, self._global_constraints = self._load_one(path, scope)
            self._sigs = {scope: self._sig(path) for scope, path in self._paths().items()}
            logger.info(
                f"[permission-rules] 已加载 workspace={len(self._workspace_rules)} "
                f"global={len(self._global_rules)}"
            )

    def _load_one(self, path: Path | None, scope: str) -> tuple[list[dict], list[dict]]:
        if path is None:
            return [], []
        data = load_json_or_backup(path, {})
        if not isinstance(data, dict):
            data = {}
        rules: list[dict] = []
        for raw in data.get("rules") or []:
            rule = self._validate_rule(raw, scope)
            if rule is None:
                logger.warning(f"[permission-rules] 跳过非法规则（{scope}）：{raw!r}")
                continue
            rules.append(rule)
        constraints = [
            c for c in (data.get("constraints") or [])
            if isinstance(c, dict) and c.get("id") and c.get("text")
        ]
        return rules, constraints

    def _validate_rule(self, raw: dict, scope: str) -> dict | None:
        if not isinstance(raw, dict):
            return None
        decision = str(raw.get("decision", "allow"))
        if decision not in _DECISIONS:
            return None
        kind = str(raw.get("kind", "command"))
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
        if kind != "command" and match["type"] == "argv_glob":
            return None
        if match["type"] in ("prefix", "argv_glob") and kind == "command" and decision in ("allow", "ask"):
            head = _norm_head(value)
            if head in BANNED_PREFIX_HEADS:
                return None
            if head in DESTRUCTIVE_HEADS or head.startswith("mkfs"):
                value_text = " ".join(str(v) for v in value) if isinstance(value, list) else str(value)
                match = {"type": "exact", "value": canonical_text(value_text)}
                if not match["value"]:
                    return None
        rule = dict(raw)
        rule["decision"] = decision
        rule["kind"] = kind
        rule["match"] = match
        rule.setdefault("id", new_rule_id())
        rule.setdefault("enabled", True)
        rule.setdefault("source", "user")
        rule.setdefault("tool", "*")
        rule["scope"] = scope
        return rule

    # ── 查询 / CRUD ────────────────────────────────────────────────

    def all_rules(self, scope: str | None = None) -> list[dict]:
        with self._lock:
            self._ensure_loaded()
            if scope == "workspace":
                source = self._workspace_rules
            elif scope == "global":
                source = self._global_rules
            else:
                source = self._workspace_rules + self._global_rules
            return [dict(r) for r in source]

    def get_rule(self, rule_id: str, scope: str) -> dict | None:
        with self._lock:
            self._ensure_loaded()
            source = self._rules_for(scope)
            return next((dict(r) for r in source if r.get("id") == rule_id), None)

    def _rules_for(self, scope: str) -> list[dict]:
        return self._workspace_rules if scope == "workspace" else self._global_rules

    def _path_for(self, scope: str) -> Path | None:
        return self._workspace_path if scope == "workspace" else self._global_path

    def add_rule(self, rule: dict, scope: str = "workspace") -> dict | None:
        validated = self._validate_rule(rule, scope)
        if validated is None:
            return None
        with self._lock:
            self._ensure_loaded()
            target = self._rules_for(scope)
            key = (
                validated.get("tool", "*"), validated["kind"], validated["decision"],
                validated["match"]["type"], str(validated["match"]["value"]).lower(),
            )
            for existing in target:
                ekey = (
                    existing.get("tool", "*"), existing["kind"], existing["decision"],
                    existing["match"]["type"], str(existing["match"]["value"]).lower(),
                )
                if ekey == key:
                    self._bump(existing)
                    return dict(existing)
            target.append(validated)
            self.save(scope)
            return dict(validated)

    def update_rule(self, rule_id: str, updates: dict, scope: str) -> dict | None:
        with self._lock:
            self._ensure_loaded()
            target = self._rules_for(scope)
            for idx, existing in enumerate(target):
                if existing.get("id") != rule_id:
                    continue
                if existing.get("source") == "builtin":
                    return None
                merged = {**existing, **updates}
                validated = self._validate_rule(merged, scope)
                if validated is None:
                    return None
                validated["id"] = rule_id
                target[idx] = validated
                self.save(scope)
                return dict(validated)
            return None

    def delete_rule(self, rule_id: str, scope: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            target = self._rules_for(scope)
            for idx, existing in enumerate(target):
                if existing.get("id") == rule_id:
                    if existing.get("source") == "builtin":
                        return False
                    target.pop(idx)
                    self.save(scope)
                    return True
            return False

    def revoke_judge_rules(self, scope: str) -> int:
        with self._lock:
            self._ensure_loaded()
            target = self._rules_for(scope)
            kept = [r for r in target if r.get("source") != "judge"]
            removed = len(target) - len(kept)
            if removed:
                if scope == "workspace":
                    self._workspace_rules = kept
                else:
                    self._global_rules = kept
                self.save(scope)
            return removed

    def save(self, scope: str) -> None:
        path = self._path_for(scope)
        if path is None:
            return
        with _path_lock(path):
            rules = self._workspace_rules if scope == "workspace" else self._global_rules
            constraints = self._workspace_constraints if scope == "workspace" else self._global_constraints
            data = {"version": RULES_VERSION, "rules": rules, "constraints": constraints}
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(path, data)
                self._sigs[scope] = self._sig(path)
            except OSError as e:
                logger.warning(f"[permission-rules] 写盘失败（{scope}）：{e}")

    # ── 匹配 ──────────────────────────────────────────────────────

    def _candidates(self, kind: str, tool: str, decision: str) -> list[dict]:
        result: list[dict] = []
        for source in (self._workspace_rules, self._global_rules):
            for rule in source:
                if not rule.get("enabled", True):
                    continue
                if rule.get("kind") != kind or rule.get("decision") != decision:
                    continue
                if rule.get("tool", "*") not in ("*", tool):
                    continue
                result.append(rule)
        return result

    def _bump(self, rule: dict) -> None:
        rule["hits"] = int(rule.get("hits") or 0) + 1
        rule["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        now = time.monotonic()
        if now - self._last_hit_flush >= 5.0:
            self._last_hit_flush = now
            scope = rule.get("scope", "workspace")
            self.save(scope)

    @staticmethod
    def _match_segment(rule: dict, segment: str, tokens: list[str]) -> bool:
        match = rule["match"]
        if match["type"] == "exact":
            return canonical_text(match["value"]) == segment
        if match["type"] == "prefix":
            value = canonical_text(match["value"])
            if segment == value or segment.startswith(value + " "):
                return True
            # 以 / 或 \ 结尾的前缀是路径子树：`rm -rf /` 应命中 `rm -rf /tmp/x`
            return value.endswith(("/", "\\")) and segment.startswith(value)
        return _glob_match(match["value"], tokens)

    def evaluate_command(self, tool: str, command: str) -> tuple[str | None, dict | None]:
        """命令裁决：返回 (decision, matched_rule)；未命中 (None, None)。

        deny/ask 任一段命中即返回；allow 必须每段都命中。
        """
        command = command or ""
        canon = canonical_text(command)
        if not canon:
            return None, None
        self._ensure_loaded()
        with self._lock:
            for decision in ("deny", "ask"):
                candidates = self._candidates("command", tool, decision)
                for rule in candidates:
                    if self._match_segment(rule, canon, canon.split()):
                        self._bump(rule)
                        return decision, rule
                if _has_substitution(command):
                    continue
                mode = "pwsh" if tool == "pwsh" else "bash"
                segments = [canonical_text(s) for s in _split_command(command, mode) if canonical_text(s)]
                for rule in candidates:
                    for seg in segments:
                        if self._match_segment(rule, seg, seg.split()):
                            self._bump(rule)
                            return decision, rule
            # allow：exact 整条，或每段都被 allow 规则覆盖
            allow_candidates = self._candidates("command", tool, "allow")
            for rule in allow_candidates:
                if rule["match"]["type"] == "exact" and canonical_text(rule["match"]["value"]) == canon:
                    self._bump(rule)
                    return "allow", rule
            if _has_substitution(command):
                return None, None
            mode = "pwsh" if tool == "pwsh" else "bash"
            segments = [canonical_text(s) for s in _split_command(command, mode) if canonical_text(s)]
            if not segments:
                return None, None
            covered: list[dict] = []
            for seg in segments:
                hit = None
                for rule in allow_candidates:
                    if self._match_segment(rule, seg, seg.split()):
                        hit = rule
                        break
                if hit is None:
                    return None, None
                covered.append(hit)
            for rule in covered:
                self._bump(rule)
            return "allow", covered[0]

    def evaluate_path(self, kind: str, resolved: str, tool: str = "*") -> tuple[str | None, dict | None]:
        if kind not in ("path:read", "path:write", "path:delete") or not resolved:
            return None, None
        self._ensure_loaded()
        with self._lock:
            for decision in ("deny", "ask", "allow"):
                for rule in self._candidates(kind, tool, decision):
                    match = rule["match"]
                    if match["type"] == "prefix" and _path_prefix_covers(match["value"], resolved):
                        self._bump(rule)
                        return decision, rule
                    if match["type"] == "exact" and os.path.normcase(
                        os.path.abspath(match["value"])
                    ) == os.path.normcase(os.path.abspath(resolved)):
                        self._bump(rule)
                        return decision, rule
        return None, None

    def evaluate_url(self, url: str, tool: str = "browser") -> tuple[str | None, dict | None]:
        url = (url or "").strip()
        if not url:
            return None, None
        self._ensure_loaded()
        with self._lock:
            for decision in ("deny", "ask", "allow"):
                for rule in self._candidates("browser:navigate", tool, decision):
                    match = rule["match"]
                    value = str(match["value"]).strip()
                    if match["type"] == "exact" and value == url:
                        self._bump(rule)
                        return decision, rule
                    if match["type"] == "prefix" and url.startswith(value):
                        self._bump(rule)
                        return decision, rule
        return None, None

    def constraints(self) -> list[dict]:
        self._ensure_loaded()
        merged: dict[str, dict] = {c["id"]: dict(c) for c in DEFAULT_CONSTRAINTS}
        with self._lock:
            for c in self._workspace_constraints + self._global_constraints:
                merged[c["id"]] = {"severity": c.get("severity", "warn"), **c}
        return list(merged.values())

    def granted_displays(self, limit: int = 20) -> list[str]:
        self._ensure_loaded()
        with self._lock:
            rules = self._workspace_rules + self._global_rules
            return [
                r.get("display", "") for r in rules
                if r.get("enabled", True) and r.get("decision") == "allow"
            ][:limit]

    # ── 内置展示 ──────────────────────────────────────────────────

    @staticmethod
    def builtin_rules() -> list[dict]:
        return builtin_deny_rules()

    @staticmethod
    def readonly_groups() -> dict[str, tuple[str, ...]]:
        return dict(READONLY_GROUPS)
