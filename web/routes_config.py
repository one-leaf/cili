"""Global Config + MCP 管理 路由域。"""

from __future__ import annotations

import asyncio
import logging
import secrets

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.config import (
    GLOBAL_CONFIG_PATH,
    MCPConfig,
    load_config,
    load_global_config,
    migrate_legacy_config,
    save_global_config,
)
from core.stream_executor import get_stream_executor
from core.tools.mcp import get_provider
from web.deps import _sessions_lock, registry

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------- Models ----------

class ModelSpecRequest(BaseModel):
    """单个模型条目（provider.models 表的值）。"""
    name: str | None = None
    max_output_tokens: int | None = None
    max_context_tokens: int | None = None
    model_type: str | None = None
    input: list[str] | None = None
    reasoning: str | None = None
    cache_control: str | None = None


class ProviderRequest(BaseModel):
    """接入层配置（providers 表的值）。"""
    interface_type: str | None = None
    api_key: str | None = None
    auth_type: str | None = None
    backend: str | None = None
    base_url: str | None = None
    headers: dict[str, str] | None = None
    models: dict[str, ModelSpecRequest] | None = None


class UpdateConfigRequest(BaseModel):
    """Request model for updating global configuration."""
    providers: dict[str, ProviderRequest] | None = None  # 接入层 + 模型表（前端发全量）
    agents: dict[str, dict] | None = None                # 角色引用 {role: {model: "provider/模型键"}}
    system: dict | None = None                           # System parameters (pip_mirror, etc.)
    mcp_servers: dict | None = None                      # MCP 服务器配置 {name: {...}}
    qq: dict | None = None                               # QQ 接入端配置
    wechat: dict | None = None                           # 微信接入端配置（凭证走扫码，不在这里）


class McpTestRequest(BaseModel):
    name: str = ""
    config: dict | None = None


class TestConfigRequest(BaseModel):
    """连接测试：provider 接入层字段 + model 条目字段（测具体 provider+model 组合）。"""
    interface_type: str | None = None
    api_key: str | None = None
    auth_type: str | None = None
    base_url: str | None = None
    headers: dict[str, str] | None = None
    model_name: str | None = None  # 发给 API 的模型 ID
    provider: str | None = None    # provider 名：api_key 留空时按它取已保存的 key


class DetectModelsRequest(BaseModel):
    """模型列表自动检测：按 interface_type 调标准模型列表端点。"""
    interface_type: str | None = None
    api_key: str | None = None
    auth_type: str | None = None
    base_url: str | None = None
    headers: dict[str, str] | None = None
    provider: str | None = None    # provider 名：api_key 留空时按它取已保存的 key


class QQTestRequest(BaseModel):
    """测试 QQ 凭据（未提供 secret 时用已保存的）。"""
    account_id: str | None = None
    app_id: str | None = None
    secret: str | None = None


# ---------- 掩码 helpers ----------

def _mask_secret(v: str) -> str:
    """掩码密钥：保留首尾 4 位，中间省略；过短则整体打码。"""
    return v[:4] + "..." + v[-4:] if len(v) > 8 else "***"


def _is_masked(v: str | None) -> bool:
    """判断前端回传的 api_key 是否为掩码样式（保留明文时用已存值）。"""
    return bool(v) and "..." in v


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


def _normalize_model_keys(config: dict) -> dict:
    """规范化 providers.models 条目的键（max_tokens → max_output_tokens 的视图转换）。

    旧迁移产物文件里可能还存着 max_tokens 旧键；运行时 ModelSpec.from_dict 有兼容
    回退，这里让 API 视图统一输出新键（前端保存后即固化为新键）。
    """
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return config
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        models = provider.get("models")
        if not isinstance(models, dict):
            continue
        for spec in models.values():
            if isinstance(spec, dict) and "max_tokens" in spec and "max_output_tokens" not in spec:
                spec["max_output_tokens"] = spec.pop("max_tokens")
    return config


def _mask_api_key(config: dict) -> dict:
    """Mask API keys in config for safe display."""
    result = config.copy()
    # providers 每项的 api_key 掩码（api_key_masked 附加以便前端回显）
    if "providers" in result and isinstance(result["providers"], dict):
        masked_providers = {}
        for name, provider in result["providers"].items():
            if isinstance(provider, dict):
                p = provider.copy()
                api_key = p.get("api_key", "")
                if api_key:
                    p["api_key_masked"] = _mask_secret(api_key)
                p.pop("api_key", None)
                masked_providers[name] = p
            else:
                masked_providers[name] = provider
        result["providers"] = masked_providers
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
    # Mask QQ secret（按账号）
    if "qq" in result and isinstance(result["qq"], dict):
        qq_copy = result["qq"].copy()
        accounts = qq_copy.get("accounts")
        if isinstance(accounts, list):
            masked_accounts = []
            for acct in accounts:
                if isinstance(acct, dict):
                    a = acct.copy()
                    secret = a.get("secret", "")
                    if secret:
                        a["secret_masked"] = _mask_secret(secret)
                    a.pop("secret", None)
                    masked_accounts.append(a)
                else:
                    masked_accounts.append(acct)
            qq_copy["accounts"] = masked_accounts
        result["qq"] = qq_copy
    return result


def _apply_account_enabled_changes(
    interface: str, before: dict[str, bool], after: dict[str, bool]
) -> None:
    """按账号 diff 启停接入端（设计 §7.1）：只动开关变化的账号。

    未变的账号不被重启（重启意味着断连与可能的会话游标丢失）；被删除的
    账号一并停止。阻塞操作，调用方需丢线程池。
    """
    try:
        if interface == "qq":
            from interfaces.qq import start_qq_interface, stop_qq_interface

            start_fn, stop_fn = start_qq_interface, stop_qq_interface
        else:
            from interfaces.wechat import start_wechat_interface, stop_wechat_interface

            start_fn, stop_fn = start_wechat_interface, stop_wechat_interface

        for acct_id, enabled in after.items():
            if before.get(acct_id) == enabled:
                continue          # 开关没变 → 不动它
            if enabled:
                if not start_fn(acct_id):
                    logger.warning(
                        f"[Config] 账号 {acct_id} 已启用但未启动"
                        f"（凭据缺失/未登录/SDK 未安装）"
                    )
            else:
                stop_fn(acct_id)
        # 被删除的账号：不再出现在列表里，停掉它的宿主
        for acct_id in before:
            if acct_id not in after:
                stop_fn(acct_id)
    except Exception as e:  # noqa: BLE001 — 启停失败不该让保存请求失败
        logger.warning(f"[Config] 切换 {interface} 接入端失败: {e}")


# ---------- Config ----------

@router.get("/api/config")
async def get_config():
    """Get global model configuration."""
    config = migrate_legacy_config(load_global_config())
    # 规范化模型键 + 掩码
    config = _normalize_model_keys(config)
    masked_config = _mask_api_key(config)
    return {
        "config": masked_config,
        "config_path": str(GLOBAL_CONFIG_PATH),
    }


@router.put("/api/config")
async def update_config(request: UpdateConfigRequest):
    """Update global model configuration."""
    config = migrate_legacy_config(load_global_config())

    # Update providers（全量替换；api_key 留空或掩码样式 → 继承已保存值）
    if request.providers is not None:
        current_providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
        new_providers: dict = {}
        for name, provider_req in request.providers.items():
            provider = provider_req.model_dump(exclude_none=True)
            incoming_key = provider.get("api_key", "")
            saved_key = (current_providers.get(name) or {}).get("api_key", "")
            if not incoming_key or _is_masked(incoming_key):
                provider["api_key"] = saved_key
            if provider.get("base_url"):
                provider["base_url"] = provider["base_url"].rstrip("/")
            new_providers[name] = provider
        config["providers"] = new_providers

    # Update agents（全量替换；master.model 必填校验交给 load_config 的 fail-fast）
    if request.agents is not None:
        config["agents"] = {name: dict(cfg) for name, cfg in request.agents.items()}

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

    # Update QQ 接入端配置（账号列表，全量替换；secret 留空/掩码 = 保留原值）
    qq_enable_before: dict[str, bool] = {}
    qq_enable_after: dict[str, bool] = {}
    if request.qq is not None:
        incoming = dict(request.qq)
        accounts_in = incoming.get("accounts")
        if not isinstance(accounts_in, list):
            accounts_in = []
        existing_qq = config.get("qq") if isinstance(config.get("qq"), dict) else {}
        old_accounts = existing_qq.get("accounts") if isinstance(existing_qq.get("accounts"), list) else []
        old_by_id = {
            str(a.get("id") or ""): a for a in old_accounts if isinstance(a, dict)
        }
        new_accounts: list[dict] = []
        for item in accounts_in:
            if not isinstance(item, dict):
                continue
            from interfaces.qq.config import QQConfig

            cfg_obj = QQConfig.from_dict(item)      # 白名单归一化：未知键丢弃、类型兜底
            acct_id = cfg_obj.id.strip()
            if not acct_id:
                acct_id = secrets.token_hex(4)
            cfg_obj.id = acct_id
            # secret 留空或掩码 → 继承已保存值（前端只回显掩码，不回传明文）
            if not cfg_obj.secret or _is_masked(cfg_obj.secret):
                cfg_obj.secret = str((old_by_id.get(acct_id) or {}).get("secret") or "")
            new_accounts.append(cfg_obj.to_dict())
        config["qq"] = {"accounts": new_accounts}
        qq_enable_before = {
            str(a.get("id") or ""): bool(a.get("enabled"))
            for a in old_accounts if isinstance(a, dict)
        }
        qq_enable_after = {
            str(a.get("id") or ""): bool(a.get("enabled")) for a in new_accounts
        }

    # Update 微信接入端配置（账号列表，全量替换；凭证走扫码、不在配置里）
    wechat_enable_before: dict[str, bool] = {}
    wechat_enable_after: dict[str, bool] = {}
    if request.wechat is not None:
        incoming = dict(request.wechat)
        accounts_in = incoming.get("accounts")
        if not isinstance(accounts_in, list):
            accounts_in = []
        existing_wechat = config.get("wechat") if isinstance(config.get("wechat"), dict) else {}
        old_accounts = existing_wechat.get("accounts") if isinstance(existing_wechat.get("accounts"), list) else []
        new_accounts: list[dict] = []
        for item in accounts_in:
            if not isinstance(item, dict):
                continue
            from interfaces.wechat.config import WechatConfig

            cfg_obj = WechatConfig.from_dict(item)  # 白名单归一化：未知键丢弃、类型兜底
            if not cfg_obj.id:
                cfg_obj.id = secrets.token_hex(4)
            new_accounts.append(cfg_obj.to_dict())
        config["wechat"] = {"accounts": new_accounts}
        wechat_enable_before = {
            str(a.get("id") or ""): bool(a.get("enabled"))
            for a in old_accounts if isinstance(a, dict)
        }
        wechat_enable_after = {
            str(a.get("id") or ""): bool(a.get("enabled")) for a in new_accounts
        }

    if not save_global_config(config):
        raise HTTPException(status_code=500, detail="Failed to save config")

    # 开关变化的账号 → 就地启停（不必重启 Cili，未变的账号不断连）。
    # start/stop 会等宿主线程就绪/退出（数秒），属阻塞操作 → 丢线程池。
    if qq_enable_before != qq_enable_after:
        await asyncio.to_thread(
            _apply_account_enabled_changes, "qq", qq_enable_before, qq_enable_after
        )
    if wechat_enable_before != wechat_enable_after:
        await asyncio.to_thread(
            _apply_account_enabled_changes, "wechat", wechat_enable_before, wechat_enable_after
        )

    # 重连 MCP 服务器（签名变化才真正重连）
    try:
        new_cfg = load_config()
        get_provider().reload(new_cfg.mcp_servers)
    except Exception as e:
        logger.warning(f"[Config] 重连 MCP 服务器失败: {e}")

    # 通知所有缓存的 master runner 重新加载配置（新的 API key / model 等）。
    # reload_config 会重建工具/发起网络探测，属阻塞操作：只在锁内取快照，
    # 调用放到锁外，避免卡住事件循环
    with _sessions_lock:
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
    with _sessions_lock:
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


def _saved_provider_api_key(provider: str) -> str:
    """已保存配置里某 provider 的 api_key（未找到返回 ""）。"""
    if not provider:
        return ""
    providers = migrate_legacy_config(load_global_config()).get("providers") or {}
    entry = providers.get(provider) or {}
    return str(entry.get("api_key") or "") if isinstance(entry, dict) else ""


def _resolve_api_key(request_api_key: str | None, provider: str | None) -> str:
    """解析本次请求该用的 api_key（测试连接与模型列表检测共用）。

    显式传入的优先（掩码视为未传）；否则取**请求所指 provider** 已保存的 key；
    只有请求没指定 provider（空请求＝测主模型）才回退到 master 角色的 key——
    否则会拿别的 provider 的 key 去请求，表现为莫名其妙的 401。
    """
    key = request_api_key or ""
    if key and not _is_masked(key):
        return key
    key = _saved_provider_api_key(provider or "")
    if not key and not provider:
        key = load_config().model_for_role("master").api_key
    return key


def _resolve_test_provider(request: TestConfigRequest) -> tuple[str, str, str, dict[str, str]]:
    """解析连接测试的接入层参数。"""
    api_key = _resolve_api_key(request.api_key, request.provider)
    interface_type = request.interface_type or "anthropic"
    base_url = (request.base_url or "").rstrip("/")
    return interface_type, api_key, base_url, dict(request.headers or {})


@router.post("/api/config/test")
async def test_config(request: TestConfigRequest | None = None):
    """Test model configuration by connecting to the API（测具体 provider+model 组合）。

    参数为空时用已保存的主模型配置。
    """
    if request is None:
        request = TestConfigRequest()
    try:
        interface_type, api_key, base_url, extra_headers = _resolve_test_provider(request)
        if not api_key:
            return {
                "success": False,
                "message": "API Key 不能为空",
                "interface_type": interface_type,
            }

        from core.config import ModelConfig
        model_cfg = ModelConfig(
            name=request.model_name or "claude-sonnet-4-6",
            interface_type=interface_type,
            api_key=api_key,
            base_url=base_url or "https://api.anthropic.com",
            auth_type=request.auth_type or "auto",
            headers=extra_headers,
        )

        from core.llm import create_llm_client
        client = create_llm_client(model_cfg)
        try:
            # test_connection 是同步阻塞的 LLM 请求（重试总上限 300s，见 core/llm/client.py）：
            # 直接在 async 路由里调用会冻住整个事件循环，所有页面一起挂。走专用池。
            success, message = await asyncio.get_running_loop().run_in_executor(
                get_stream_executor(), client.test_connection
            )
        finally:
            # 必须放 finally：test_connection 抛异常时否则会泄漏 httpx 客户端
            client.close()

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


def _session_headers_for(interface_type: str, auth_type: str, api_key: str, headers: dict[str, str]) -> dict[str, str]:
    """构造模型列表检测的请求头（与正式请求同源：auth_type 分派 + 显式 headers）。"""
    out: dict[str, str] = {"content-type": "application/json"}
    if auth_type == "none":
        pass
    elif auth_type == "bearer":
        out["Authorization"] = f"Bearer {api_key}"
    elif auth_type == "x-api-key":
        out["x-api-key"] = api_key
    elif interface_type == "anthropic":
        out["x-api-key"] = api_key
        out["anthropic-version"] = "2023-06-01"
    else:
        out["Authorization"] = f"Bearer {api_key}"
    out.update(headers or {})
    return out


@router.post("/api/config/detect-models")
async def detect_models(request: DetectModelsRequest | None = None):
    """模型列表自动检测（一次性导入助手）：拉取网关模型 ID 列表，前端勾选后合入 models 表。

    anthropic → GET {base_url}/v1/models（分页跟随）；openai → GET {base_url}/models。
    """
    if request is None:
        request = DetectModelsRequest()
    interface_type = (request.interface_type or "anthropic").lower()
    api_key = _resolve_api_key(request.api_key, request.provider)
    base_url = (request.base_url or "").rstrip("/")
    if not base_url:
        base_url = "https://api.anthropic.com" if interface_type == "anthropic" else "https://api.openai.com/v1"

    if interface_type == "anthropic":
        url = f"{base_url}/v1/models"
    else:
        url = f"{base_url}/models"

    headers = _session_headers_for(interface_type, request.auth_type or "auto", api_key, request.headers or {})
    model_ids: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if interface_type == "anthropic":
                params: dict[str, str] = {"limit": "100"}
                while True:
                    resp = await client.get(url, headers=headers, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    model_ids.extend(item.get("id", "") for item in data.get("data", []) if item.get("id"))
                    if not data.get("has_more") or not data.get("last_id"):
                        break
                    params = {"limit": "100", "after_id": data["last_id"]}
            else:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                model_ids.extend(item.get("id", "") for item in data.get("data", []) if item.get("id"))
    except Exception as e:  # noqa: BLE001 — 网关不支持时前端提示改手动添加
        return {"success": False, "models": [], "error": str(e)[:300]}

    return {"success": True, "models": sorted(set(model_ids))}


@router.post("/api/config/test-qq")
async def test_qq_connection(request: QQTestRequest | None = None):
    """测试 QQ 凭据：能否拿到 access token 与网关地址。

    未提供 ``secret`` 时用已保存的（前端只回显掩码，不回传明文）。用进程当前
    域名（§4.2：域名不再按账号/按次切换），不触碰正在运行的接口——独立临时
    客户端，测完即关。
    """
    if request is None:
        request = QQTestRequest()

    app_id = (request.app_id or "").strip()
    secret = (request.secret or "").strip()

    saved_accounts = (load_global_config().get("qq") or {}).get("accounts") or []
    saved: dict = {}
    if isinstance(saved_accounts, list):
        for a in saved_accounts:
            if not isinstance(a, dict):
                continue
            if request.account_id and str(a.get("id") or "") != request.account_id:
                continue
            saved = a
            break

    if not app_id:
        app_id = str(saved.get("app_id") or "")
    if not secret:
        secret = str(saved.get("secret") or "")

    try:
        from interfaces.qq.gateway import test_connection

        ok, message = await test_connection(app_id, secret)
        return {"success": ok, "message": message}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Config] 测试 QQ 连接失败: {e}")
        return {"success": False, "message": str(e)[:200]}
