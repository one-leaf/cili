"""旧格式 setting.json → providers/agents 新格式的转换与迁移（幂等）。"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime

from core.fs_utils import atomic_write_json

logger = logging.getLogger(__name__)


_LEGACY_MODEL_SPEC_EXCLUDE = {"name", "interface_type", "api_key", "base_url", "temperature", "multimodal", "reasoning_effort", "max_tokens", "max_context_tokens"}


def _convert_legacy_model_spec(obj: dict) -> dict:
    """旧角色模型对象 → 新 ModelSpec 字段（multimodal/reasoning_effort 转换，temperature 丢弃）。"""
    spec = {k: v for k, v in obj.items() if k not in _LEGACY_MODEL_SPEC_EXCLUDE}
    spec["name"] = obj.get("name", "")
    spec["max_output_tokens"] = int(obj.get("max_tokens", 32768))
    spec["max_context_tokens"] = int(obj.get("max_context_tokens", 200000))
    if "multimodal" in obj:
        spec["input"] = ["text", "image"] if obj.get("multimodal") else ["text"]
    effort = obj.get("reasoning_effort", "")
    spec["reasoning"] = effort if effort else "medium"
    return spec


def _backup_global_config():
    """备份 setting.json；返回备份路径。已存在 setting.json.bak 时用带时间戳后缀，不覆盖更早的回滚副本。"""
    from core.config import GLOBAL_CONFIG_PATH

    backup = GLOBAL_CONFIG_PATH.with_suffix(".json.bak")
    if backup.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = GLOBAL_CONFIG_PATH.with_suffix(f".json.bak-{stamp}")
    shutil.copy2(GLOBAL_CONFIG_PATH, backup)
    return backup


def convert_legacy_config(global_config: dict) -> dict | None:
    """旧格式（model/worker_model/lite_model 为对象）→ 新格式 dict；非旧格式返回 None。

    纯函数，不碰文件系统。master 缺失时抛 RuntimeError。
    合成 provider 键为 ``default``（凭据不同时 ``default_2`` 起），backend 默认 litellm。
    """
    from core.config import default_base_url

    legacy_role_models = {
        role: global_config.get(top_key)
        for role, top_key in (("master", "model"), ("worker", "worker_model"), ("lite", "lite_model"))
    }
    if not any(isinstance(obj, dict) for obj in legacy_role_models.values()):
        return None

    master_obj = legacy_role_models.get("master")
    if not isinstance(master_obj, dict) or not master_obj.get("name"):
        raise RuntimeError("旧格式配置缺少 model.name（master 主模型），无法迁移；请补全后重试")

    providers_raw: dict[str, dict] = {}
    agents: dict[str, dict] = {}
    dropped_fields: list[str] = []

    for role, obj in legacy_role_models.items():
        if not isinstance(obj, dict) or not obj.get("name"):
            continue
        cred = (
            obj.get("interface_type", "anthropic"),
            obj.get("api_key", ""),
            (obj.get("base_url") or default_base_url(obj.get("interface_type", "anthropic"))).rstrip("/"),
        )
        provider_key = next(
            (key for key, raw in providers_raw.items() if raw["_cred"] == cred), None
        )
        if provider_key is None:
            provider_key = "default" if not providers_raw else f"default_{len(providers_raw) + 1}"
            providers_raw[provider_key] = {
                "_cred": cred,
                "interface_type": cred[0],
                "api_key": cred[1],
                "base_url": cred[2],
                "backend": "litellm",
                "headers": {},
                "auth_type": "auto",
                "models": {},
            }
        model_key = "main" if role == "master" else role
        while model_key in providers_raw[provider_key]["models"]:
            model_key += "_2"
        if "temperature" in obj:
            dropped_fields.append(f"{role}.temperature")
        providers_raw[provider_key]["models"][model_key] = _convert_legacy_model_spec(obj)
        agents[role] = {"model": f"{provider_key}/{model_key}"}

    providers: dict[str, dict] = {}
    for key, raw in providers_raw.items():
        raw.pop("_cred")
        providers[key] = raw

    migrated = {
        key: value for key, value in global_config.items()
        if key not in ("model", "worker_model", "lite_model")
    }
    migrated["providers"] = providers
    migrated["agents"] = agents
    if dropped_fields:
        migrated["_migrate_dropped"] = dropped_fields  # 供调用方日志，写盘前剥离
    return migrated


def migrate_legacy_config(global_config: dict) -> dict:
    """旧格式 → 新格式（备份 → 转换 → 原子写回）；新格式原样返回。幂等。

    仅在真实配置加载路径（load_config / get_config）调用；纯转换见 convert_legacy_config。
    """
    migrated = convert_legacy_config(global_config)
    if migrated is None:
        return global_config

    from core.config import GLOBAL_CONFIG_PATH

    backup_path = _backup_global_config()
    dropped_fields = migrated.pop("_migrate_dropped", [])

    try:
        atomic_write_json(GLOBAL_CONFIG_PATH, migrated)
    except OSError as e:
        raise RuntimeError(f"迁移结果写回失败（原文件未动，备份在 {backup_path}）: {e}") from e

    dropped_note = f"；丢弃字段: {', '.join(dropped_fields)}" if dropped_fields else ""
    logger.info(
        f"[Config] 旧格式配置已迁移：providers={list(migrated.get('providers', {}))}，"
        f"agents={migrated.get('agents', {})}；备份: {backup_path}{dropped_note}"
    )
    return migrated
