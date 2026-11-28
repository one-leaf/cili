"""会话归属：nodeid + 可选用户身份。

会话是共享工作区里的协作对象，但“继续写”和“删除”应限制在 owner。
nodeid 解决机器归属；组织/接口用户身份解决同机多用户与换机场景。
"""

from __future__ import annotations

from typing import Any

from core.node_identity import get_node_identity

OWNER_KEYS = (
    "owner_node_id",
    "owner_node_name",
    "owner_user_id",
    "owner_user_label",
)


def _org_user() -> tuple[str, str]:
    try:
        from core.org.store import load_session
        data = load_session()
        user = data.get("user") if isinstance(data, dict) else None
        if isinstance(user, dict):
            uid = str(user.get("id") or user.get("user_id") or user.get("phone") or "").strip()
            label = str(
                user.get("name") or user.get("display_name") or user.get("phone") or ""
            ).strip()
            return uid, label
    except Exception:
        pass
    return "", ""


def owner_metadata(metadata: dict | None = None) -> dict[str, str]:
    """补齐当前 owner；metadata 已显式带 owner 时保留显式值。"""
    meta = dict(metadata or {})
    node = get_node_identity()
    user_id, user_label = _org_user()
    if not user_id:
        account = str(meta.get("channel_account") or meta.get("account_id") or "").strip()
        if account:
            user_id = account
            user_label = user_label or account
    return {
        "owner_node_id": str(meta.get("owner_node_id") or node["id"]),
        "owner_node_name": str(meta.get("owner_node_name") or node["name"]),
        "owner_user_id": str(meta.get("owner_user_id") or user_id),
        "owner_user_label": str(meta.get("owner_user_label") or user_label),
    }


def owner_fields(metadata: dict | None) -> dict[str, Any]:
    meta = metadata or {}
    fields = {key: str(meta.get(key) or "") for key in OWNER_KEYS}
    fields["owner_mine"] = is_mine(meta)
    return fields


def is_mine(metadata: dict | None) -> bool | None:
    """True=自己的，False=别人的，None=未归属（旧会话）。"""
    meta = metadata or {}
    owner_node = str(meta.get("owner_node_id") or "").strip()
    owner_user = str(meta.get("owner_user_id") or "").strip()
    if not owner_node and not owner_user:
        return None
    node = get_node_identity()
    user_id, _ = _org_user()
    if owner_user and user_id and owner_user == user_id:
        return True
    if owner_node and owner_node == node["id"]:
        return True
    return False


def owner_label(metadata: dict | None) -> str:
    meta = metadata or {}
    return str(
        meta.get("owner_user_label")
        or meta.get("owner_node_name")
        or meta.get("owner_node_id")
        or ""
    )
