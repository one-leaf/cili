"""权限判定器：规则未命中且策略为 ``ai`` 时做一次结构化 LLM 判定。

调用形态：一次性 ``chat_structured``（复用 memory_pipeline 的 lite 先例），不起子 agent。
配置来自顶层 ``config.permissions.judge``（``setting.json → permissions``）。

- 超时、失败策略、连错降级、高置信门槛、会话缓存与旧审批判定器一致；
- ``outside.read=ai`` 只把路径元数据交给判定器，不读取/发送文件内容；
- 审计写 ``{session_dir}/approval_audit.jsonl``。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field, replace

from core.config import get_permission_config
from core.llm import Message, create_llm_client

logger = logging.getLogger(__name__)

_JUDGE_MAX_TOKENS = 2000

_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["allow", "ask", "deny"]},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string", "description": "一句话中文说明该动作做什么、影响什么"},
        "reason": {"type": "string", "description": "判定理由（中文）"},
        "alternative": {"type": "string", "description": "deny 时的替代做法建议（中文，可空）"},
        "suggested_rule": {
            "type": "object",
            "description": "建议落盘的 allow 规则粒度（可省略）",
            "properties": {
                "type": {"type": "string", "enum": ["exact", "prefix", "argv_glob"]},
                "value": {"description": "匹配值：exact/prefix 为字符串，argv_glob 为字符串数组"},
                "display": {"type": "string", "description": "规则的自然语言描述（中文）"},
            },
            "required": ["type", "value"],
        },
    },
    "required": ["decision", "summary", "reason"],
}

_SYSTEM_PROMPT = """你是 Cili Agent 的权限判定器。对给定的「待判定动作」输出结构化结论，不执行任何动作。

输入字段说明：
- action：待判定的命令 / Python 代码 / URL / 路径原文；
- kind：command / path:read / path:write / path:delete / browser:navigate；
- routing：为什么该动作进入 AI 判定（mode=ai、路由原因）；
- cwd / workspace_root：执行目录与工作区边界；
- role：master / worker / lite；
- extracted_targets：静态分析提取到的读/写/删目标（op=read/write/delete；inside_workspace 表示目标是否在工作区内）；
- path_metadata：kind=path:read 时的路径元数据（路径、文件名、目录、是否敏感；不含文件内容）；
- static_findings：静态规则扫描的命中证据（id 与 constraints 对应）；
- constraints：安全约束清单（severity=deny 为架构性约束；severity=warn 为需要用户确认的场景）；
- recent_approvals：本会话已放行的命令 / 导航 / 路径 / 已落盘规则；
- has_user_channel：当前是否存在可询问用户的通道（false 时对边界情况倾向 deny）。

判定准则：
1. deny：违反 severity=deny 约束、明确恶意、或破坏性极大概率不可逆的动作；
2. ask：有副作用但范围或目的不明确、越界写删、破坏性系统操作、陌生动作形态；
3. allow：目标明确、影响范围有限、与 recent_approvals 一致的常规操作；
4. 不确定或证据不足时输出 ask，**不允许低置信 allow**；
5. severity=deny 的约束应拒绝，除非该动作明显无副作用（如 --version / --help 查询）；
6. kind=path:read 时只收到路径元数据（路径、文件名、目录、是否敏感），不会收到文件内容；
   只判断“这个位置是否值得确认”，不要假设文件内容。

输出要求：
- summary / reason / alternative 一律中文；summary 是一句话说明动作做什么、影响什么（会展示给用户）；
- 相同输入必须给出一致结论；
- suggested_rule：建议落盘的 allow 规则粒度，type ∈ exact/prefix/argv_glob，value 为匹配值
  （argv_glob 为字符串数组）。prefix 的首个 token 不得是 bash/sh/sudo/env/python/pwsh/cmd/wsl
  等入口词，也不得是 rm/dd/format 等破坏性动词；拿不准就省略该字段。"""


@dataclass
class PermissionDecision:
    """一次判定的结论（judge 结论 / 失败策略兜底 / 缓存命中）。"""

    decision: str  # allow | ask | deny
    risk: str = "medium"
    summary: str = ""
    reason: str = ""
    alternative: str = ""
    suggested_rule: dict | None = None
    source: str = "judge"  # judge | cache | failure
    error: str = ""
    duration_ms: int | None = None
    usage: dict | None = field(default=None, repr=False)


class PermissionJudge:
    """一次判定 = 输入组装 + 一次结构化 LLM 调用 + 校验/缓存/降级。"""

    def __init__(
        self,
        config,
        rules,
        store=None,
        *,
        role: str = "",
        has_user_channel: bool = True,
        workspace_root: str = "",
        cwd: str = "",
        session_dir=None,
    ) -> None:
        self.config = config
        self.rules = rules
        self.store = store
        self.role = role
        self.has_user_channel = bool(has_user_channel)
        self.workspace_root = workspace_root
        self.cwd = cwd
        self.session_dir = session_dir
        self._consecutive_failures = 0
        self._cache: dict[str, dict] = {}

    # ── 主入口 ────────────────────────────────────────────────────

    def decide(self, permission: dict) -> PermissionDecision:
        cfg = self._cfg()
        did = str((permission or {}).get("decision_id") or "")
        if not cfg.enabled or not cfg.judge.enabled:
            return self._failure_decision(permission, cfg, "自动判定未启用")
        if did and did in self._cache:
            cached = dict(self._cache[did])
            cached["source"] = "cache"
            return PermissionDecision(**cached)

        self._audit("asked", permission, {"source": "judge"})
        if self._consecutive_failures >= max(1, cfg.judge.max_consecutive_failures):
            decision = self._failure_decision(
                permission, cfg,
                f"判定连续失败 {self._consecutive_failures} 次，本会话已停用自动判定",
            )
            self._audit("decided", permission, {"decision": decision.decision, "source": "failure"})
            return decision

        started = time.perf_counter()
        try:
            raw, usage = self._invoke(self._build_input(permission), cfg)
            decision = self._validate(raw)
        except Exception as e:  # noqa: BLE001 —— 任何判定失败都走失败策略
            logger.warning(f"[permission-judge] 判定失败：{e}")
            self._consecutive_failures += 1
            decision = self._failure_decision(permission, cfg, str(e))
        else:
            self._consecutive_failures = 0
            decision.usage = usage
            decision.summary = decision.summary or permission.get("command", "")[:120]
            if cfg.judge.cache == "session" and did:
                self._cache[did] = {
                    "decision": decision.decision, "risk": decision.risk, "summary": decision.summary,
                    "reason": decision.reason, "alternative": decision.alternative,
                    "suggested_rule": decision.suggested_rule, "source": "judge", "error": "",
                }
        decision.duration_ms = int((time.perf_counter() - started) * 1000)
        self._audit("decided", permission, {
            "decision": decision.decision, "risk": decision.risk, "summary": decision.summary[:120],
            "source": decision.source, "duration_ms": getattr(decision, "duration_ms", None),
        })
        return decision

    # ── 输入 / 调用 / 校验 ────────────────────────────────────────

    def _build_input(self, permission: dict) -> list[Message]:
        payload = {
            "tool": permission.get("tool", ""),
            "kind": permission.get("kind", "command"),
            "action": (permission.get("command") or "")[:8000],
            "routing": {
                "mode": permission.get("mode") or permission.get("verdict") or "ai",
                "reason": permission.get("reason", ""),
            },
            "cwd": self.cwd,
            "workspace_root": self.workspace_root,
            "role": self.role,
            "has_user_channel": self.has_user_channel,
            "extracted_targets": permission.get("targets") or [],
            "static_findings": permission.get("findings") or [],
            "constraints": self.rules.constraints() if self.rules else [],
            "recent_approvals": self._recent_approvals(),
        }
        if permission.get("kind") == "path:read":
            payload["path_metadata"] = {
                "path": permission.get("command", ""),
                "note": "仅路径元数据；不包含文件内容",
            }
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        return [Message(role="user", content=f"待判定动作：\n{text}")]

    def _recent_approvals(self) -> list[str]:
        """本会话已放行的命令 / 导航 / 路径 + 已落盘规则（限 10 条）。"""
        items: list[str] = []
        if self.store:
            items.extend(f"command: {c}" for c in self.store.approved_commands()[:5])
            items.extend(f"navigate: {u}" for u in self.store.approved_navigations()[:3])
            for rule in self.store.approved_path_rules()[:5]:
                items.append(f"{rule.get('kind', 'path')}: {rule.get('command', '')}")
        if self.rules:
            items.extend(f"rule: {d}" for d in self.rules.granted_displays(5))
        return items[:10]

    def _invoke(self, messages: list[Message], cfg) -> tuple[dict, dict | None]:
        result: dict = {}

        def _work() -> None:
            lite_model = self.config.model_for_role("lite")
            attempts = [replace(lite_model, reasoning="off"),
                        replace(lite_model, reasoning="low")]
            last_error: Exception | None = None
            for model_cfg in attempts:
                client = create_llm_client(model_cfg)
                try:
                    max_tokens = _JUDGE_MAX_TOKENS
                    cap = getattr(model_cfg, "max_output_tokens", None)
                    if isinstance(cap, int) and cap > 0:
                        max_tokens = min(max_tokens, cap)
                    resp = client.chat_structured(
                        messages, system=_SYSTEM_PROMPT, output_schema=_OUTPUT_SCHEMA,
                        max_tokens=max_tokens)
                    result["resp"] = resp
                    result["reasoning_used"] = model_cfg.reasoning
                    return
                except Exception as e:  # noqa: BLE001
                    last_error = e
                finally:
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001
                        pass
            raise last_error  # attempts 非空，必有最后一次异常

        thread = threading.Thread(target=_work, daemon=True)
        thread.start()
        thread.join(timeout=max(5, cfg.judge.timeout_seconds))
        if thread.is_alive():
            raise TimeoutError(f"判定超时（{cfg.judge.timeout_seconds}s）")
        if "resp" not in result:
            raise RuntimeError("判定线程异常退出")
        resp = result["resp"]
        usage = None
        u = getattr(resp, "usage", None)
        if u is not None:
            usage = {
                "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
                "cache_read_tokens": int(getattr(u, "cache_read_tokens", 0) or 0),
                "cache_creation_tokens": int(getattr(u, "cache_creation_tokens", 0) or 0),
                "api_calls": 1,
                "model": getattr(resp, "model", "") or "",
            }
        return resp, usage

    @staticmethod
    def _validate(raw: dict) -> PermissionDecision:
        if not isinstance(raw, dict):
            raise ValueError("判定输出不是对象")
        decision = raw.get("decision")
        if decision not in ("allow", "ask", "deny"):
            raise ValueError(f"decision 枚举外取值：{decision!r}")
        risk = raw.get("risk")
        if risk not in ("low", "medium", "high"):
            raise ValueError(f"risk 枚举外取值：{risk!r}")
        if not str(raw.get("summary") or "").strip() or not str(raw.get("reason") or "").strip():
            raise ValueError("缺少 summary/reason")
        suggested = raw.get("suggested_rule")
        if isinstance(suggested, dict):
            stype = suggested.get("type")
            svalue = suggested.get("value")
            ok = stype in ("exact", "prefix", "argv_glob") and (
                isinstance(svalue, str) and svalue.strip()
                or (stype == "argv_glob" and isinstance(svalue, list) and svalue)
            )
            if not ok:
                suggested = None
        else:
            suggested = None
        return PermissionDecision(
            decision=decision, risk=risk,
            summary=str(raw.get("summary") or "").strip(),
            reason=str(raw.get("reason") or "").strip(),
            alternative=str(raw.get("alternative") or "").strip(),
            suggested_rule=suggested, source="judge",
        )

    # ── 失败策略 / 审计 ───────────────────────────────────────────

    def _failure_decision(self, permission: dict, cfg, error: str) -> PermissionDecision:
        outcome = cfg.judge.on_error if cfg.judge.on_error in ("ask", "deny", "allow") else "ask"
        if outcome == "ask" and not self.has_user_channel:
            outcome = "deny"
        return PermissionDecision(
            decision=outcome, risk="high" if outcome != "allow" else "medium",
            summary=f"自动判定不可用：{(permission or {}).get('command', '')[:80]}",
            reason=f"自动判定失败（{error}），按 {outcome} 策略处理",
            source="failure", error=error,
        )

    def _audit(self, event: str, permission: dict, extra: dict) -> None:
        from core.tools.permission import append_permission_audit
        append_permission_audit(self.session_dir, event, {
            "decision_id": (permission or {}).get("decision_id", ""),
            "tool": (permission or {}).get("tool", ""),
            "kind": (permission or {}).get("kind", "command"),
            "action": str((permission or {}).get("command", ""))[:300],
            **extra,
        })

    def _cfg(self):
        return get_permission_config(self.config)
