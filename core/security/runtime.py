"""权限运行时装配：store / rules / resolver / judge 的唯一构造点。

SessionRunner 初始化与配置热重载共用本工厂，保证两处口径一致
（工作区规则文件、全局规则路径、青少年保护与 lite 判定器的角色开关）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.permission_settings import get_permission_config
from core.security.permission_judge import PermissionJudge
from core.security.permission_policy import PermissionResolver
from core.security.permission_rules import PermissionRules
from core.security.permission_state import PermissionStore


@dataclass
class PermissionStoreRules:
    store: Any
    rules: Any


def build_permission_store_rules(
    config,
    *,
    workspace_uuid: str,
    cwd: str,
    permission_store=None,
    permission_rules=None,
) -> PermissionStoreRules:
    """只构造 store 与 rules（根/子代理共享实例）。"""
    from core.config import DATA_DIR, get_workspace_data_dir

    permission_cfg = get_permission_config(config)
    global_path = (
        Path(permission_cfg.global_rules_path)
        if permission_cfg.global_rules_path
        else DATA_DIR / "approval_rules.json"
    )
    store = permission_store if permission_store is not None else PermissionStore()
    if permission_rules is not None:
        rules = permission_rules
    else:
        rules = PermissionRules(
            get_workspace_data_dir(workspace_uuid) / "approval_rules.json",
            global_path=global_path,
            workspace_root=cwd,
            hot_reload=permission_cfg.rules_hot_reload,
        )
    return PermissionStoreRules(store=store, rules=rules)


@dataclass
class PermissionStack:
    store: Any
    rules: Any
    resolver: Any
    judge: Any


def build_permission_stack(
    config,
    *,
    workspace_uuid: str,
    cwd: str,
    role: str,
    has_user_channel: bool,
    session_dir: Path | None = None,
    permission_store=None,
    permission_rules=None,
) -> PermissionStack:
    """构造一套权限运行时（store/rules 可外部传入以在根/子代理间共享）。"""
    pair = build_permission_store_rules(
        config,
        workspace_uuid=workspace_uuid,
        cwd=cwd,
        permission_store=permission_store,
        permission_rules=permission_rules,
    )
    store, rules = pair.store, pair.rules
    permission_cfg = get_permission_config(config)
    resolver = PermissionResolver(
        permission_cfg, rules, store, workspace_root=cwd, cwd=cwd,
    )
    judge = PermissionJudge(
        config, rules, store,
        role=role, has_user_channel=has_user_channel,
        workspace_root=cwd, cwd=cwd, session_dir=session_dir,
    )
    return PermissionStack(store=store, rules=rules, resolver=resolver, judge=judge)