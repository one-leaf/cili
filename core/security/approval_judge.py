"""lite 安全判定器（设计 §3.3）：规则未命中的动作做一次结构化 LLM 判定。

调用形态：一次性 ``chat_structured``（复用 memory_pipeline 的 lite 先例），不起子 agent。

- **超时**：调用放线程 + ``Thread.join(timeout)``，超时按失败策略处理（工作线程
  daemon 化自行收尾，主流程不被阻塞）；
- **失败策略** ``judge_on_error``：ask（默认）/ deny / allow；无询问通道时 ask 退化为 deny；
- **连错降级**：连续失败 N 次（默认 3）后本会话停用判定，直接走失败策略
  （先例：Claude-Code auto 分类器）；
- **高置信门槛**：缺字段 / 枚举外取值按失败处理，提示词要求低置信输出 ask；
- **会话缓存**：decision_id → 结论，同命令重发不再二次判定；
- **allow 自动落盘**：judge 只产出结论，落盘由调用方（Loop）经
  ``approval_rules.build_granted_rule`` 管道执行（BANNED_PREFIXES / DESTRUCTIVE_HEADS /
  risk=high 不落盘三重约束在那里生效）；
- **审计**：每次判定写 asked / decided 成对事件到 ``{session_dir}/approval_audit.jsonl``。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field

from core.config import ApprovalConfig, get_approval_config
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
            "description": "建议落盘的规则粒度（可省略）",
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

_SYSTEM_PROMPT = """你是 Cili Agent 的命令安全判定器。对给定的「待判定动作」输出结构化结论，不执行任何动作。

输入字段说明：
- action：待判定的命令 / Python 代码 / URL 原文；
- extracted_targets：静态分析提取到的写/删目标（op=write/delete；inside_workspace 表示目标是否在工作区内）；
- static_findings：静态规则扫描的命中证据（id 与 constraints 对应）；
- constraints：安全约束清单（severity=deny 为架构性约束；severity=warn 为需要用户确认的场景）；
- recent_approvals：本会话已放行的动作；
- has_user_channel：当前是否存在可询问用户的通道（false 时对边界情况倾向 deny）。

判定准则：
1. deny：违反 severity=deny 约束、明确恶意、或破坏性极大概率不可逆的动作；
2. ask：有副作用但范围或目的不明确、越界写删、破坏性系统操作、陌生命令形态；
3. allow：目标明确、影响范围有限、与 recent_approvals 一致的常规操作；
4. 不确定或证据不足时输出 ask，**不允许低置信 allow**；
5. severity=deny 的约束应拒绝，除非该命令明显无副作用（如 --version / --help 查询）。

输出要求：
- summary / reason / alternative 一律中文；summary 是一句话说明动作做什么、影响什么（会展示给用户）；
- 相同输入必须给出一致结论；
- suggested_rule：建议落盘的规则粒度，type ∈ exact/prefix/argv_glob，value 为匹配值
  （argv_glob 为字符串数组）。prefix 的首个 token 不得是 bash/sh/sudo/env/python/pwsh/cmd/wsl
  等入口词，也不得是 rm/dd/format 等破坏性动词；拿不准就省略该字段。"""


@dataclass
class ApprovalDecision:
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


class ApprovalJudge:
    """一次判定 = 输入组装 + 一次结构化 LLM 调用 + 校验/缓存/降级。

    实例随 runner 存活（interactive master 与 autonomous 子代理各一份），持有
    会话级缓存与连错计数；子代理与主代理共享 rules/store 实例但缓存/计数独立。
    """

    def __init__(self, config, rules, store=None, *, role: str = "", has_user_channel: bool = True,
                 workspace_root: str = "", cwd: str = "", session_dir=None) -> None:
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

    def decide(self, approval: dict) -> ApprovalDecision:
        cfg = self._cfg()
        did = str((approval or {}).get("decision_id") or "")
        if not cfg.enabled or not cfg.judge_enabled:
            return self._failure_decision(approval, cfg, "自动判定未启用")
        if did and did in self._cache:
            cached = dict(self._cache[did])
            cached["source"] = "cache"
            return ApprovalDecision(**cached)

        self._audit("asked", approval, {"source": "judge"})
        if self._consecutive_failures >= max(1, cfg.judge_max_consecutive_failures):
            decision = self._failure_decision(
                approval, cfg, f"判定连续失败 {self._consecutive_failures} 次，本会话已停用自动判定")
            self._audit("decided", approval, {"decision": decision.decision, "source": "failure"})
            return decision

        started = time.perf_counter()
        try:
            raw, usage = self._invoke(self._build_input(approval), cfg)
            decision = self._validate(raw)
        except Exception as e:  # noqa: BLE001 —— 任何判定失败都走失败策略
            logger.warning(f"[approval-judge] 判定失败：{e}")
            self._consecutive_failures += 1
            decision = self._failure_decision(approval, cfg, str(e))
        else:
            self._consecutive_failures = 0
            decision.usage = usage
            decision.summary = decision.summary or approval.get("command", "")[:120]
            if cfg.judge_cache == "session" and did:
                self._cache[did] = {
                    "decision": decision.decision, "risk": decision.risk, "summary": decision.summary,
                    "reason": decision.reason, "alternative": decision.alternative,
                    "suggested_rule": decision.suggested_rule, "source": "judge", "error": "",
                }
        decision.duration_ms = int((time.perf_counter() - started) * 1000)
        self._audit("decided", approval, {
            "decision": decision.decision, "risk": decision.risk, "summary": decision.summary[:120],
            "source": decision.source, "duration_ms": getattr(decision, "duration_ms", None),
        })
        return decision

    # ── 输入 / 调用 / 校验 ────────────────────────────────────────

    def _build_input(self, approval: dict) -> list[Message]:
        payload = {
            "tool": approval.get("tool", ""),
            "kind": approval.get("kind", "command"),
            "action": (approval.get("command") or "")[:8000],
            "cwd": self.cwd,
            "workspace_root": self.workspace_root,
            "role": self.role,
            "has_user_channel": self.has_user_channel,
            "extracted_targets": approval.get("targets") or [],
            "static_findings": approval.get("findings") or [],
            "constraints": self.rules.constraints() if self.rules else [],
            "recent_approvals": (self.store.approved_commands()[:10] if self.store else []),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        return [Message(role="user", content=f"待判定动作：\n{text}")]

    def _invoke(self, messages: list[Message], cfg: ApprovalConfig) -> tuple[dict, dict | None]:
        result: dict = {}

        def _work() -> None:
            client = create_llm_client(self.config.model_for_role("lite"))
            try:
                max_tokens = _JUDGE_MAX_TOKENS
                model_cfg = getattr(client, "config", None)
                cap = getattr(model_cfg, "max_output_tokens", None)
                if isinstance(cap, int) and cap > 0:
                    max_tokens = min(max_tokens, cap)
                resp = client.chat_structured(
                    messages, system=_SYSTEM_PROMPT, output_schema=_OUTPUT_SCHEMA, max_tokens=max_tokens)
                result["resp"] = resp
            finally:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

        thread = threading.Thread(target=_work, daemon=True)
        thread.start()
        thread.join(timeout=max(5, cfg.judge_timeout_seconds))
        if thread.is_alive():
            raise TimeoutError(f"判定超时（{cfg.judge_timeout_seconds}s）")
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
    def _validate(raw: dict) -> ApprovalDecision:
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
        return ApprovalDecision(
            decision=decision, risk=risk,
            summary=str(raw.get("summary") or "").strip(),
            reason=str(raw.get("reason") or "").strip(),
            alternative=str(raw.get("alternative") or "").strip(),
            suggested_rule=suggested, source="judge",
        )

    # ── 失败策略 / 审计 ───────────────────────────────────────────

    def _failure_decision(self, approval: dict, cfg: ApprovalConfig, error: str) -> ApprovalDecision:
        """失败策略（§3.3.4）：ask 默认，无通道时退化为 deny；failure-allow 不落盘。"""
        outcome = cfg.judge_on_error if cfg.judge_on_error in ("ask", "deny", "allow") else "ask"
        if outcome == "ask" and not self.has_user_channel:
            outcome = "deny"
        return ApprovalDecision(
            decision=outcome, risk="high" if outcome != "allow" else "medium",
            summary=f"自动判定不可用：{(approval or {}).get('command', '')[:80]}",
            reason=f"自动判定失败（{error}），按 {outcome} 策略处理",
            source="failure", error=error,
        )

    def _audit(self, event: str, approval: dict, extra: dict) -> None:
        from core.tools.approval import append_approval_audit
        append_approval_audit(self.session_dir, event, {
            "decision_id": (approval or {}).get("decision_id", ""),
            "tool": (approval or {}).get("tool", ""),
            "kind": (approval or {}).get("kind", "command"),
            "action": str((approval or {}).get("command", ""))[:300],
            **extra,
        })

    def _cfg(self) -> ApprovalConfig:
        return get_approval_config(self.config)
