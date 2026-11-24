"""SIWC 凭据模型（与存储/刷新解耦，跨模块共享）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.llm.chatgpt_oauth import const


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Credentials:
    client_id: str
    sub: str
    access_token: str
    refresh_token: str
    id_token: str = ""
    email: str = ""
    plan_type: str = ""
    expires_at: datetime = field(default_factory=utcnow)
    earliest_refresh_at: datetime | None = None
    scopes: list[str] = field(default_factory=list)
    reauth_required: bool = False
    ext_agent_host_id: str = ""
    issuer: str = const.ISSUER
    saved_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "issuer": self.issuer,
            "client_id": self.client_id,
            "ext_agent_host_id": self.ext_agent_host_id,
            "sub": self.sub,
            "email": self.email,
            "plan_type": self.plan_type,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "expires_at": _iso(self.expires_at),
            "earliest_refresh_at": _iso(self.earliest_refresh_at),
            "scopes": list(self.scopes),
            "reauth_required": self.reauth_required,
            "saved_at": _iso(self.saved_at or utcnow()),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Credentials":
        return cls(
            client_id=str(data.get("client_id") or ""),
            sub=str(data.get("sub") or ""),
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            id_token=str(data.get("id_token") or ""),
            email=str(data.get("email") or ""),
            plan_type=str(data.get("plan_type") or ""),
            expires_at=_parse_dt(data.get("expires_at")) or utcnow(),
            earliest_refresh_at=_parse_dt(data.get("earliest_refresh_at")),
            scopes=[str(s) for s in (data.get("scopes") or []) if s],
            reauth_required=bool(data.get("reauth_required")),
            ext_agent_host_id=str(data.get("ext_agent_host_id") or ""),
            issuer=str(data.get("issuer") or const.ISSUER),
            saved_at=_parse_dt(data.get("saved_at")),
        )


def _iso(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).isoformat() if value else ""


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
