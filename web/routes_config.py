"""Global Config + MCP 管理 路由域。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.config import (
    load_global_config, save_global_config, load_config,
    ModelConfig, MCPConfig, GLOBAL_CONFIG_PATH,
)
from core.tools.mcp import get_provider

from web.deps import registry, _sessions_lock

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------- Models ----------

class ModelConfigRequest(BaseModel):
    """Request model for updating a model configuration."""
    name: str | None = None
    interface_type: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int | None = None
    max_context_tokens: int | None = None
    multimodal: bool | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None
    cache_control: str | None = None


class UpdateConfigRequest(BaseModel):
    """Request model for updating global configuration."""
    model: ModelConfigRequest | None = None          # master 角色模型
    worker_model: ModelConfigRequest | None = None   # worker 角色模型 (optional, inherits master)
    lite_model: ModelConfigRequest | None = None     # lite 角色模型 (optional, inherits master)
    system: dict | None = None                       # System parameters (pip_mirror, etc.)
    mcp_servers: dict | None = None                  # MCP 服务器配置 {name: {...}}
    qq: dict | None = None                           # QQ 接入端配置


class McpTestRequest(BaseModel):
    name: str = ""
    config: dict | None = None


class TestConfigRequest(BaseModel):
    config: ModelConfigRequest | None = None


class QQTestRequest(BaseModel):
    """测试 QQ 凭据（未提供 secret 时用已保存的）。"""
    app_id: str | None = None
    secret: str | None = None
    use_sandbox: bool | None = None


# ---------- 掩码 helpers ----------

def _mask_secret(v: str) -> str:
    """掩码密钥：保留首尾 4 位，中间省略；过短则整体打码。"""
    return v[:4] + "..." + v[-4:] if len(v) > 8 else "***"


def _mask_single_model(model: dict) -> dict:
    """Mask API key in a single model config dict."""
    m = model.copy()
    api_key = m.get("api_key", "")
    if api_key:
        m["api_key_masked"] = _mask_secret(api_key)
    m.pop("api_key", None)
    return m


def _mask_mcp_server(cfg: dict) -> dict:
    """掩码单个 MCP server 配置中的 headers 值（保留 key 名）。"""
    c = dict(cfg)
    headers = c.get("headers") or {}
    if headers:
        c["headers"] = {
            k: _mask_secret(v)
            for k, v in headers.items() if v
        }
    return c


def _mask_api_key(config: dict) -> dict:
    """Mask API keys in config for safe display."""
    result = config.copy()
    for key in ("model", "worker_model", "lite_model"):
        if key in result and isinstance(result[key], dict):
            result[key] = _mask_single_model(result[key])
    # Mask MinerU API key in system config
    if "system" in result and isinstance(result["system"], dict):
        sys_copy = result["system"].copy()
        mineru_key = sys_copy.get("mineru_api_key", "")
        if mineru_key:
            sys_copy["mineru_api_key_masked"] = _mask_secret(mineru_key)
        sys_copy.pop("mineru_api_key", None)
        result["system"] = sys_copy
    # Mask MCP headers (可能含 Authorization/Bearer 密钥)
    if "mcp_servers" in result and isinstance(result["mcp_servers"], dict):
        result["mcp_servers"] = {
            name: _mask_mcp_server(cfg) for name, cfg in result["mcp_servers"].items()
        }
    # Mask QQ secret
    if "qq" in result and isinstance(result["qq"], dict):
        qq_copy = result["qq"].copy()
        secret = qq_copy.get("secret", "")
        if secret:
            qq_copy["secret_masked"] = _mask_secret(secret)
        qq_copy.pop("secret", None)
        result["qq"] = qq_copy
    return result


def _update_model_config(existing: dict, update: ModelConfigRequest) -> dict:
    """Update a model config dict with values from request."""
    result = existing.copy() if existing else {}
    for key, value in update.model_dump(exclude_none=True).items():
        if key == "base_url" and value:
            result[key] = value.rstrip("/")
        else:
            result[key] = value
    return result


def _apply_qq_enabled(enabled: bool) -> None:
    """按开关启停 QQ 接入端（阻塞操作，调用方需丢线程池）。"""
    try:
        from interfaces.qq import start_qq_interface, stop_qq_interface

        if enabled:
            started = start_qq_interface()
            if not started:
                logger.warning("[Config] QQ 已启用但未启动（凭据缺失或 SDK 未安装）")
        else:
            stop_qq_interface()
    except Exception as e:  # noqa: BLE001 — 启停失败不该让保存请求失败
        logger.warning(f"[Config] 切换 QQ 接入端失败: {e}")


# ---------- Config ----------

@router.get("/api/config")
async def get_config():
    """Get global model configuration."""
    config = load_global_config()
    # Mask API keys for security
    masked_config = _mask_api_key(config)
    return {
        "config": masked_config,
        "config_path": str(GLOBAL_CONFIG_PATH),
    }


@router.put("/api/config")
async def update_config(request: UpdateConfigRequest):
    """Update global model configuration."""
    config = load_global_config()

    # Update master model config (multi-turn)
    if request.model is not None:
        existing_model = config.get("model", {})
        config["model"] = _update_model_config(existing_model, request.model)

    # Update worker/lite model config (optional, inherits master by default)
    # name 为空 → 移除角色模型配置，回退继承 master model
    for role_key in ("worker_model", "lite_model"):
        role_req = getattr(request, role_key, None)
        if role_req is None:
            continue
        if not role_req.name:
            config.pop(role_key, None)
        else:
            existing_role = config.get(role_key, {})
            config[role_key] = _update_model_config(existing_role, role_req)

    # Update system config
    if request.system is not None:
        existing_system = config.get("system", {})
        existing_system.update(request.system)
        config["system"] = existing_system

    # Update MCP servers config
    if request.mcp_servers is not None:
        if request.mcp_servers:
            # _preserve_headers 标记：继承已保存 config 的原 headers（前端不覆盖密钥）
            current_mcp = config.get("mcp_servers", {})
            if not isinstance(current_mcp, dict):
                current_mcp = {}
            new_servers = {}
            for name, server in request.mcp_servers.items():
                server = dict(server)
                if server.pop("_preserve_headers", False) and name in current_mcp:
                    server["headers"] = current_mcp[name].get("headers", {})
                new_servers[name] = server
            config["mcp_servers"] = new_servers
        else:
            config.pop("mcp_servers", None)

    # Update QQ 接入端配置
    qq_toggled = False
    qq_enabled = False
    if request.qq is not None:
        existing_qq = config.get("qq", {})
        if not isinstance(existing_qq, dict):
            existing_qq = {}
        new_qq = dict(existing_qq)
        incoming = dict(request.qq)
        # secret 留空 = 不修改（前端只回显掩码，不回传明文）
        if not incoming.get("secret"):
            incoming.pop("secret", None)
        new_qq.update(incoming)
        config["qq"] = new_qq
        qq_enabled = bool(new_qq.get("enabled"))
        qq_toggled = qq_enabled != bool(existing_qq.get("enabled"))

    if not save_global_config(config):
        raise HTTPException(status_code=500, detail="Failed to save config")

    # 开关变化 → 就地启停 QQ 接入端（不必重启 Cili）。
    # start/stop 会等宿主线程就绪/退出（数秒），属阻塞操作 → 丢线程池。
    if qq_toggled:
        await asyncio.to_thread(_apply_qq_enabled, qq_enabled)

    # 重连 MCP 服务器（签名变化才真正重连）
    try:
        new_cfg = load_config()
        get_provider().reload(new_cfg.mcp_servers)
    except Exception as e:
        logger.warning(f"[Config] 重连 MCP 服务器失败: {e}")

    # 通知所有缓存的 master runner 重新加载配置（新的 API key / model 等）。
    # reload_config 会重建工具/发起网络探测，属阻塞操作：只在锁内取快照，
    # 调用放到锁外，避免卡住事件循环
    async with _sessions_lock:
        cached = list(registry.items())
    for key, runner in cached:
        runner.reload_config()
        logger.info(f"[Config] 已通知 master runner {key} 重新加载配置")

    return {"success": True, "config_path": str(GLOBAL_CONFIG_PATH)}


@router.get("/api/mcp/servers")
async def get_mcp_servers():
    """Get MCP servers config + 实时连接状态（headers 掩码显示）。"""
    cfg = load_config()
    provider = get_provider()
    status = provider.status()
    servers = {}
    for name, mcfg in cfg.mcp_servers.items():
        st = status.get(name, {})
        servers[name] = {
            "name": name,
            "url": mcfg.url,
            "headers_masked": _mask_mcp_server({"headers": mcfg.headers}).get("headers", {}),
            "tool_timeout": mcfg.tool_timeout,
            "enabled_tools": mcfg.enabled_tools,
            "status": st.get("status", "offline"),
            "tool_count": st.get("tool_count", 0),
            "tools": provider.server_tools(name),
        }
    return {"servers": servers}


@router.post("/api/mcp/reload")
async def reload_mcp():
    """强制重连所有 MCP 服务器，并刷新缓存的 master runner（deferred 工具重建）。"""
    cfg = load_config()
    provider = get_provider()
    provider.reload(cfg.mcp_servers, force=True)
    async with _sessions_lock:
        for key, runner in registry.items():
            runner.reload_config()
    return {"success": True, "servers": provider.status()}


@router.post("/api/mcp/test")
async def test_mcp_server(request: McpTestRequest | None = None):
    """测试单个 MCP 服务器连接并枚举工具（不保存配置，测完即断开）。

    优先用请求体 config（表单新增场景，含真实 headers）；否则按 name 用已保存配置。
    """
    if request is None:
        request = McpTestRequest()
    mcfg = None
    if request.config:
        try:
            mcfg = MCPConfig.from_dict(request.config)
        except Exception as e:
            return {"status": "failed", "error": f"配置解析失败: {e}"}
    elif request.name:
        mcfg = load_config().mcp_servers.get(request.name)
        if mcfg is None:
            return {"status": "failed", "error": "未找到已保存的服务器配置（请先在表单中测试）"}
    else:
        return {"status": "failed", "error": "未提供服务器配置或名称"}
    try:
        return get_provider().test_connect(mcfg)
    except Exception as e:
        return {"status": "failed", "error": str(e)}


@router.post("/api/config/test")
async def test_config(request: TestConfigRequest | None = None):
    """Test model configuration by connecting to the API.

    如果 request.config 有值，则用传入的参数测试；否则用已保存的主模型配置。
    """
    if request is None:
        request = TestConfigRequest()
    try:
        if request.config is not None:
            # 用传入的配置临时测试
            # 如果 api_key 为空，尝试从已保存的配置中获取
            api_key = request.config.api_key
            if not api_key:
                saved_config = load_config()
                if saved_config.model.api_key:
                    api_key = saved_config.model.api_key

            if not api_key:
                return {
                    "success": False,
                    "message": "API Key 不能为空",
                    "interface_type": request.config.interface_type or "anthropic",
                }

            model_cfg = ModelConfig(
                name=request.config.name or "claude-sonnet-4-6",
                interface_type=request.config.interface_type or "anthropic",
                api_key=api_key,
                base_url=(request.config.base_url or "https://api.anthropic.com").rstrip("/"),
                max_tokens=int(request.config.max_tokens or 16384),
                max_context_tokens=int(request.config.max_context_tokens or 256000),
                multimodal=bool(request.config.multimodal) if request.config.multimodal is not None else True,
                temperature=float(request.config.temperature) if request.config.temperature is not None else 0.2,
            )
        else:
            # 默认用已保存的主模型
            config = load_config()
            model_cfg = config.model

        from core.llm import create_llm_client
        client = create_llm_client(model_cfg)
        try:
            success, message = client.test_connection()
        finally:
            # 必须放 finally：test_connection 抛异常时否则会泄漏 httpx 客户端
            client.close()
        interface_type = model_cfg.interface_type

        return {
            "success": success,
            "message": message,
            "interface_type": interface_type,
        }
    except Exception as e:
        return {
            "success": False,
            "message": str(e),
            "interface_type": "unknown",
        }


@router.post("/api/config/test-qq")
async def test_qq_connection(request: QQTestRequest | None = None):
    """测试 QQ 凭据：能否拿到 access token 与网关地址。

    未提供 ``secret`` 时用已保存的（前端只回显掩码，不回传明文）。
    不触碰正在运行的接口——用独立临时客户端，测完即关。
    """
    if request is None:
        request = QQTestRequest()

    app_id = (request.app_id or "").strip()
    secret = (request.secret or "").strip()
    use_sandbox = request.use_sandbox

    saved = load_global_config().get("qq") or {}
    if not app_id:
        app_id = str(saved.get("app_id") or "")
    if not secret:
        secret = str(saved.get("secret") or "")
    if use_sandbox is None:
        use_sandbox = bool(saved.get("use_sandbox", True))

    try:
        from interfaces.qq.gateway import test_connection

        ok, message = await test_connection(app_id, secret, bool(use_sandbox))
        return {"success": ok, "message": message}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Config] 测试 QQ 连接失败: {e}")
        return {"success": False, "message": str(e)[:200]}
