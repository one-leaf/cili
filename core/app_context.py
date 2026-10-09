"""应用上下文：集中持有各全局单例的启动/关闭。

原先单例的创建/启动/停止点横跨 ``main.py``、``web_api.lifespan`` 与各模块的
懒初始化（例如 cron 在 ``main.py`` 启动、却在 ``web_api`` 关闭），加入第二个
接入端后「谁启动 cron / 谁关浏览器」没有明确归属。这里统一为
``startup()`` / ``shutdown()``，各接入端进程都调用它（幂等，可重复调用）。
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class AppContext:
    """接口无关的应用级单例生命周期。"""

    def __init__(self) -> None:
        self._started = False
        self._shutdown = False

    # ---------- 启动 ----------

    def startup(self) -> None:
        """启动接口无关的服务（幂等）。"""
        if self._started:
            return
        self._started = True

        # 浏览器服务实例（Playwright 延迟到首次操作时启动）
        try:
            from core.browser_service import get_service
            get_service()
        except Exception as e:
            logger.warning(f"[AppContext] 初始化浏览器服务失败: {e}")

        # AgentMailbox 单例
        try:
            from core.agent_mailbox import start_agent_mailbox
            start_agent_mailbox()
        except Exception as e:
            logger.warning(f"[AppContext] 启动 AgentMailbox 失败: {e}")

        # MCP provider：后台连接已配置的服务器（不阻塞启动）
        threading.Thread(target=self._connect_mcp_servers, daemon=True).start()

        # Cron 调度器
        try:
            from core.cron import start_scheduler
            start_scheduler()
        except Exception as e:
            logger.warning(f"[AppContext] 启动 cron 调度器失败: {e}")

    @staticmethod
    def _connect_mcp_servers() -> None:
        try:
            from core.config import load_config
            cfg = load_config()
            if cfg.mcp_servers:
                from core.tools.mcp import get_provider
                get_provider().ensure_connected(cfg.mcp_servers)
        except Exception as e:
            logger.warning(f"[AppContext] 连接 MCP 服务器失败: {e}")

    # ---------- 关闭 ----------

    def shutdown(self) -> None:
        """停止全部单例并清理 runner 池（幂等）。"""
        if self._shutdown:
            return
        self._shutdown = True

        for name, stop in (
            ("AgentMailbox", self._stop_mailbox),
            ("浏览器服务", self._stop_browser),
            ("MCP provider", self._stop_mcp),
            ("cron 调度器", self._stop_cron),
        ):
            try:
                stop()
            except Exception as e:
                logger.warning(f"[AppContext] 停止{name}失败: {e}")

        # runner 池（master runner 实例）
        try:
            from core.session_registry import registry
            logger.info(f"[AppContext] 正在关闭，清理 {len(registry)} 个 master runner...")
            registry.shutdown_all()
        except Exception as e:
            logger.warning(f"[AppContext] 清理 runner 池失败: {e}")
        logger.info("[AppContext] 资源清理完成")

    @staticmethod
    def _stop_mailbox() -> None:
        from core.agent_mailbox import stop_agent_mailbox
        stop_agent_mailbox()

    @staticmethod
    def _stop_browser() -> None:
        from core.browser_service import stop_browser_service
        stop_browser_service()

    @staticmethod
    def _stop_mcp() -> None:
        from core.tools.mcp import stop_mcp_provider
        stop_mcp_provider()

    @staticmethod
    def _stop_cron() -> None:
        from core.cron import stop_scheduler
        stop_scheduler()


# 模块级单例（各接入端共享）
app_context = AppContext()
