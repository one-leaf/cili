"""工作流领域模型：Workflow / WorkflowStep / State。

设计见 workspace/default/workflow-chain-design-2026-11-19.md。

- 工作流与步骤 id 统一用 `secrets.token_hex(4)`（8 位小写十六进制），
  与 workspace / 会话 id 同一算法。
- 步骤 `sources` 落盘存步骤 id；API/工具输入可用位置下标，由
  `resolve_sources` 统一映射。
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from core.time_utils import now_str

MAX_STEPS = 20
MAX_WORKFLOW_NAME = 50
MAX_WORKFLOW_DESC = 2000
MAX_STEP_NAME = 60
MAX_REQ = 8000
MAX_HISTORY = 20
MAX_WORKFLOW_INPUTS = 20
MAX_INPUT_VALUE = 4000
INPUT_TYPES = {"text", "textarea"}
_INPUT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

WORKFLOW_STATUSES = {"idle", "running", "completed", "failed", "stopped", "interrupted"}
STEP_STATUSES = {"pending", "running", "done", "failed", "stopped", "interrupted"}

_ID_RE = re.compile(r"^[0-9a-f]{8}$")


class WorkflowValidationError(ValueError):
    """工作流定义非法。"""


def new_short_id() -> str:
    """8 位小写十六进制 id（与 workspace / 会话 id 同算法）。"""
    return secrets.token_hex(4)


def is_short_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _text(value: Any, *, field_name: str, max_len: int, required: bool) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise WorkflowValidationError(f"{field_name}不能为空")
    if len(text) > max_len:
        raise WorkflowValidationError(f"{field_name}不能超过 {max_len} 字")
    return text


EXECUTOR_ROLES = {"worker", "lite"}


def _str_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WorkflowValidationError(f"{field_name} 必须是数组")
    if len(value) > 20:
        raise WorkflowValidationError(f"{field_name} 最多 20 项")
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > 500:
            raise WorkflowValidationError(f"{field_name} 单项不能超过 500 字")
        out.append(text)
    return out


@dataclass
class WorkflowStep:
    id: str
    name: str
    requirement: str
    sources: list[str] = field(default_factory=list)
    executor: str = "worker"
    reads: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "requirement": self.requirement,
            "sources": list(self.sources),
            "executor": self.executor,
            "reads": list(self.reads),
            "outputs": list(self.outputs),
        }


@dataclass
class WorkflowInput:
    key: str
    label: str
    type: str = "text"
    required: bool = False
    default: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "description": self.description,
        }


def _parse_inputs(value: Any) -> list[WorkflowInput]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WorkflowValidationError("inputs 必须是数组")
    if len(value) > MAX_WORKFLOW_INPUTS:
        raise WorkflowValidationError(f"输入项最多 {MAX_WORKFLOW_INPUTS} 个")
    out: list[WorkflowInput] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise WorkflowValidationError(f"输入项 {index + 1} 必须是对象")
        key = str(item.get("key") or "").strip()
        if not _INPUT_KEY_RE.match(key) or len(key) > 40:
            raise WorkflowValidationError(
                f"输入项 key 非法：{key!r}（字母/下划线开头，仅字母数字下划线，≤40 字）")
        if key in seen:
            raise WorkflowValidationError(f"输入项 key 重复：{key}")
        seen.add(key)
        label = _text(item.get("label"), field_name=f"输入项 {key} 的名称",
                      max_len=60, required=True)
        kind = str(item.get("type") or "text").strip().lower()
        if kind not in INPUT_TYPES:
            raise WorkflowValidationError(f"输入项 {key} 的 type 必须是 text 或 textarea")
        default = _text(item.get("default"), field_name=f"输入项 {key} 的默认值",
                        max_len=2000, required=False)
        description = _text(item.get("description"),
                            field_name=f"输入项 {key} 的说明",
                            max_len=500, required=False)
        raw_required = item.get("required", False)
        if not isinstance(raw_required, bool):
            raise WorkflowValidationError(f"输入项 {key} 的 required 必须是布尔值")
        out.append(WorkflowInput(key=key, label=label, type=kind,
                                 required=raw_required, default=default,
                                 description=description))
    return out


class WorkflowInputRequiredError(WorkflowValidationError):
    """缺少必填运行参数；missing 为可机读的输入项描述。"""

    def __init__(self, missing: list[dict[str, Any]]):
        self.missing = missing
        labels = "、".join(str(item.get("label") or item.get("key")) for item in missing)
        super().__init__("缺少必填运行参数：" + labels)


def validate_run_inputs(workflow: "Workflow", raw: Any) -> dict[str, str]:
    """校验并归一化一次运行的输入；未声明的 key 忽略。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise WorkflowValidationError("inputs 必须是对象")
    values: dict[str, str] = {}
    missing: list[dict[str, Any]] = []
    for spec in workflow.inputs:
        value = raw.get(spec.key, None)
        text = "" if value is None else str(value).strip()
        if len(text) > MAX_INPUT_VALUE:
            raise WorkflowValidationError(
                f"输入项「{spec.label}」不能超过 {MAX_INPUT_VALUE} 字")
        if spec.required and not text:
            missing.append(spec.to_dict())
        if text:
            values[spec.key] = text
    if missing:
        raise WorkflowInputRequiredError(missing)
    return values


@dataclass
class Workflow:
    id: str
    name: str
    description: str = ""
    inputs: list[WorkflowInput] = field(default_factory=list)
    steps: list[WorkflowStep] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    version: int = 1
    # 侧栏隐藏（与会话 hidden 同一语义）；更新工作流时未携带该字段则沿用原值
    hidden: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, workflow_id: str | None = None) -> "Workflow":
        if not isinstance(data, dict):
            raise WorkflowValidationError("工作流必须是对象")
        raw_steps = data.get("steps")
        if raw_steps is None and isinstance(data.get("subtasks"), list):
            raw_steps = data.get("subtasks")  # 旧版字段
        if not isinstance(raw_steps, list):
            raise WorkflowValidationError("steps 必须是数组")
        if not (1 <= len(raw_steps) <= MAX_STEPS):
            raise WorkflowValidationError(f"步骤数量必须在 1–{MAX_STEPS} 之间")

        name = _text(data.get("name"), field_name="工作流名称", max_len=MAX_WORKFLOW_NAME,
                     required=True)
        description = _text(data.get("description"), field_name="工作流描述",
                            max_len=MAX_WORKFLOW_DESC, required=False)
        inputs = _parse_inputs(data.get("inputs"))

        # 第一遍：归一化 id / 文本，先不解析 sources
        pending: list[tuple[WorkflowStep, list[Any]]] = []
        seen: set[str] = set()
        for item in raw_steps:
            if not isinstance(item, dict):
                raise WorkflowValidationError("步骤必须是对象")
            sid = str(item.get("id") or "").strip()
            if not sid:
                sid = new_short_id()
            if not is_short_id(sid):
                raise WorkflowValidationError(f"步骤 id 非法：{sid!r}（应为 8 位十六进制）")
            if sid in seen:
                raise WorkflowValidationError(f"步骤 id 重复：{sid}")
            seen.add(sid)
            raw_sources = item.get("sources")
            if raw_sources is None:
                raw_sources = []
            if not isinstance(raw_sources, list):
                raise WorkflowValidationError(f"步骤 {sid} 的 sources 必须是数组")
            executor = str(item.get("executor") or "worker").strip().lower()
            if executor not in EXECUTOR_ROLES:
                raise WorkflowValidationError(
                    f"步骤 {sid} 的 executor 必须是 worker 或 lite")
            pending.append((
                WorkflowStep(
                    id=sid,
                    name=_text(item.get("name"), field_name="步骤名称",
                               max_len=MAX_STEP_NAME, required=True),
                    requirement=_text(item.get("requirement"), field_name="步骤要求",
                                      max_len=MAX_REQ, required=True),
                    executor=executor,
                    reads=_str_list(item.get("reads"), field_name=f"步骤 {sid} 的 reads"),
                    outputs=_str_list(item.get("outputs"), field_name=f"步骤 {sid} 的 outputs"),
                ),
                raw_sources,
            ))

        # 第二遍：位置 → id；校验来源必须存在且在本步之前
        id_to_index = {step.id: i for i, (step, _raw) in enumerate(pending)}
        for index, (step, raw_sources) in enumerate(pending):
            resolved: list[str] = []
            for raw in raw_sources:
                if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                    raise WorkflowValidationError(
                        f"步骤「{step.name}」的 sources 只能是用位置下标或步骤 id")
                if isinstance(raw, int):
                    if not (0 <= raw < len(pending)):
                        raise WorkflowValidationError(
                            f"步骤「{step.name}」引用了不存在的位置 {raw}")
                    src_id = pending[raw][0].id
                else:
                    src_id = raw.strip()
                    if src_id not in id_to_index:
                        raise WorkflowValidationError(
                            f"步骤「{step.name}」引用了不存在的步骤 {src_id}")
                src_index = id_to_index[src_id]
                if src_index >= index:
                    raise WorkflowValidationError(
                        f"步骤「{step.name}」的来源必须排在它前面（{src_id} 在后）")
                if src_id in resolved:
                    raise WorkflowValidationError(
                        f"步骤「{step.name}」的来源重复：{src_id}")
                resolved.append(src_id)
            step.sources = resolved

        now = now_str()
        return cls(
            id=workflow_id or str(data.get("id") or new_short_id()),
            name=name,
            description=description,
            inputs=inputs,
            steps=[step for step, _raw in pending],
            created_at=str(data.get("created_at") or now),
            updated_at=str(data.get("updated_at") or now),
            version=int(data.get("version") or 1),
            hidden=bool(data.get("hidden", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "inputs": [i.to_dict() for i in self.inputs],
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "hidden": self.hidden,
        }

    def index_of(self, step_id: str) -> int:
        for i, step in enumerate(self.steps):
            if step.id == step_id:
                return i
        return -1

    def get(self, step_id: str) -> WorkflowStep | None:
        for step in self.steps:
            if step.id == step_id:
                return step
        return None


@dataclass
class StepState:
    """单个步骤的运行状态。"""

    status: str = "pending"
    session_id: str = ""
    executor: str = "worker"
    artifact: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    summary: str = ""
    error: str = ""
    truncated: bool = False
    completion_declared: bool = False
    declared_status: str = ""
    phase: str = ""
    current_tool: str = ""
    iterations: int = 0
    stats: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "StepState":
        data = data if isinstance(data, dict) else {}
        status = str(data.get("status") or "pending")
        if status not in STEP_STATUSES:
            status = "pending"
        executor = str(data.get("executor") or "worker")
        if executor not in EXECUTOR_ROLES:
            executor = "worker"
        return cls(
            status=status,
            session_id=str(data.get("session_id") or ""),
            executor=executor,
            artifact=str(data.get("artifact") or ""),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            duration_ms=int(data.get("duration_ms") or 0),
            summary=str(data.get("summary") or ""),
            error=str(data.get("error") or ""),
            truncated=bool(data.get("truncated")),
            completion_declared=bool(data.get("completion_declared")),
            declared_status=str(data.get("declared_status") or ""),
            phase=str(data.get("phase") or ""),
            current_tool=str(data.get("current_tool") or ""),
            iterations=int(data.get("iterations") or 0),
            stats=dict(data.get("stats") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "session_id": self.session_id,
            "executor": self.executor,
            "artifact": self.artifact,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "summary": self.summary,
            "error": self.error,
            "truncated": self.truncated,
            "completion_declared": self.completion_declared,
            "declared_status": self.declared_status,
            "phase": self.phase,
            "current_tool": self.current_tool,
            "iterations": self.iterations,
            "stats": dict(self.stats),
        }


@dataclass
class WorkflowState:
    status: str = "idle"
    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    current_index: int = 0
    panel_session_id: str = ""
    results_session_id: str = ""
    origin_session_id: str = ""
    run_inputs: dict[str, str] = field(default_factory=dict)
    items: dict[str, StepState] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    version: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WorkflowState":
        data = data if isinstance(data, dict) else {}
        status = str(data.get("status") or "idle")
        if status not in WORKFLOW_STATUSES:
            status = "idle"
        raw_items = data.get("items")
        items: dict[str, StepState] = {}
        if isinstance(raw_items, dict):
            for key, value in raw_items.items():
                items[str(key)] = StepState.from_dict(value)
        history = data.get("history")
        return cls(
            status=status,
            run_id=str(data.get("run_id") or ""),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            current_index=int(data.get("current_index") or 0),
            panel_session_id=str(data.get("panel_session_id") or ""),
            results_session_id=str(data.get("results_session_id") or ""),
            origin_session_id=str(data.get("origin_session_id") or ""),
            run_inputs={str(k): str(v) for k, v in (data.get("run_inputs") or {}).items()}
            if isinstance(data.get("run_inputs"), dict) else {},
            items=items,
            history=[dict(x) for x in history] if isinstance(history, list) else [],
            version=int(data.get("version") or 1),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "current_index": self.current_index,
            "panel_session_id": self.panel_session_id,
            "results_session_id": self.results_session_id,
            "origin_session_id": self.origin_session_id,
            "run_inputs": dict(self.run_inputs),
            "items": {k: v.to_dict() for k, v in self.items.items()},
            "history": [dict(x) for x in self.history],
        }

    def push_history(self, record: dict[str, Any], max_kept: int = MAX_HISTORY) -> None:
        self.history.insert(0, dict(record))
        del self.history[max_kept:]

    def reset_run(self, run_id: str) -> None:
        """开始新一次运行：清空步骤状态，保留会话 id 与历史。"""
        self.status = "running"
        self.run_id = run_id
        self.started_at = now_str()
        self.finished_at = ""
        self.current_index = 0
        self.run_inputs = {}
        self.items = {}


def resolve_step_index(workflow: "Workflow", step: Any) -> int:
    """步骤定位：0 基序号 / 步骤名 / 步骤 id。"""
    if isinstance(step, bool):
        raise WorkflowValidationError("step 必须是步骤序号或名称")
    if isinstance(step, int):
        index = step
    elif isinstance(step, str):
        text = step.strip()
        if text.isdigit():
            index = int(text)
        else:
            for i, item in enumerate(workflow.steps):
                if text in (item.name, item.id):
                    return i
            raise WorkflowValidationError(f"未找到步骤 {step!r}")
    else:
        raise WorkflowValidationError("step 必须是步骤序号或名称")
    if not (0 <= index < len(workflow.steps)):
        raise WorkflowValidationError(f"step 序号越界：{index}")
    return index
