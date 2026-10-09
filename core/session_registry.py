"""会话注册表：接口无关的 SessionRunner 池 + 运行认领 + Attachment 记录。

从 ``web/deps.py`` 下沉，使各接入端（Web / QQ / ...）共享同一份 runner 池与
会话运行权认领逻辑。原先这些状态被 web 独占，第二个接入端只能反向依赖 web
或复制一份池子（导致会话无法真正共享）。

生命周期：当前仍以 LRU 淘汰为主（``evict_idle``），Attachment 仅作记录；
待接入端显式 attach/detach 后（D 阶段）再切换为引用计数驱动。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from core.config import load_config
from core.output_sink import OutputSink
from core.session import SessionStore
from core.session_runner import SessionRunner

logger = logging.getLogger(__name__)

DEFAULT_MAX_RUNNERS = 20


def session_key(workspace_uuid: str, session_id: str) -> str:
    """会话键的单一构造入口（原散落在 web 各处各自拼装 ``f"{ws}:{sid}"``）。"""
    return f"{workspace_uuid}:{session_id}"


@dataclass
class Attachment:
    """一个接入端对某会话的附着记录（供多接口共享会话时做输出路由/生命周期）。"""
    attachment_id: str
    interface: str
    session_key: str


@dataclass
class HostedSession:
    """注册表托管的一个会话：runner 实例 + 附着的接入端集合。"""
    key: str
    runner: SessionRunner
    attachments: dict[str, Attachment] = field(default_factory=dict)


class SessionRegistry:
    """master runner 池：懒创建、LRU 淘汰、运行权认领、接口附着。"""

    def __init__(self, max_runners: int = DEFAULT_MAX_RUNNERS):
        self.max_runners = max_runners
        self._hosted: dict[str, HostedSession] = {}
        self._access: dict[str, float] = {}
        # 执行中认领（防 send_message / answer_ask_user 并发双循环改写 messages）
        self._claims: set[str] = set()
        self._claims_lock = threading.Lock()
        # 接入端对注册表做多步操作（如遍历重载配置、删除工作区）时持此锁
        self.lock = asyncio.Lock()
        self._attachment_index: dict[str, str] = {}  # attachment_id -> session_key

    # ---------- 查询 ----------

    def get(self, key: str) -> SessionRunner | None:
        hosted = self._hosted.get(key)
        return hosted.runner if hosted else None

    def hosted(self, key: str) -> HostedSession | None:
        return self._hosted.get(key)

    def keys(self) -> list[str]:
        return list(self._hosted.keys())

    def items(self) -> list[tuple[str, SessionRunner]]:
        return [(k, h.runner) for k, h in self._hosted.items()]

    def values(self) -> list[SessionRunner]:
        return [h.runner for h in self._hosted.values()]

    def __len__(self) -> int:
        return len(self._hosted)

    def __contains__(self, key: str) -> bool:
        return key in self._hosted

    # ---------- 生命周期 ----------

    async def get_or_create(
        self,
        workspace_uuid: str,
        session_id: str,
        workspace_dir: str,
        *,
        on_create: Callable[[SessionRunner], None] | None = None,
    ) -> SessionRunner:
        """取回或懒创建 runner。

        ``on_create`` 在新建 runner 时回调一次（接入端用于绑定 default_sink 等）。
        调用方负责 workspace 存在性校验（接口相关，不在此层）。
        """
        key = session_key(workspace_uuid, session_id)
        async with self.lock:
            self._access[key] = time.time()
            if key not in self._hosted:
                self.evict_idle()
                config = load_config()
                runner = SessionRunner(
                    config, role="master", cwd=workspace_dir, workspace_uuid=workspace_uuid
                )
                self._attach_requested_session(runner, session_id)
                if on_create is not None:
                    on_create(runner)
                self._register_mailbox(runner, session_id)
                self._hosted[key] = HostedSession(key=key, runner=runner)
            return self._hosted[key].runner

    def _attach_requested_session(self, runner: SessionRunner, session_id: str) -> None:
        """把 runner 从默认会话切到请求的 session_id（已存在则加载，否则新建）。"""
        if session_id == runner.current_session_id:
            return
        session_dir = runner.sessions_dir / session_id
        if (session_dir / "index.json").exists():
            runner.switch_session(session_id)
            logger.info(f"Loaded existing session: {session_id}")
            return
        # 新会话：为请求的 id 直接新建 SessionStore 并立即落盘，
        # 避免默认会话 index.json 不迁移、旧目录 rmdir 静默失败残留（W15）
        old_session_dir = runner.session.session_dir
        new_store = SessionStore(session_id, runner.sessions_dir)
        new_store.name = f"Session {session_id[:8]}"
        new_store.save(force=True)  # 新会话首次落盘：跳过脏标记短路
        runner.attach_session(new_store)
        # 删除空的旧默认会话目录，避免孤立目录（仅当目录确实为空时）
        if old_session_dir.exists() and old_session_dir != new_store.session_dir:
            try:
                if not any(old_session_dir.iterdir()):
                    old_session_dir.rmdir()
            except OSError:
                pass
        logger.info(f"Creating new session: {session_id}")

    def _register_mailbox(self, runner: SessionRunner, session_id: str) -> None:
        """注册会话到 AgentMailbox（支持跨会话消息传递）。"""
        try:
            from core.agent_mailbox import get_agent_mailbox
            mbus = get_agent_mailbox()
            mbus.register_session(session_id, runner.session.name)
            mbus.register_agent(session_id, session_id)
        except Exception as e:
            logger.warning(f"Failed to register session with AgentMailbox: {e}")

    def evict_idle(self) -> None:
        """超过上限时淘汰最久未访问的非运行中 runner。"""
        if len(self._hosted) <= self.max_runners:
            return
        idle_keys = [k for k, h in self._hosted.items() if not h.runner.is_running()]
        if not idle_keys:
            return
        oldest = min(idle_keys, key=lambda k: self._access.get(k, 0))
        logger.info(f"[master runner LRU] 淘汰闲置 master runner: {oldest}")
        hosted = self._hosted.pop(oldest)
        self._access.pop(oldest, None)
        for aid in list(hosted.attachments):
            self._attachment_index.pop(aid, None)
        try:
            hosted.runner.cleanup()
        except Exception as e:
            logger.warning(f"[master runner LRU] 清理被淘汰的 master runner 失败: {e}")

    def remove(self, key: str) -> SessionRunner | None:
        """移除并清理单个会话（供删除会话使用）。调用方持有 _lock。"""
        hosted = self._hosted.pop(key, None)
        self._access.pop(key, None)
        if hosted is None:
            return None
        for aid in list(hosted.attachments):
            self._attachment_index.pop(aid, None)
        try:
            hosted.runner.cleanup()
        except Exception as e:
            logger.warning(f"[SessionRegistry] 清理 runner {key} 失败: {e}")
        return hosted.runner

    def remove_workspace(self, workspace_uuid: str) -> None:
        """移除某工作区的全部会话（供删除/重置工作区使用）。"""
        prefix = f"{workspace_uuid}:"
        for key in [k for k in self._hosted if k.startswith(prefix)]:
            self.remove(key)

    def shutdown_all(self) -> None:
        """停止并清理全部 runner（供各接入端进程退出时调用）。"""
        for key, hosted in list(self._hosted.items()):
            try:
                hosted.runner.stop()
                hosted.runner.cleanup()
            except Exception as e:
                logger.warning(f"[SessionRegistry] 清理 master runner {key} 失败: {e}")
        self._hosted.clear()
        self._access.clear()
        self._attachment_index.clear()

    # ---------- 运行权认领 ----------

    def claim(self, key: str) -> bool:
        """原子认领会话执行权（关闭 is_running 检查到 run 启动之间的 TOCTOU 窗口）。"""
        with self._claims_lock:
            if key in self._claims:
                return False
            runner = self.get(key)
            if runner is not None and runner.is_running():
                return False
            self._claims.add(key)
            return True

    def release(self, key: str) -> None:
        with self._claims_lock:
            self._claims.discard(key)

    def is_idle(self, key: str) -> bool:
        with self._claims_lock:
            if key in self._claims:
                return False
            runner = self.get(key)
            if runner is not None and runner.is_running():
                return False
            return True

    # ---------- Attachment（多接口附着；D 阶段接入路由） ----------

    def attach(self, key: str, interface: str, attachment_id: str) -> Attachment:
        hosted = self._hosted.get(key)
        if hosted is None:
            raise KeyError(f"Session not hosted: {key}")
        att = Attachment(attachment_id=attachment_id, interface=interface, session_key=key)
        hosted.attachments[attachment_id] = att
        self._attachment_index[attachment_id] = key
        return att

    def detach(self, attachment_id: str) -> None:
        key = self._attachment_index.pop(attachment_id, None)
        if key is None:
            return
        hosted = self._hosted.get(key)
        if hosted is not None:
            hosted.attachments.pop(attachment_id, None)

    def attachments(self, key: str) -> list[Attachment]:
        hosted = self._hosted.get(key)
        return list(hosted.attachments.values()) if hosted else []


# 模块级单例（各接入端共享同一份 runner 池）
registry = SessionRegistry()
