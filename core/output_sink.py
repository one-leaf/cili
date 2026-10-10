"""接口无关的输出接收端（OutputSink）。

SessionRunner 不关心输出发往何处；各接入端（Web SSE / QQ / ...）各自提供一个
OutputSink 实现，把「接口绑定」集中到一处，取代原先散落的多个回调属性。

生命周期分两层：
- ``runner.default_sink``：随 runner 创建设定（如工具实时输出 → 全局事件总线），
  也用于后台子代理完成后的自动恢复循环。
- ``run(sink=...)`` / ``resume_after_ask_user(sink=...)``：本次运行的活动 sink。
  未传时回退到 ``default_sink``。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Callable


def _noop(*_args, **_kwargs) -> None:
    """默认空实现：未绑定的回调静默忽略。"""
    return None


@dataclass
class OutputSink:
    """一次 run/resume 的全部输出回调打包。

    字段签名与 SessionRunner 的回调一致；未提供的字段使用 no-op。
    工具实时输出增量 ``on_tool_output`` 参数为 (tool_name, content, written_bytes, tool_use_id)。
    """
    on_text: Callable[[str], None] = _noop
    on_thinking: Callable[[str], None] = _noop
    on_tool_call: Callable[[str, dict, str], None] = _noop
    on_tool_result: Callable[[str, str, bool, str], None] = _noop
    # None 表示「不挂载流式输出回调」——工具据此决定是否推送增量（on_output 为流式开关），
    # 故此处保留 None 语义，不能像其他字段那样默认 no-op。
    on_tool_output: Callable[[str, str, int, str], None] | None = None
    on_session_start: Callable[[str, str], None] = _noop
    on_session_complete: Callable[[str], None] = _noop
    # 一次 interactive 回合（run / resume）结束、会话已落盘后调用。
    # 接入端据此重拉会话状态：请求级流断开（刷新页面）后，界面靠这条信号自愈。
    on_turn_complete: Callable[[], None] = _noop


def merge_sink(base: OutputSink, **overrides) -> OutputSink:
    """以 base 为底，用「已显式设置」的 overrides 覆盖，返回新 OutputSink。

    未设置 = None（如 on_tool_output）或默认 no-op（如未绑定的 on_text），
    这类字段不覆盖 base——用于「接口只覆盖部分回调、其余继承持久 sink」的场景。
    """
    clean = {k: v for k, v in overrides.items() if v is not None and v is not _noop}
    return replace(base, **clean) if clean else base


def layer_sink(base: OutputSink, override: OutputSink | None) -> OutputSink:
    """以 base 为底、override 中已显式设置的字段覆盖。override 为 None 时返回 base。"""
    if override is None:
        return base
    return merge_sink(base, **{f.name: getattr(override, f.name) for f in fields(override)})
