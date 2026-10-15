"""TUI 渲染原语：SSE 帧 → 终端输出。

M1 基础版（无 rich Live）：正文帧累积、回合结束一次性 markdown 精渲染；
工具行 / 结果摘要 / 文件 / 错误即到即打。提交到 scrollback 的内容不再改动
（stable/tail 双区模型的 M1 简化——tail 区就是当前累积缓冲，M2 换 rich Live）。
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.markdown import Markdown

# 工具行参数摘要取哪些字段、按什么顺序（命中即停）
_SUMMARY_KEYS = (
    "action", "path", "file_path", "command", "pattern", "glob", "query", "url",
    "name", "skill", "task", "description",
)
_SUMMARY_MAX = 80


def summarize_tool_input(tool_input: dict | list | None) -> str:
    """工具入参 → 单行摘要（read /workspace/x.py，bash → 命令前 80 字符）。"""
    if tool_input is None or tool_input == {}:
        return ""
    if not isinstance(tool_input, dict):
        if tool_input is None:
            return ""
        text = str(tool_input)
        return text[:_SUMMARY_MAX]
    for key in _SUMMARY_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text[:_SUMMARY_MAX]
    # 兜底：短 JSON（ask_user 等大入参不值得展示，超长截断）
    try:
        text = json.dumps(tool_input, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(tool_input)
    return text[:_SUMMARY_MAX] if len(text) <= _SUMMARY_MAX * 4 else ""


def format_ask_user_card(tool_input: dict) -> str:
    """ask_user 入参 → 编号问题卡文本（渲染与答案解析共用一张编号表）。"""
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if not isinstance(questions, list) or not questions:
        return ""
    lines: list[str] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        header = q.get("header", "")
        lines.append(f"❓ [{header}] {q.get('question', '')}" if header else f"❓ {q.get('question', '')}")
        for idx, opt in enumerate(q.get("options", []) or [], start=1):
            label = opt.get("label", "") if isinstance(opt, dict) else str(opt)
            desc = opt.get("description", "") if isinstance(opt, dict) else ""
            lines.append(f"  {idx}. {label}" + (f"  — {desc}" if desc else ""))
        if q.get("multi_select"):
            lines.append("  （多选：输入编号，如 1 3）")
    lines.append("  回复编号或直接输入你的答案。")
    return "\n".join(lines)


def build_option_labels(tool_input: dict) -> list[str]:
    """扁平提取 ask_user 所有问题卡的选项 label（编号 → label 映射，QQ 同款）。"""
    labels: list[str] = []
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if isinstance(questions, list):
        for q in questions:
            if isinstance(q, dict):
                for opt in q.get("options", []) or []:
                    if isinstance(opt, dict) and opt.get("label"):
                        labels.append(str(opt["label"]))
    return labels


def resolve_answer(text: str, labels: list[str]) -> str:
    """把编号答案（"1" / "1 3" / "1,3"）映射回 label；自由文本原样返回。"""
    stripped = text.strip()
    if not labels or not stripped:
        return text
    parts = stripped.replace("，", " ").replace(",", " ").split()
    if not all(p.isdigit() and 1 <= int(p) <= len(labels) for p in parts):
        return text
    return "；".join(labels[int(p) - 1] for p in parts)


class Renderer:
    """终端输出收敛点：所有 rich 调用都经这里（M1 无 Live，M2 只改内部）。"""

    def __init__(self, console: Console | None = None, no_color: bool = False):
        self.console = console or Console(no_color=no_color)

    def submit_markdown(self, text: str) -> None:
        if text.strip():
            self.console.print(Markdown(text))

    def submit_thinking(self, text: str) -> None:
        if text.strip():
            self.console.print(f"💭 {text}", style="dim italic")

    def tool_line(self, tool: str, tool_input: Any) -> None:
        summary = summarize_tool_input(tool_input)
        suffix = f" {summary}" if summary else ""
        self.console.print(f"🔧 {tool}{suffix}", style="cyan")

    def tool_result_line(self, tool: str, content: str, is_error: bool) -> None:
        if is_error:
            first_lines = "\n".join(content.splitlines()[:10])
            more = "\n…（已截断）" if len(content.splitlines()) > 10 else ""
            self.console.print(f"✘ {tool}\n[red]{first_lines}{more}[/red]")
            return
        n = len(content.splitlines()) if content else 0
        self.console.print(f"✔ {tool}" + (f" ({n} 行)" if n else ""), style="green")

    def file_line(self, name: str, path: str, note: str) -> None:
        suffix = f" —— {note}" if note else ""
        self.console.print(f"📄 [{name}]({path}){suffix}", style="yellow")

    def error_line(self, msg: str) -> None:
        self.console.print(f"[red]✘ {msg}[/red]")

    def status_line(self, msg: str) -> None:
        self.console.print(f"{msg}", style="dim")

    def info_line(self, msg: str) -> None:
        self.console.print(msg, style="bold")

    def ask_user_card(self, tool_input: Any) -> None:
        card = format_ask_user_card(tool_input if isinstance(tool_input, dict) else {})
        if card:
            self.console.print(card, style="yellow")
