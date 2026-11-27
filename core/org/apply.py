"""把组织下发的配置物化到 setting.json，并支持退出恢复。"""

from __future__ import annotations

import logging
import re
from typing import Any

from core.config import CONFIG_RELOAD_LOCK, load_global_config, save_global_config
from core.config_migrations import migrate_legacy_config
from core.session_registry import registry
from core.time_utils import now_str

logger = logging.getLogger(__name__)

ORG_PROVIDER_PREFIX = "org-"
META_MODEL_KEYS = {"mode", "provider_key", "label", "roles"}


class OrgApplyError(ValueError):
    pass


def safe_provider_key(value: str) -> str:
    raw = str(value or "").strip()
    if re.match(r"^[A-Za-z0-9_-]{1,64}$", raw):
        return raw
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")[:64]
    return cleaned or "org-default"


def _is_org_ref(value: str) -> bool:
    return value.startswith(ORG_PROVIDER_PREFIX) and "/" in value


def provider_key_for(org_id: str, model: dict | None = None) -> str:
    # 以服务端下发的 provider_key 为准（cili 不再改写命名）
    configured = str((model or {}).get("provider_key") or "").strip()
    if configured:
        return configured.replace("/", "-")
    return safe_provider_key(ORG_PROVIDER_PREFIX + str(org_id or "default"))


def _provider_payload(model: dict) -> dict:
    if not isinstance(model, dict):
        raise OrgApplyError("组织配置缺少 model")
    models = model.get("models")
    if not isinstance(models, dict) or not models:
        raise OrgApplyError("组织 model 至少需要一个 models 条目")
    provider = {
        key: value for key, value in model.items() if key not in META_MODEL_KEYS
    }
    provider.setdefault("protocol", "openai-compatible")
    provider["models"] = models
    return provider


def apply_config(
    payload: dict,
    *,
    bind_roles: list[str] | None = None,
    existing_state: dict | None = None,
) -> dict:
    """写入 provider；角色绑定仅在显式传 bind_roles 时执行（服务端默认不绑定）。"""
    model = payload.get("model") if isinstance(payload, dict) else None
    if not isinstance(model, dict):
        raise OrgApplyError("组织服务端未下发 model")
    org = payload.get("org") or {}
    user = payload.get("user") or {}
    provider_key = provider_key_for(str(org.get("id") or "default"), model)
    provider = _provider_payload(model)
    models = provider["models"]

    role_map = model.get("roles")
    if not isinstance(role_map, dict):
        role_map = {}
    if bind_roles is None:
        selected = [
            r for r in role_map if r in {"master", "worker", "lite", "workflow"}
        ]
    else:
        selected = [r for r in bind_roles if r in role_map]
    # 不再强制绑定角色：selected 为空时只同步 provider，用户可在模型列表自选。
    config = migrate_legacy_config(load_global_config())
    agents = config.setdefault("agents", {})
    providers = config.setdefault("providers", {})

    state = dict(existing_state or {})
    roles_before = state.get("roles_before")
    if not isinstance(roles_before, dict):
        roles_before = {}
    else:
        roles_before = dict(roles_before)
    previous_applied = state.get("roles_applied")
    if not isinstance(previous_applied, dict):
        previous_applied = {}
    old_key = str(state.get("provider_key") or "")

    # state 丢失时从 setting.json 反推组织托管痕迹，避免把用户手选模型
    # 当成首次加入而覆盖掉。
    if not previous_applied and not old_key:
        inferred: dict[str, str] = {}
        for role, agent in agents.items():
            value = str((agent or {}).get("model") or "")
            if _is_org_ref(value):
                inferred[role] = value
        if inferred:
            previous_applied = inferred
            old_key = next(iter(inferred.values())).split("/", 1)[0]
        elif provider_key in providers:
            old_key = provider_key

    # 之前管理、本次未选中的角色：按快照恢复并停止管理。
    for role in list(previous_applied):
        if role in selected:
            continue
        agent = dict(agents.get(role) or {})
        current = str(agent.get("model") or "")
        if _is_org_ref(current):
            old = str(roles_before.get(role) or "")
            if old and not _is_org_ref(old):
                agent["model"] = old
            else:
                agent.pop("model", None)
            agents[role] = agent
        roles_before.pop(role, None)

    applied: dict[str, str] = {}
    skipped: list[str] = []
    for role in selected:
        model_key = str(role_map.get(role) or "")
        if model_key not in models:
            skipped.append(role)
            continue
        ref = f"{provider_key}/{model_key}"
        agent = dict(agents.get(role) or {})
        current = str(agent.get("model") or "")
        old_applied = str(previous_applied.get(role) or "")
        first_bind = not previous_applied and not old_key
        managed = (
            first_bind
            or not current
            or current == old_applied
            or _is_org_ref(current)
        )
        if not managed:
            # 用户后来手动改成了个人模型，不覆盖、不再管理。
            skipped.append(role)
            roles_before.pop(role, None)
            continue
        if role not in roles_before or _is_org_ref(str(roles_before.get(role) or "")):
            roles_before[role] = "" if _is_org_ref(current) else current
        applied[role] = ref
        agent["model"] = ref
        agents[role] = agent

    if old_key and old_key != provider_key:
        providers.pop(old_key, None)
    providers[provider_key] = provider

    if not save_global_config(config):
        raise OrgApplyError("保存 setting.json 失败")
    _reload_cached_runners()

    state.update(
        {
            "provider_key": provider_key,
            "roles_applied": applied,
            "roles_before": roles_before,
            "model_label": str(model.get("label") or ""),
            "model_protocol": str(provider.get("protocol") or ""),
            "model_keys": sorted(models.keys()),
            "config_version": str(payload.get("version") or ""),
            "org": dict(org),
            "user": dict(user),
            "last_sync": now_str(),
            "last_error": "部分角色未绑定（用户已手动改模型或组织模型键无效）" if skipped else "",
            "session_invalid": False,
        }
    )
    if skipped:
        state["roles_skipped"] = skipped
    else:
        state.pop("roles_skipped", None)
    return state


def restore_config(state: dict) -> bool:
    """退出/失效时删除组织 provider，并按快照恢复角色。"""
    provider_key = str((state or {}).get("provider_key") or "")
    roles_before = (state or {}).get("roles_before") or {}
    if not provider_key:
        return True

    config = migrate_legacy_config(load_global_config())
    providers = config.get("providers") or {}
    agents = config.setdefault("agents", {})
    changed = providers.pop(provider_key, None) is not None

    for role, raw_agent in list(agents.items()):
        agent = dict(raw_agent or {})
        current = str(agent.get("model") or "")
        if not current.startswith(provider_key + "/"):
            continue
        old = str(roles_before.get(role) or "")
        if old and not old.startswith(provider_key + "/"):
            agent["model"] = old
        else:
            agent.pop("model", None)
        agents[role] = agent
        changed = True

    if not changed:
        return True
    if not save_global_config(config):
        logger.warning("[org] 恢复 setting.json 失败")
        return False
    _reload_cached_runners()
    return True


def _reload_cached_runners() -> None:
    """配置保存后让缓存 runner 立即使用新 provider。"""
    try:
        with CONFIG_RELOAD_LOCK:
            with registry.lock:
                cached = list(registry.items())
            for key, runner in cached:
                try:
                    runner.reload_config()
                except Exception:
                    logger.warning("[org] runner %s 重载配置失败", key, exc_info=True)
    except Exception:
        logger.warning("[org] 重载 runner 失败", exc_info=True)
