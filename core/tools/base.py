"""Tool base class and interface."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from core.compression import (
    BYTES_PER_TOKEN as _BYTES_PER_TOKEN,
)
from core.compression import (
    truncate_middle as _truncate_middle,
)
from core.compression import (
    truncate_result as _truncate_result,
)
from core.fs_utils import normalize_bash_path
from core.security.path_policy import OP_DELETE, OP_READ, PathPolicy, PathTarget
from core.tool_output import save_tool_output
from core.tools.background import BackgroundMixin
from core.tools.permission import META_KEY, permission_placeholder_text
from core.tools.result import ToolResult
from core.tools.shell import ShellMixin

# 提示注入防护（SEC-18）：UNTRUSTED_DATA_BEGIN/END 定义在 core/untrusted.py，
# 这里 import 后继续作为兼容出口（工具与测试从 core.tools.base 引用）。
# 显式 `as` 别名标记为有意 re-export，避免被 ruff F401 当作未使用导入清掉。
from core.untrusted import (
    UNTRUSTED_DATA_BEGIN as UNTRUSTED_DATA_BEGIN,
)
from core.untrusted import (
    UNTRUSTED_DATA_END as UNTRUSTED_DATA_END,
)


@runtime_checkable
class SessionHandle(Protocol):
    """工具持有的 session 引用契约。

    interactive 模式传入 SessionStore（额外具备 save()/agent_logs），autonomous 模式
    传入 SessionIdentity（仅 session_id/session_dir）。工具应只依赖下面两个属性，不要靠
    ``hasattr`` 探测能力，以免换一种载体时静默走错分支。
    """
    session_id: str
    session_dir: Path | None


class Tool(ShellMixin, BackgroundMixin):
    """Base class for all tools."""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}  # JSON Schema

    # 并发安全标志：纯读、无副作用、非流式的工具标记为 True，允许同一批内并行执行。
    # 未标记（默认 False）的工具在批内始终单线程执行（各自成单批）。
    concurrency_safe: bool = False

    # Directories to skip during file search operations
    IGNORE_DIRS: set[str] = {
        ".git", ".cili", "node_modules", "__pycache__", ".venv", "venv",
        ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
        ".next", ".nuxt", "target",
    }

    # 目录名后缀匹配（egg-info 实际叫 foo.egg-info，精确名匹配永不命中）
    IGNORE_DIR_SUFFIXES: tuple[str, ...] = (".egg-info",)

    # ── 工具输出限制 ──────────────────────────────────────────────────────
    # 默认单工具结果上限（字符数），各工具可按需覆盖
    MAX_TOOL_RESULT_SIZE_CHARS: int = 50_000

    # Bash 工具专用限制（更严格，防止命令输出撑爆上下文）
    _BASH_MAX_RESULT_SIZE_CHARS: int = 30_000
    _BASH_MAX_OUTPUT_LINES: int = 2000

    # Token 预算常量（4 字节 ≈ 1 token）
    BYTES_PER_TOKEN: int = _BYTES_PER_TOKEN

    @staticmethod
    def truncate_middle(text: str, max_tokens: int) -> str:
        """Token 预算截断（委托 core.compression）。"""
        return _truncate_middle(text, max_tokens)

    @staticmethod
    def truncate_result(text: str, max_chars: int) -> str:
        """按字符数截断工具结果（委托 core.compression）。"""
        return _truncate_result(text, max_chars)

    # ── 接口能力档案钩子 ────────────────────────────────────────────────

    def set_interface_profile(self, profile) -> None:
        """接收当前回合的接口能力档案（InterfaceProfile | None）。

        ``SessionRunner.set_turn_interface`` 在接口切换时对**全部**工具调用；
        接口感知的工具（如 send_user_file）覆写本方法按档案调整描述/行为，其余工具
        用这个空实现。``profile`` 为 None 表示驱动接口未声明。
        """
        return None

    def __init__(self, cwd: str = ".", workspace_uuid: str = "", session: SessionHandle | None = None,
                 permission_store=None, permission_rules=None, permission_policy=None,
                 **kwargs):
        """统一构造协议。

        子类若重写 ``__init__``，应显式声明所需参数并透传 ``**kwargs``，否则
        ``registry._factory`` 注入的参数（config/permission_*）会导致实例化失败。
        """
        self.cwd = os.path.abspath(cwd)
        self.workspace_uuid = workspace_uuid
        self.session = session  # For accessing session info (e.g., in python tool)
        # 权限存储 / 规则 / 裁决（根/子代理共享同一实例，由 runner 注入）
        self.permission_store = permission_store
        self.permission_rules = permission_rules
        self.permission_policy = permission_policy
        # 工具输出文件路径：由 agent 在 execute() 前设置
        # _run_bash() 逐行写入此文件（实时流式），前端可轮询读取
        # save_output_to_file() 兜底确保所有工具输出都落盘
        self.output_file: str | None = None
        # 工具实时输出钩子（由 agent 在 execute() 前设置）：cb(chunk, written_bytes)
        # chunk 为增量片段，written_bytes 为写入后的累计字节偏移（与 /stream 端点一致）
        self.on_output: Callable[[str, int], None] | None = None
        # 同实例执行锁：并行批中同一工具实例的多次调用也串行化，
        # 防止 output_file/on_output 两个实例属性互相覆盖产生竞态。
        self._exec_lock = threading.Lock()

    def _resolve_path(self, path: str) -> str:
        """Resolve a file path to absolute, relative to cwd (纯解析器).

        Uses realpath（解析 `..`、符号链接与 junction）。写/删是否越界由
        各工具的 _path_gate 统一判定（PathPolicy），这里不再抛边界错误。

        自动处理 bash 风格路径（/c/... -> C:/...），LLM 从 shell 输出复制路径时
        常遇到这种格式。
        """
        path = normalize_bash_path(path)
        if not os.path.isabs(path):
            path = os.path.join(self.cwd, path)
        return os.path.realpath(path)

    def _workflow_definition_guard(self, resolved: str) -> ToolResult | None:
        """禁止直接用 write/edit 改工作流定义（实现在 core.workflows.guards）。"""
        from core.workflows.guards import workflow_definition_violation

        message = workflow_definition_violation(self.cwd, resolved)
        return ToolResult(message, error=True) if message else None

    def _permission_policy(self):
        """取本次注入的裁决器；独立工具场景构造默认策略。"""
        if self.permission_policy is None:
            from core.permission_settings import PermissionConfig
            from core.security.permission_policy import PermissionPolicy

            self.permission_policy = PermissionPolicy(
                PermissionConfig(), self.permission_rules, self.permission_store,
                workspace_root=self.cwd, cwd=self.cwd,
            )
        return self.permission_policy

    def _permission_has_channel(self) -> bool:
        """是否有用户审批通道（共享 PermissionStore）。独立工具 fail-closed。"""
        return self.permission_store is not None

    def _path_policy(self) -> PathPolicy:
        """构造本工具工作区（= cwd）的路径权限判定器。

        各工具 cwd 即其 workspace 目录；子代理继承 master 的 cwd，
        因此 master/worker/lite 的 workspace 边界天然统一。
        """
        return PathPolicy(
            workspace_root=self.cwd, cwd=self.cwd,
            permission_policy=self._permission_policy(),
            permission_store=self.permission_store,
            permission_rules=self.permission_rules,
        )

    def _path_gate(self, targets: list) -> ToolResult | None:
        """统一写/删路径审批门。返回 None 表示放行；否则返回拒绝/占位结果。

        - 任一目标越界且无审批通道（store 与 rules 均为 None）→ 硬拒绝（fail-closed）
        - 否则按序返回第一个未批准越界目标的审批占位符（pending 单槽，一次一卡），
          占位符携带全部目标的判定证据（供 lite 判定与审计）
        - 已批准目标（会话级或命中规则）自动放行
        """
        if not targets:
            return None
        policy = self._path_policy()
        denied = policy.any_denied(targets)
        if denied is not None:
            verb = "删除" if denied.op == OP_DELETE else "写入"
            return ToolResult(
                f"Error: {verb}目标不在工作区 {policy.workspace_root!r} 内，且无审批通道：{denied.raw}",
                error=True,
            )
        pending = policy.first_pending(targets, tool=self.name)
        if pending is not None:
            approval = policy.permission_meta(pending, tool=self.name)
            approval["targets"] = [policy.target_evidence(t) for t in targets]
            approval["findings"] = []
            return ToolResult(
                permission_placeholder_text(approval),
                completed=False,
                meta={META_KEY: approval},
            )
        return None

    def _read_gate(self, paths: list[str]) -> ToolResult | None:
        """统一读侧门：工作区外读取按 outside.read 裁决（allow/ai/ask/deny）。"""
        if not paths:
            return None
        for path in paths:
            teen_block = self._teen_read_guard(path)
            if teen_block is not None:
                return teen_block
        targets = [
            PathTarget(OP_READ, p, "read", resolved=p)
            for p in paths
        ]
        policy = self._path_policy()

        def _status(target: PathTarget) -> str:
            return policy.read_status(target.resolved, tool=self.name, raw=target.raw)

        denied = next((t for t in targets if _status(t) == "deny"), None)
        if denied is not None:
            return ToolResult(
                f"Error: 读取目标不在工作区 {policy.workspace_root!r} 内，且无审批通道：{denied.raw}",
                error=True,
            )
        pending = next((t for t in targets if _status(t) == "needs_approval"), None)
        if pending is not None:
            approval = policy.permission_meta(pending, tool=self.name)
            approval["targets"] = [policy.target_evidence(t) for t in targets]
            approval["findings"] = []
            return ToolResult(
                permission_placeholder_text(approval),
                completed=False,
                meta={META_KEY: approval},
            )
        return None

    def _pending_permission(self, command: str, decision_id: str, targets: list, findings: list,
                            kind: str = "command",
                            reason: str = "未命中允许规则，需要权限判定",
                            mode: str = "ai", rule: dict | None = None) -> dict:
        """构造判定占位 meta：证据齐全（目标 / 静态发现），供判定/审批/审计。"""
        policy = self._path_policy()
        return {
            "decision_id": decision_id,
            "command": command,
            "kind": kind,
            "tool": self.name,
            "reason": reason,
            "targets": [policy.target_evidence(t) for t in targets],
            "findings": findings,
            "mode": mode,
            "verdict": mode,
            "rule": rule,
        }

    def _teen_command_guard(self, command: str) -> ToolResult | None:
        """青少年模式：回退类命令硬拒（实现在 core.teen_mode）。"""
        from core.teen_mode import guard_tool_command

        message = guard_tool_command(self.name, command)
        return ToolResult(message, error=True) if message else None

    def _teen_read_guard(self, resolved: str) -> ToolResult | None:
        """青少年模式读侧门（实现在 core.teen_mode）。"""
        from core.teen_mode import guard_tool_read

        message = guard_tool_read(resolved, is_inside=self._path_policy().is_inside)
        return ToolResult(message, error=True) if message else None

    def _emit_output(self, chunk: str, written_bytes: int) -> None:
        """调用 on_output 钩子，异常不影响工具执行（钩子失败静默忽略）。"""
        cb = self.on_output
        if cb is None:
            return
        try:
            cb(chunk, written_bytes)
        except Exception:
            pass

    def save_output_to_file(self, result: ToolResult, output_file: str | None = None) -> str | None:
        """统一保存工具输出到外部文件（实现在 core/tool_output.py）。"""
        explicit = output_file
        saved = save_tool_output(result, explicit or self.output_file)
        if saved and not explicit:
            self.output_file = saved
        return saved

    def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with given parameters. Override in subclass."""
        raise NotImplementedError

    def to_schema(self) -> dict[str, Any]:
        """Convert to Anthropic tool schema format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def validate_input(self, kwargs: dict[str, Any]) -> ToolResult | None:
        """校验 LLM 传入的参数是否符合 parameters schema。

        检查项：
        - required 字段是否存在
        - enum 值是否合法
        - 基本类型约束（string/integer/number/boolean/array/object）

        注意：此方法应在 coerce_input 完成类型转换后调用。

        Returns:
            None: 校验通过
            ToolResult(error=True): 校验失败，错误信息已格式化
        """
        if not self.parameters:
            return None

        props = self.parameters.get("properties", {})
        required = set(self.parameters.get("required", []))

        # 1. 检查必填字段
        missing = required - set(kwargs.keys())
        if missing:
            return ToolResult(
                f"Error: 缺少必填参数: {', '.join(sorted(missing))}",
                error=True,
            )

        # 2. 逐字段校验（只校验 LLM 实际传了的字段）
        for key, value in kwargs.items():
            if key not in props:
                continue
            prop_schema = props[key]
            prop_type = prop_schema.get("type", "")

            # enum 校验
            if "enum" in prop_schema and value not in prop_schema["enum"]:
                allowed = prop_schema["enum"]
                return ToolResult(
                    f"Error: 参数 '{key}' 值无效。允许值: {allowed}",
                    error=True,
                )

            # 类型校验（跳过 None 值，让默认值生效）
            if value is None:
                continue

            if prop_type == "string" and not isinstance(value, str):
                return ToolResult(
                    f"Error: 参数 '{key}' 应为 string 类型，实际为 {type(value).__name__}",
                    error=True,
                )
            # bool 是 int 的子类，须显式排除，否则 true/false 会被当作合法 integer
            elif prop_type in ("integer", "number") and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                return ToolResult(
                    f"Error: 参数 '{key}' 应为 {prop_type} 类型，实际为 {type(value).__name__}",
                    error=True,
                )
            elif prop_type == "boolean" and not isinstance(value, bool):
                return ToolResult(
                    f"Error: 参数 '{key}' 应为 boolean 类型，实际为 {type(value).__name__}",
                    error=True,
                )
            elif prop_type == "array" and not isinstance(value, list):
                return ToolResult(
                    f"Error: 参数 '{key}' 应为 array 类型，实际为 {type(value).__name__}",
                    error=True,
                )
            elif prop_type == "object" and not isinstance(value, dict):
                return ToolResult(
                    f"Error: 参数 '{key}' 应为 object 类型，实际为 {type(value).__name__}",
                    error=True,
                )

        return None

    def coerce_input(self, kwargs: dict[str, Any]) -> dict[str, Any] | ToolResult:
        """根据 parameters schema 校验并修正 LLM 传入的参数。

        流程：
        1. 类型转换 — integer/boolean 等类型修正（LLM 常将数字传为字符串）
        2. validate_input() — 参数合法性校验

        Returns:
            dict: 校验并转换后的参数
            ToolResult(error=True): 校验失败
        """
        if not self.parameters:
            return kwargs

        # 第一步：类型转换（在 validate_input 之前，因为 LLM 常把数字传为字符串）
        props = self.parameters.get("properties", {})
        required = set(self.parameters.get("required", []))
        coerced = {}

        for key, value in (kwargs or {}).items():
            prop_schema = props.get(key, {})
            prop_type = prop_schema.get("type", "")

            # 数值类型转换
            if prop_type in ("integer", "number") and isinstance(value, str):
                try:
                    value = int(value) if prop_type == "integer" else float(value)
                except (ValueError, TypeError):
                    pass  # 无法转换则保留原值，让 validate_input 报错

            # 布尔类型转换（"true"/"false" 字符串 → bool）
            elif prop_type == "boolean" and isinstance(value, str):
                if value.lower() == "true":
                    value = True
                elif value.lower() == "false":
                    value = False

            # 跳过缺失的可选参数（让默认值生效）
            if value is None and key not in required:
                continue

            coerced[key] = value

        # 第二步：过滤未知参数——LLM 常把其他工具的参数（如 bash 的 timeout）
        # 张冠李戴，直接透传会让 execute(**kwargs) 抛 TypeError。
        # 为提高容错性，静默忽略未知参数，仅保留合法参数。
        valid = set(props.keys())
        coerced = {k: v for k, v in coerced.items() if k in valid}

        # 第三步：参数校验（在类型转换之后）
        validation_error = self.validate_input(coerced)
        if validation_error is not None:
            return validation_error

        return coerced

