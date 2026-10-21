"""青少年模式：进程级全局开关（家长密码 + 解锁窗口 + 策略 + 审计 + 时长）。

面向家长可控、可审计的受限模式。状态持久化在 ``data/cili/teen_mode.json``
（独立于 setting.json——后者会被前端 PUT 全量替换，不适合放安全状态）。
读取按 (path, mtime_ns, size) 指纹缓存 + 热重载，外部改动（家长手工编辑、
恢复流程删文件重启）会被及时察觉。

服务端强制：所有限制查询走本模块，前端只做灰显与提示。解锁状态由服务端
单例持有（作用域字典：``*`` 全局、``ws:<uuid>`` 单工作区），不依赖 cookie
——单用户自托管场景下「家长在场 N 分钟」就是授权语义，Runner 后台线程
也能读到同一状态。

硬禁语义：``hard=True`` 的动作（会话删除、消息撤销、执行日志删除、工作区
删除/重置）在模式开启期间即使解锁也不放行——要删除，必须先关闭青少年模式。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

from core.config import DATA_DIR
from core.fs_utils import atomic_write_json, load_json_or_backup

logger = logging.getLogger(__name__)

STATE_PATH = DATA_DIR / "teen_mode.json"
AUDIT_DIR = DATA_DIR / "teen"
AUDIT_PATH = AUDIT_DIR / "audit.jsonl"
USAGE_PATH = AUDIT_DIR / "usage.json"

SCHEMA_VERSION = 1

# 解锁作用域：`*` 全局（设置、审批），`ws:<uuid>` 单个工作区
SCOPE_GLOBAL = "*"

DEFAULT_UNLOCK_MINUTES = 15
UNLOCK_WINDOW_OPTIONS = (5, 15, 30, 60)

# 密码失败限流：连续 FAIL_LIMIT 次错误锁 BASE_LOCK_MINUTES，之后指数退避
FAIL_LIMIT = 5
BASE_LOCK_MINUTES = 5
MAX_LOCK_MINUTES = 60

DEFAULT_POLICY: dict = {
    "deny_hide": True,            # 禁止隐藏会话（隐藏会让家长看不到）
    "deny_share": True,           # 禁止分享会话链接（内容出口）
    "deny_memory_delete": True,   # 禁止记忆删除/归档
    "deny_file_delete": False,    # 工作区内文件删除：默认放开（写代码需要）
    "outside_workspace": "deny",  # 越界写/删：deny（替代现状 needs_approval）
    "allowed_workspaces": [],     # 工作区白名单；空 = 全部受限
    "deny_read_outside": True,    # 读侧限制（read/grep/glob 工作区外拒绝）
    "approval_strict": True,      # 审批 strict：未解锁不出卡、不落盘规则
    "mcp_tools": "deny",          # 外部 MCP 工具
    "browser_upload": "deny",     # 浏览器上传
    "cron": "deny",               # 定时任务增删改
    "prompt_block": True,         # 注入 teen_mode system block
}

DEFAULT_LIMITS: dict = {
    "daily_minutes": 0,           # 每日总时长（0 = 不限），按回合运行时间累计
    "allowed_windows": [],        # 允许使用时段，如 ["08:00-21:00"]；空 = 不限
}

# 被拒动作的中文文案（路由 403 detail / 接入端提示共用）
_DENY_MESSAGES: dict[str, str] = {
    "session_delete": "青少年模式已开启：会话删除已被禁用。如需删除，请家长在设置中先关闭青少年模式。",
    "message_revert": "青少年模式已开启：消息撤销已被禁用。",
    "exec_log_delete": "青少年模式已开启：执行日志删除已被禁用。",
    "workspace_delete": "青少年模式已开启：工作区删除已被禁用。",
    "workspace_reset": "青少年模式已开启：工作区重置已被禁用。",
    "session_hide": "青少年模式已开启：会话隐藏已被禁用。",
    "session_share": "青少年模式已开启：会话分享链接不可用。",
    "memory_delete": "青少年模式已开启：记忆删除/归档需要家长授权。",
    "config_update": "青少年模式已开启：该设置项已被锁定，需家长解锁后修改。",
    "mcp_call": "青少年模式已开启：外部 MCP 工具需要家长授权。",
    "cron_modify": "青少年模式已开启：定时任务的增删改需要家长授权。",
    "browser_upload": "青少年模式已开启：浏览器上传需要家长授权。",
    "file_delete": "青少年模式已开启：文件删除已被家长锁定。",
    "workspace_locked": "该工作区已锁定，需家长解锁后才能使用。",
    "approval_needed": "青少年模式已开启：该操作需要家长授权（解锁窗口内重试）。",
    "teen_admin": "青少年模式已开启：请先输入家长密码解锁。",
}

DEFAULT_DENY_MESSAGE = "青少年模式已开启：该操作需要家长授权。"


def deny_message(action: str) -> str:
    """被拒动作的中文提示文案。"""
    return _DENY_MESSAGES.get(action, DEFAULT_DENY_MESSAGE)


class TeenModeDenied(PermissionError):
    """青少年模式下被拒绝的操作（数据层断言 / 工作区守卫抛出）。

    Web 层由 web_api 的异常处理器转 403；接入端捕获后回中文提示。
    """

    def __init__(self, action: str, message: str = ""):
        self.action = action
        super().__init__(message or deny_message(action))


class TeenLockedOut(Exception):
    """密码连续错误触发限流锁定。"""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"密码错误次数过多，已锁定 {max(1, retry_after_seconds // 60)} 分钟")


def _now() -> datetime:
    return datetime.now()


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _hash_password(password: str, salt: bytes | None = None) -> dict:
    """scrypt 加盐哈希（不存明文）。"""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=16384, r=8, p=1,
        dklen=32, maxmem=64 * 1024 * 1024,
    )
    return {"algo": "scrypt", "salt": salt.hex(), "hash": digest.hex(),
            "n": 16384, "r": 8, "p": 1}


def _verify_password(password: str, record: dict) -> bool:
    try:
        expected = bytes.fromhex(record.get("hash", ""))
        salt = bytes.fromhex(record.get("salt", ""))
    except ValueError:
        return False
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=int(record.get("n", 16384)), r=int(record.get("r", 8)),
            p=int(record.get("p", 1)), dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, OSError):
        return False
    return hmac.compare_digest(digest, expected)


def _parse_windows(windows: list) -> list[tuple[int, int]]:
    """解析 ["08:00-21:00", ...] → [(分钟数, 分钟数)]；非法段忽略。"""
    result = []
    for w in windows or []:
        try:
            a, b = str(w).split("-", 1)
            ha, ma = a.strip().split(":", 1)
            hb, mb = b.strip().split(":", 1)
            start = int(ha) * 60 + int(ma)
            end = int(hb) * 60 + int(mb)
            if 0 <= start <= 1440 and 0 <= end <= 1440:
                result.append((start, end))
        except (ValueError, AttributeError):
            continue
    return result


def _in_window(now_minute: int, windows: list[tuple[int, int]]) -> bool:
    for start, end in windows:
        if start <= end:
            if start <= now_minute < end:
                return True
        else:  # 跨 midnight，如 22:00-07:00
            if now_minute >= start or now_minute < end:
                return True
    return False


class TeenModeManager:
    """青少年模式单例：状态读写、密码、解锁窗口、策略、审计、时长记账。"""

    def __init__(self, state_path: Path | None = None,
                 audit_path: Path | None = None,
                 usage_path: Path | None = None):
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self.audit_path = Path(audit_path) if audit_path else AUDIT_PATH
        self.usage_path = Path(usage_path) if usage_path else USAGE_PATH
        self._cache: dict = {}
        self._sig: tuple | None = None
        self._mutex = threading.RLock()
        self._audit_mutex = threading.Lock()

    # ----- 状态读写（指纹缓存 + 热重载） -----

    def _default_state(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "enabled": False,
            "password": {},
            "created_at": "",
            "updated_at": "",
            "policy": dict(DEFAULT_POLICY),
            "limits": dict(DEFAULT_LIMITS),
            "unlock": {
                "window_minutes": DEFAULT_UNLOCK_MINUTES,
                "scopes": {},
                "failed_attempts": 0,
                "locked_until": "",
            },
        }

    def _load(self) -> dict:
        """读取状态；按 (path, mtime_ns, size) 指纹缓存，变更/损坏自动重读回退。"""
        try:
            st = self.state_path.stat()
            sig = (str(self.state_path), st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None  # 文件不存在 = 模式关闭

        if sig is not None and sig == self._sig:
            return self._cache

        if sig is None:
            data = {}
        else:
            data = load_json_or_backup(self.state_path, {}) or {}
            if not isinstance(data, dict):
                data = {}
            logger.info("[teen] 状态文件已重载: %s", self.state_path)

        self._cache = data
        self._sig = sig
        return data

    def _save(self, state: dict) -> None:
        state["updated_at"] = _ts(_now())
        atomic_write_json(self.state_path, state)
        # 写后自更新缓存与指纹，避免紧接着的读触发重读（原子写改变 mtime/size）
        try:
            st = self.state_path.stat()
            self._sig = (str(self.state_path), st.st_mtime_ns, st.st_size)
        except OSError:
            self._sig = None
        self._cache = state

    def _mutate(self, fn) -> dict:
        """持锁读-改-写状态。fn(state) 原地修改，返回 state。"""
        with self._mutex:
            state = self._load()
            state = {**self._default_state(), **state} if state else self._default_state()
            # 补齐缺失的嵌套默认值（旧文件兼容）
            for key in ("policy", "limits", "unlock"):
                if not isinstance(state.get(key), dict):
                    state[key] = dict(DEFAULT_POLICY if key == "policy"
                                      else DEFAULT_LIMITS if key == "limits" else {})
            state["policy"] = {**DEFAULT_POLICY, **state["policy"]}
            state["limits"] = {**DEFAULT_LIMITS, **state["limits"]}
            fn(state)
            self._save(state)
            return state

    # ----- 状态查询 -----

    def is_enabled(self) -> bool:
        return bool(self._load().get("enabled"))

    def policy(self) -> dict:
        state = self._load()
        merged = {**DEFAULT_POLICY, **(state.get("policy") or {})}
        return merged if self.is_enabled() else dict(DEFAULT_POLICY)

    def limits(self) -> dict:
        state = self._load()
        return {**DEFAULT_LIMITS, **(state.get("limits") or {})} if self.is_enabled() else dict(DEFAULT_LIMITS)

    def status(self) -> dict:
        """公开状态（供前端灰显；不含密码哈希）。"""
        state = self._load()
        enabled = bool(state.get("enabled"))
        unlock_cfg = state.get("unlock") or {}
        locked_until = unlock_cfg.get("locked_until") or ""
        return {
            "enabled": enabled,
            "unlock_until": (unlock_cfg.get("scopes") or {}).get(SCOPE_GLOBAL, ""),
            "unlock_scopes": dict(unlock_cfg.get("scopes") or {}),
            "password_locked_until": locked_until,
            "window_minutes": unlock_cfg.get("window_minutes", DEFAULT_UNLOCK_MINUTES),
            "policy": {**DEFAULT_POLICY, **(state.get("policy") or {})} if enabled else dict(DEFAULT_POLICY),
            "limits": {**DEFAULT_LIMITS, **(state.get("limits") or {})} if enabled else dict(DEFAULT_LIMITS),
            "usage_today": self.usage(),
            "created_at": state.get("created_at", ""),
            "updated_at": state.get("updated_at", ""),
        }

    # ----- 开关与密码 -----

    def enable(self, password: str, policy: dict | None = None,
               limits: dict | None = None, window_minutes: int = DEFAULT_UNLOCK_MINUTES) -> None:
        if len(password or "") < 6:
            raise ValueError("密码长度至少 6 位")
        if not isinstance(window_minutes, int) or window_minutes not in UNLOCK_WINDOW_OPTIONS:
            window_minutes = DEFAULT_UNLOCK_MINUTES

        def _apply(state: dict) -> None:
            state["enabled"] = True
            state["password"] = _hash_password(password)
            state["created_at"] = state.get("created_at") or _ts(_now())
            state["policy"] = {**DEFAULT_POLICY, **(policy or {})}
            state["limits"] = {**DEFAULT_LIMITS, **(limits or {})}
            state["unlock"] = {
                "window_minutes": window_minutes,
                "scopes": {},
                "failed_attempts": 0,
                "locked_until": "",
            }

        # 审计在落盘后写：is_enabled() 快照才反映新状态（启动审计依赖它）
        self._mutate(_apply)
        self.audit("enable", source="web")

    def disable(self, password: str, source: str = "web") -> None:
        if not self.is_enabled():
            return
        self._require_unlock_scope("disable")
        if not self.verify_password(password):
            self.audit("unlock_failed", action="disable", source=source)
            raise PermissionError("密码错误")
        # 关闭前审计（此时 enabled 仍为 True，audit 的 enabled 快照才有意义）
        self.audit("disable", source=source)

        def _apply(state: dict) -> None:
            state["enabled"] = False
            unlock = state.get("unlock") or {}
            unlock["scopes"] = {}
            unlock["failed_attempts"] = 0
            unlock["locked_until"] = ""
            state["unlock"] = unlock

        self._mutate(_apply)

    def change_password(self, old_password: str, new_password: str) -> None:
        self._require_unlock_scope("password_change")
        if len(new_password or "") < 6:
            raise ValueError("新密码长度至少 6 位")
        if not self.verify_password(old_password):
            self.audit("unlock_failed", action="password_change", source="web")
            raise PermissionError("原密码错误")

        def _apply(state: dict) -> None:
            state["password"] = _hash_password(new_password)
            self.audit("password_change", source="web")

        self._mutate(_apply)

    def _require_unlock_scope(self, action: str) -> None:
        """管理模式操作（改密/关模式/改策略）要求全局解锁窗口。"""
        if self.is_enabled() and not self.is_unlocked(SCOPE_GLOBAL):
            raise TeenModeDenied("teen_admin")

    def verify_password(self, password: str) -> bool:
        """校验密码，带失败限流（连续错误触发指数退避锁定）。"""
        state = self._load()
        unlock = state.get("unlock") or {}
        locked_until = _parse_ts(unlock.get("locked_until") or "")
        now = _now()
        if locked_until and locked_until > now:
            raise TeenLockedOut(int((locked_until - now).total_seconds()) + 1)
        record = state.get("password") or {}
        if record and _verify_password(password or "", record):
            if unlock.get("failed_attempts"):
                def _reset(state_: dict) -> None:
                    u = state_.get("unlock") or {}
                    u["failed_attempts"] = 0
                    u["locked_until"] = ""
                    state_["unlock"] = u
                self._mutate(_reset)
            return True
        return False

    def register_password_failure(self) -> int:
        """记录一次密码失败，返回当前连续失败次数（达到阈值时设置锁定）。"""

        def _apply(state: dict) -> int:
            unlock = state.get("unlock") or {}
            attempts = int(unlock.get("failed_attempts") or 0) + 1
            unlock["failed_attempts"] = attempts
            if attempts >= FAIL_LIMIT:
                over = attempts - FAIL_LIMIT
                minutes = min(BASE_LOCK_MINUTES * (2 ** (over // FAIL_LIMIT)), MAX_LOCK_MINUTES)
                unlock["locked_until"] = _ts(_now() + timedelta(minutes=minutes))
            state["unlock"] = unlock
            return attempts

        return self._mutate(_apply)

    # ----- 解锁窗口 -----

    def unlock(self, password: str, scope: str = SCOPE_GLOBAL,
               minutes: int | None = None, source: str = "web", ip: str = "") -> str:
        """验证密码并开启解锁窗口，返回窗口到期时间。"""
        if not isinstance(scope, str) or not (scope == SCOPE_GLOBAL or
                                              (scope.startswith("ws:") and len(scope) > 3)):
            raise ValueError("无效的解锁作用域")
        if not self.is_enabled():
            raise TeenModeDenied("teen_admin", "青少年模式未开启")
        if not self.verify_password(password):
            attempts = self.register_password_failure()
            self.audit("unlock_failed", source=source, detail={"attempts": attempts, "scope": scope})
            raise PermissionError("密码错误")
        window = minutes if (isinstance(minutes, int) and minutes in UNLOCK_WINDOW_OPTIONS) \
            else int((self._load().get("unlock") or {}).get("window_minutes") or DEFAULT_UNLOCK_MINUTES)
        until = _ts(_now() + timedelta(minutes=window))

        def _apply(state: dict) -> None:
            unlock = state.get("unlock") or {}
            scopes = unlock.get("scopes") or {}
            scopes[scope] = until
            unlock["scopes"] = scopes
            unlock["failed_attempts"] = 0
            unlock["locked_until"] = ""
            state["unlock"] = unlock

        self._mutate(_apply)
        self.audit("unlock", source=source, ip=ip, detail={"scope": scope, "until": until})
        return until

    def lock(self, scope: str | None = None, source: str = "web", ip: str = "") -> None:
        """立即结束解锁窗口；scope=None 清空全部作用域。"""

        def _apply(state: dict) -> None:
            unlock = state.get("unlock") or {}
            if scope is None:
                unlock["scopes"] = {}
            else:
                scopes = unlock.get("scopes") or {}
                scopes.pop(scope, None)
                unlock["scopes"] = scopes
            state["unlock"] = unlock

        self._mutate(_apply)
        self.audit("lock", source=source, ip=ip, detail={"scope": scope or SCOPE_GLOBAL})

    def is_unlocked(self, scope: str = SCOPE_GLOBAL) -> bool:
        """该作用域（或全局）是否处于解锁窗口内。"""
        if not self.is_enabled():
            return False
        scopes = (self._load().get("unlock") or {}).get("scopes") or {}
        now = _now()
        for key in (scope, SCOPE_GLOBAL):
            if key == scope or key == SCOPE_GLOBAL:
                expiry = _parse_ts(scopes.get(key, ""))
                if expiry and expiry > now:
                    return True
        return False

    # ----- 策略 -----

    def update_policy(self, policy: dict | None = None,
                      limits: dict | None = None,
                      window_minutes: int | None = None,
                      source: str = "web") -> None:
        self._require_unlock_scope("config_update")

        def _apply(state: dict) -> None:
            if policy is not None:
                merged = {**DEFAULT_POLICY, **(state.get("policy") or {})}
                for key, value in policy.items():
                    if key in DEFAULT_POLICY:
                        merged[key] = value
                state["policy"] = merged
            if limits is not None:
                merged = {**DEFAULT_LIMITS, **(state.get("limits") or {})}
                for key, value in limits.items():
                    if key in DEFAULT_LIMITS:
                        merged[key] = value
                state["limits"] = merged
            if window_minutes in UNLOCK_WINDOW_OPTIONS:
                unlock = state.get("unlock") or {}
                unlock["window_minutes"] = window_minutes
                state["unlock"] = unlock
            self.audit("policy_update", source=source,
                       detail={"keys": list((policy or {}).keys()) + list((limits or {}).keys())})

        self._mutate(_apply)

    def approval_strict(self) -> bool:
        """审批 strict：未解锁时不出审批卡、判定放行不落盘、高风险直接拒绝。"""
        return self.is_enabled() and bool(self.policy().get("approval_strict"))

    # ----- 工作区访问（白名单 + 锁定） -----

    def workspace_access(self, workspace_uuid: str) -> str:
        """allow | locked。白名单内或处于解锁作用域内 → allow；关闭时一律 allow。"""
        if not workspace_uuid:
            return "allow"
        if not self.is_enabled():
            return "allow"
        if self.is_unlocked(SCOPE_GLOBAL):
            return "allow"
        if self.is_unlocked(f"ws:{workspace_uuid}"):
            return "allow"
        allowed = self.policy().get("allowed_workspaces") or []
        if not allowed:
            return "locked"  # 空 = 全部受限（默认最严）
        return "allow" if workspace_uuid in allowed else "locked"

    def guard_workspace(self, workspace_uuid: str, source: str = "core") -> None:
        """受限工作区判定：locked 时记审计并抛 TeenModeDenied。"""
        if self.workspace_access(workspace_uuid) == "locked":
            self.audit("deny", action="workspace_locked", target=workspace_uuid, source=source)
            self.record_denied("workspace_locked")
            raise TeenModeDenied("workspace_locked")

    # ----- 通用动作守卫 -----

    def ensure_allowed(self, action: str, *, hard: bool = False,
                       target: str = "", source: str = "core") -> None:
        """动作放行判定。hard=True：开启即拒（解锁也不放行）；否则解锁窗口内放行。"""
        if not self.is_enabled():
            return
        if not hard and self.is_unlocked(SCOPE_GLOBAL):
            return
        self.audit("deny", action=action, target=target, source=source)
        self.record_denied(action)
        raise TeenModeDenied(action)

    # ----- 审计 -----

    def audit(self, event: str, action: str = "", target: str = "",
              source: str = "", ip: str = "", detail: dict | None = None) -> None:
        """追加一条审计记录（JSONL，追加式，进程内互斥防交错）。"""
        entry: dict = {"ts": _ts(_now()), "event": event}
        if action:
            entry["action"] = action
        if target:
            entry["target"] = target
        if source:
            entry["source"] = source
        if ip:
            entry["ip"] = ip
        if detail:
            entry["detail"] = detail
        entry["enabled"] = self.is_enabled()
        line = json.dumps(entry, ensure_ascii=False)
        try:
            with self._audit_mutex:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.audit_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            logger.exception("[teen] 审计写入失败")

    def read_audit(self, limit: int = 200) -> list[dict]:
        """读取最近 limit 条审计记录（旧→新）。"""
        if not self.audit_path.exists():
            return []
        try:
            with open(self.audit_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        out = []
        for line in lines[-max(1, int(limit)):]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def check_startup_state(self) -> None:
        """启动审计：检测「由开启变为关闭/文件丢失」（外部删除文件的后门路径）。"""
        last_enabled = None
        for entry in reversed(self.read_audit(limit=500)):
            if entry.get("event") in ("enable", "disable", "startup_state_change"):
                last_enabled = bool(entry.get("enabled"))
                break
        current = self.is_enabled()
        if last_enabled and not current:
            self.audit("startup_state_change", source="startup",
                       detail={"from": True, "to": current,
                               "file_exists": self.state_path.exists()})
            logger.warning("[teen] 检测到模式由开启变为关闭（可能为外部干预），已记录审计")

    # ----- 用量与时长（P3） -----

    def usage(self) -> dict:
        """今日用量（跨日自动重置）：{date, minutes, turns, denied}。"""
        today = _now().strftime("%Y-%m-%d")
        data = load_json_or_backup(self.usage_path, {}) or {}
        if not isinstance(data, dict) or data.get("date") != today:
            return {"date": today, "minutes": 0.0, "turns": 0, "denied": {}}
        return {
            "date": today,
            "minutes": float(data.get("minutes") or 0),
            "turns": int(data.get("turns") or 0),
            "denied": dict(data.get("denied") or {}),
        }

    def record_turn_seconds(self, seconds: float, turns: int = 1) -> None:
        """累计回合运行时间（跨会话合并）。"""
        with self._mutex:
            today = _now().strftime("%Y-%m-%d")
            data = load_json_or_backup(self.usage_path, {}) or {}
            if not isinstance(data, dict) or data.get("date") != today:
                data = {"date": today, "minutes": 0.0, "turns": 0, "denied": {}}
            data["minutes"] = round(float(data.get("minutes") or 0) + max(0.0, seconds) / 60.0, 2)
            data["turns"] = int(data.get("turns") or 0) + max(0, turns)
            try:
                atomic_write_json(self.usage_path, data)
            except OSError:
                logger.exception("[teen] 用量写入失败")

    def record_denied(self, action: str) -> None:
        """累计今日被拦截操作计数（用于家长面板）。"""
        try:
            with self._mutex:
                today = _now().strftime("%Y-%m-%d")
                data = load_json_or_backup(self.usage_path, {}) or {}
                if not isinstance(data, dict) or data.get("date") != today:
                    data = {"date": today, "minutes": 0.0, "turns": 0, "denied": {}}
                denied = dict(data.get("denied") or {})
                denied[action] = int(denied.get(action) or 0) + 1
                data["denied"] = denied
                atomic_write_json(self.usage_path, data)
        except Exception:  # noqa: BLE001 —— 计数失败不影响拒绝本身
            logger.exception("[teen] 拦截计数失败")

    def check_send_allowed(self) -> None:
        """发消息前置检查：时长用尽 / 时段外直接拒绝（进行中的回合不受影响）。"""
        if not self.is_enabled():
            return
        limits_cfg = self.limits()
        daily = float(limits_cfg.get("daily_minutes") or 0)
        if daily > 0:
            used = self.usage().get("minutes", 0.0)
            if used >= daily:
                self.audit("limit_exceeded", action="daily_minutes",
                           detail={"used": used, "limit": daily})
                raise TeenModeDenied(
                    "time_limit",
                    f"今日可用时长已用完（{int(daily)} 分钟），明天 0 点重置。",
                )
        windows = _parse_windows(limits_cfg.get("allowed_windows") or [])
        if windows and not _in_window(_now().hour * 60 + _now().minute, windows):
            self.audit("limit_exceeded", action="time_window",
                       detail={"windows": limits_cfg.get("allowed_windows")})
            ranges = "、".join(str(w) for w in (limits_cfg.get("allowed_windows") or []))
            raise TeenModeDenied("time_window", f"当前不在允许使用时段（{ranges}）。")

    def usage_report(self, days: int = 7) -> dict:
        """近 N 天用量报表（家长面板）。usage.json 只存当天，历史靠审计粗算。"""
        report = {"today": self.usage(), "limits": self.limits(), "daily": []}
        # 历史逐日数据未持久化（只保留当天），先从审计聚合回合数
        per_day: dict[str, int] = {}
        for entry in self.read_audit(limit=2000):
            ts = _parse_ts(str(entry.get("ts", "")))
            if ts is None:
                continue
            day = ts.strftime("%Y-%m-%d")
            if entry.get("event") == "deny":
                per_day[day] = per_day.get(day, 0) + 1
        report["daily"] = [{"date": d, "denies": n}
                           for d, n in sorted(per_day.items())[-max(1, int(days)):]]
        return report


# ----- 模块级单例 -----

_manager: TeenModeManager | None = None
_manager_lock = threading.Lock()


def get_teen_mode() -> TeenModeManager:
    """全局 TeenModeManager 单例（测试可通过 set_teen_mode 替换）。"""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = TeenModeManager()
    return _manager


def set_teen_mode(manager: TeenModeManager | None) -> None:
    """替换/清空单例（测试注入临时目录用）。"""
    global _manager
    with _manager_lock:
        _manager = manager


def guard_or_raise(action: str, *, hard: bool = False, target: str = "",
                   source: str = "core") -> None:
    """数据层断言入口：物理删除/截断真正发生的位置调用。"""
    get_teen_mode().ensure_allowed(action, hard=hard, target=target, source=source)
