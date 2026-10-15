"""终端（TUI/CLI）接入端 —— 样例占位包。

尚未实现传输层。接入落地时的清单（能力档案部分已就绪，其余照 QQ/微信
inbound 的形态做）：

1. 实现传输层（stdin/stdout 或 SSH），在回合前调
   ``runner.set_turn_interface("tui")``、回合结束在 ``finally`` 复位为 None；
2. 实现 OutputSink（渲染到终端）；
3. import 本包的 ``profile``（import 副作用完成能力档案注册）；
4. ``Interface.profile = "tui"``（若按 core.interface.Interface 子类化）。
"""
