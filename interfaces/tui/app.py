"""TuiApp：输入循环 + 回合驱动（M1 同步版，prompt_toolkit 常驻输入框是 M3）。

分时模型：等待输入时无渲染，回合期间只消费 SSE 帧渲染。回合中 Ctrl+C →
``POST /stop``（runner 在下一步中断），继续收完流；3 秒内第二次 Ctrl+C 强制
断开连接（服务端照常收尾落盘）。
"""

from __future__ import annotations

import os
import time

from interfaces.tui.client import CiliClient, CiliServerError
from interfaces.tui.commands import build_commands
from interfaces.tui.render import Renderer, resolve_answer
from interfaces.tui.sink import SseFrameRouter

_STOP_FORCE_WINDOW = 3.0  # 秒：两次 Ctrl+C 视为强制断开


class TuiApp:
    def __init__(
        self,
        client: CiliClient,
        renderer: Renderer,
        *,
        continue_last: bool = False,
        session_id: str | None = None,
    ):
        self.client = client
        self.renderer = renderer
        self.router = SseFrameRouter(renderer)
        self.continue_last = continue_last
        self.initial_session_id = session_id
        self.quit_requested = False
        self.workspace_uuid = ""
        self.workspace_name = ""
        self.session_id = ""
        self.session_name = ""
        self.commands = build_commands(self)
        self._last_stop_request = 0.0

    # ---------- 生命周期 ----------

    def run(self) -> int:
        try:
            ws = self.client.resolve_workspace(os.getcwd())
        except CiliServerError as e:
            self.renderer.error_line(str(e))
            return 2
        self.workspace_uuid = ws.get("uuid", "")
        self.workspace_name = ws.get("name", "")
        if not self._setup_session():
            return 2

        self.renderer.info_line(f"Cili TUI — workspace: {self.workspace_name}")
        self.renderer.status_line(
            f"会话 {self.session_name}（{self.session_id}）；/h 查看命令，Ctrl+D 退出"
        )

        while not self.quit_requested:
            try:
                prompt = f"\n{self.workspace_name} › "
                line = input(prompt)
            except EOFError:
                self.renderer.status_line("")
                break
            except KeyboardInterrupt:
                self.renderer.status_line("")
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if not self._dispatch_command(line):
                    continue
            content = line
            if self.router.ask_labels:
                # ask_user 问题卡待答：编号答案映射回 label（QQ 同款），自由文本原样。
                # 服务端 prepare_turn 会把这条消息判为 resume 注入，不追加新 user message。
                content = resolve_answer(line, self.router.ask_labels)
                self.router.ask_labels = []
            try:
                self.run_turn(content)
            except CiliServerError as e:
                self.renderer.error_line(str(e))
        return 0

    def request_quit(self) -> None:
        self.quit_requested = True

    # ---------- 会话 ----------

    def _setup_session(self) -> bool:
        """按 -c / --session / 默认新建 三个来源确定当前会话。"""
        try:
            if self.initial_session_id:
                self._adopt(self.initial_session_id, "(指定会话)")
                return True
            sessions = self.client.list_sessions(self.workspace_uuid)
            if self.continue_last and sessions:
                first = sessions[0]
                self._adopt(first.get("session_id", ""), first.get("name", "Unnamed"))
                return True
            session = self.client.create_session(self.workspace_uuid)
            self.set_session(session)
            return True
        except CiliServerError as e:
            self.renderer.error_line(str(e))
            return False

    def _adopt(self, session_id: str, name: str) -> None:
        if not session_id:
            raise CiliServerError("会话 id 为空")
        self.session_id = session_id
        self.session_name = name or session_id

    def set_session(self, session: dict) -> None:
        """切换/新建会话（/new、/s 共用）。"""
        self._adopt(
            session.get("session_id", ""),
            session.get("name", "") or "Unnamed",
        )
        self.renderer.status_line(f"会话 → {self.session_name}（{self.session_id}）")

    def switch_session_by_index(self, index: int) -> None:
        sessions = self.client.list_sessions(self.workspace_uuid)
        if not 1 <= index <= len(sessions):
            self.renderer.error_line(f"序号超出范围（1-{len(sessions)}）")
            return
        s = sessions[index - 1]
        self.set_session({"session_id": s.get("session_id", ""), "name": s.get("name", "")})

    # ---------- 命令 ----------

    def _dispatch_command(self, line: str) -> bool:
        """返回 True 表示回合照常发送（/status 等透传服务端）；False 表示已处理。"""
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/status":
            return True  # 服务端识别为文本命令，走回合流渲染 usage 报告

        if cmd in ("/s", "/session"):
            if arg.isdigit():
                self.switch_session_by_index(int(arg))
            else:
                self.commands["s"].handler()
            return False

        command = self.commands.get(cmd.lstrip("/"))
        if command is None:
            self.renderer.error_line(f"未知命令 {cmd}（/h 查看帮助）")
            return False
        command.handler()
        return False

    # ---------- 回合 ----------

    def run_turn(self, content: str) -> None:
        """驱动一个回合。Ctrl+C 语义：首次 = POST /stop 后继续消费同一流直到
        收尾（生成器引用不能丢——丢了对端 HTTP 连接就断了，剩余帧收不到）；
        窗口期内二次 = close 生成器强制断开，服务端照常收尾落盘。"""
        frames = self.client.chat_stream(self.workspace_uuid, self.session_id, content)
        stop_requested = False
        try:
            while True:
                try:
                    frame = next(frames)
                except StopIteration:
                    break
                except KeyboardInterrupt:
                    if stop_requested and (
                        time.monotonic() - self._last_stop_request
                    ) < _STOP_FORCE_WINDOW:
                        self.renderer.status_line("强制断开（服务端照常收尾落盘）")
                        frames.close()
                        break
                    self._last_stop_request = time.monotonic()
                    stop_requested = True
                    self.renderer.status_line(
                        "已请求停止，等待回合收尾…（3 秒内再按 Ctrl+C 强制断开）"
                    )
                    self.stop_turn()
                    continue
                self.router.handle(frame)
                if frame.get("type") == "done":
                    break
        finally:
            frames.close()
            self.router.finish()

    def stop_turn(self) -> None:
        try:
            result = self.client.stop(self.workspace_uuid, self.session_id)
            message = result.get("message", "")
            if message:
                self.renderer.status_line(message)
        except CiliServerError as e:
            self.renderer.error_line(str(e))
