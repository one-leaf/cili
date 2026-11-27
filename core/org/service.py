"""组织托管服务：登录会话、配置同步、启动恢复与退出。"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

from core.org.apply import apply_config, restore_config
from core.org.workspaces import (
    apply_workspaces,
    disconnect_workspaces,
    prune_managed_workspaces,
    sync_managed_workspaces,
)
from core.org.client import (
    OrgAuthError,
    OrgClient,
    OrgError,
    OrgNetworkError,
    OrgProtocolError,
    OrgRateLimited,
)
from core.org.store import (
    clear_all,
    clear_session,
    clear_state,
    load_session,
    load_state,
    save_session,
    save_state,
)

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 6 * 3600
STARTUP_DELAY_SECONDS = 10


def normalize_server_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("组织服务器地址必须是 http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("组织服务器地址不能包含用户名或密码")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("组织服务器地址只填写协议、域名和端口，不要带路径/参数")
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost", "127.0.0.1", "::1",
    }:
        raise ValueError("组织服务器必须使用 HTTPS（本机调试除外）")
    return f"{parsed.scheme}://{parsed.netloc}"


def _is_session_invalid(exc: OrgAuthError) -> bool:
    if exc.status == 401:
        return True
    return exc.status == 403 and exc.code in {"token_invalid", "person_disabled"}


def mask_phone(phone: str) -> str:
    value = str(phone or "")
    if len(value) <= 4:
        return value
    if len(value) <= 7:
        return value[:2] + "*" * (len(value) - 4) + value[-2:]
    return value[:3] + "*" * (len(value) - 7) + value[-4:]


class OrgService:
    def __init__(self, *, client_factory=OrgClient):
        self._client_factory = client_factory
        self._lock = threading.RLock()
        self._sync_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── 启动 / 停止 ───────────────────────────────────────

    def startup(self) -> None:
        if self._thread is not None:
            if self._thread.is_alive():
                return
            self._thread = None
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._background_loop, daemon=True, name="OrgSync"
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            if not thread.is_alive():
                self._thread = None

    def _background_loop(self) -> None:
        if self._stop.wait(STARTUP_DELAY_SECONDS):
            return
        while not self._stop.is_set():
            try:
                if load_session() and not load_state().get("session_invalid"):
                    self.sync()
            except Exception:
                logger.warning("[org] 后台同步失败", exc_info=True)
            if self._stop.wait(SYNC_INTERVAL_SECONDS):
                break

    # ── 状态 ──────────────────────────────────────────────

    def status(self) -> dict:
        with self._lock:
            session = load_session()
            state = load_state()
        if not session or not session.get("server_url"):
            return {
                "joined": False,
                "session_invalid": False,
                "org": None,
                "user": None,
                "server_url": "",
                "last_sync": "",
                "last_error": "",
                "model": None,
                "roles": {},
                "config_version": "",
                "workspaces": [],
            }
        invalid = bool(state.get("session_invalid"))
        user_raw = session.get("user") or {}
        phone = str(user_raw.get("phone") or session.get("phone") or "")
        return {
            "joined": True,
            "session_invalid": invalid,
            "server_url": str(session.get("server_url") or ""),
            "org": session.get("org") or {},
            "user": {
                "id": user_raw.get("id") or "",
                "name": user_raw.get("name") or "",
                "groups": list(user_raw.get("groups") or []),
                "phone_masked": mask_phone(phone),
            },
            "provider_key": str(state.get("provider_key") or ""),
            "roles": {} if invalid else (state.get("roles_applied") or {}),
            "model": {
                "label": str(state.get("model_label") or ""),
                "protocol": str(state.get("model_protocol") or ""),
                "keys": list(state.get("model_keys") or []),
            } if state.get("provider_key") and not state.get("session_invalid") else None,
            "config_version": str(state.get("config_version") or ""),
            "last_sync": str(state.get("last_sync") or ""),
            "last_error": str(state.get("last_error") or ""),
            "workspaces": state.get("workspaces") or [],
        }

    # ── 对外操作 ──────────────────────────────────────────

    def discover(self, server_url: str) -> list[dict]:
        url = normalize_server_url(server_url)
        client = self._client_factory(url)
        try:
            return client.discover()
        finally:
            client.close()

    def send_sms(self, server_url: str, org_id: str, phone: str) -> dict:
        url = normalize_server_url(server_url)
        client = self._client_factory(url)
        try:
            return client.send_sms(org_id, phone)
        finally:
            client.close()

    def login(
        self,
        *,
        server_url: str,
        org_id: str,
        phone: str,
        code: str,
        device_name: str = "cili",
        bind_roles: list[str] | None = None,
    ) -> dict:
        with self._sync_lock:
            return self._login_locked(
                server_url=server_url,
                org_id=org_id,
                phone=phone,
                code=code,
                device_name=device_name,
                bind_roles=bind_roles,
            )

    def _login_locked(
        self,
        *,
        server_url: str,
        org_id: str,
        phone: str,
        code: str,
        device_name: str = "cili",
        bind_roles: list[str] | None = None,
    ) -> dict:
        url = normalize_server_url(server_url)
        client = self._client_factory(url)
        try:
            data = client.verify(
                org_id=org_id,
                phone=phone,
                code=code,
                device_name=device_name or "cili",
            )
        finally:
            client.close()

        token = str(data.get("token") or "")
        if not token:
            raise OrgError("组织服务端未返回设备令牌")
        org = data.get("org") or {}
        user = data.get("user") or {}
        old = load_session()
        old_state = load_state()
        old_org = str((old_state.get("org") or {}).get("id") or (old or {}).get("org_id") or "")
        switching = bool(old) and (
            str(old.get("server_url") or "") != url
            or str(old.get("org_id") or "") != str(org_id)
        )
        if switching and old.get("device_token") and old.get("server_url"):
            old_client = self._client_factory(str(old["server_url"]))
            try:
                old_client.logout(str(old["device_token"]))
            except Exception:  # noqa: BLE001 - 旧令牌吊销失败不阻断换组织
                logger.warning("[org] 吊销旧组织设备失败", exc_info=True)
            finally:
                old_client.close()
        if old_state.get("provider_key") and (switching or not old):
            if old_org:
                disconnect_workspaces(old_org)
            if not restore_config(old_state):
                raise OrgError("恢复原组织配置失败，已保留本地状态")
            clear_state()
            old_state = {}

        save_session(
            {
                "server_url": url,
                "org_id": str(org_id),
                "phone": str(phone),
                "device_token": token,
                "expires_at": str(data.get("expires_at") or ""),
                "org": dict(org),
                "user": dict(user),
                "config_version": str(data.get("config_version") or ""),
            }
        )
        state = dict(old_state)
        if bind_roles is not None:
            state["bind_roles"] = list(bind_roles)
        # 重新登录必须强制拉一次完整配置，避免沿用失效前的 ETag 得到 304。
        state["config_version"] = ""
        state["session_invalid"] = False
        save_state(state)
        self.sync(force=True, wait=True)
        return self.status()

    def sync(self, *, force: bool = False, wait: bool = False) -> dict:
        if not self._sync_lock.acquire(blocking=wait):
            return self.status()
        try:
            return self._sync_locked(force=force)
        finally:
            self._sync_lock.release()

    def _sync_locked(self, *, force: bool) -> dict:
        with self._lock:
            session = load_session()
            state = load_state()
        if not session or not session.get("device_token"):
            return self.status()
        if state.get("session_invalid") and not force:
            return self.status()

        url = str(session.get("server_url") or "")
        token = str(session.get("device_token") or "")
        if not state.get("org"):
            state["org"] = session.get("org") or {}
        client = self._client_factory(url)
        try:
            try:
                me = client.me(token)
            except OrgAuthError as exc:
                if _is_session_invalid(exc):
                    self._mark_invalid(state, "登录已失效，请重新验证手机号")
                else:
                    self._record_error(state, str(exc))
                return self.status()
            except (OrgNetworkError, OrgProtocolError) as exc:
                self._record_error(state, str(exc))
                return self.status()

            bind_roles = state.get("bind_roles")
            if not isinstance(bind_roles, list):
                bind_roles = None
            etag = str(state.get("config_version") or "")
            provider_key = str(state.get("provider_key") or "")
            if etag and provider_key:
                from core.config import load_global_config

                if provider_key not in (load_global_config().get("providers") or {}):
                    etag = ""  # 本地 provider 丢了，强制全量拉取
            if etag and not state.get("workspaces_synced"):
                etag = ""  # 旧状态没有工作区同步标记，补一次全量
            try:
                not_modified, payload = client.fetch_config(token, etag)
            except OrgAuthError as exc:
                if _is_session_invalid(exc):
                    self._mark_invalid(state, "登录已失效，请重新验证手机号")
                else:
                    self._record_error(state, str(exc))
                return self.status()
            except OrgRateLimited as exc:
                self._record_error(state, str(exc))
                return self.status()
            except (OrgNetworkError, OrgProtocolError) as exc:
                self._record_error(state, str(exc))
                return self.status()

            session["user"] = me.get("user") or session.get("user") or {}
            session["org"] = me.get("org") or session.get("org") or {}
            if me.get("expires_at"):
                session["expires_at"] = str(me["expires_at"])
            if self._stop.is_set():
                return self.status()
            if not_modified:
                state["workspaces"] = sync_managed_workspaces(
                    state.get("workspaces") or []
                )
                state["last_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
                state["last_error"] = (
                    "部分共享工作区同步失败"
                    if any(
                        item.get("status") != "ok"
                        for item in state["workspaces"]
                    )
                    else ""
                )
                state["session_invalid"] = False
                with self._lock:
                    save_state(state)
                    save_session(session)
                return self.status()
            try:
                state = apply_config(
                    payload, bind_roles=bind_roles, existing_state=state
                )
            except Exception as exc:
                self._record_error(state, f"应用组织配置失败: {exc}")
                return self.status()

            # 先落盘角色/provider 快照，避免 shutdown 恰好卡在后续工作区同步时丢失。
            with self._lock:
                save_state(state)

            try:
                state["workspaces"] = apply_workspaces(
                    payload.get("workspaces") or [],
                    org=payload.get("org") or {},
                    token=token,
                )
                state["workspaces_synced"] = True
                org_id = str((payload.get("org") or {}).get("id") or "")
                if org_id:
                    prune_managed_workspaces(
                        org_id,
                        {str(item.get("key") or "") for item in state["workspaces"]},
                    )
                if any(
                    item.get("status") == "error"
                    for item in state["workspaces"]
                ):
                    state["last_error"] = "部分共享工作区同步失败"
            except Exception as exc:
                state["last_error"] = f"共享工作区同步失败: {exc}"
                logger.warning("[org] 共享工作区同步失败", exc_info=True)

            if self._stop.is_set():
                return self.status()
            session["config_version"] = str(payload.get("version") or "")
            session["user"] = payload.get("user") or session.get("user") or {}
            session["org"] = payload.get("org") or session.get("org") or {}
            with self._lock:
                save_state(state)
                save_session(session)
            return self.status()
        finally:
            client.close()

    def logout(self) -> dict:
        with self._sync_lock:
            return self._logout_locked()

    def _logout_locked(self) -> dict:
        with self._lock:
            session = load_session()
            state = load_state()
        token = str(session.get("device_token") or "")
        url = str(session.get("server_url") or "")
        if token and url:
            client = self._client_factory(url)
            try:
                client.logout(token)
            finally:
                client.close()
        org_id = str(session.get("org_id") or "")
        if org_id:
            disconnect_workspaces(org_id)
        if not restore_config(state):
            state = dict(state)
            state["last_error"] = "恢复 setting.json 失败，请稍后重试退出"
            with self._lock:
                save_state(state)
            return self.status()
        clear_all()
        return self.status()

    # ── 内部 ──────────────────────────────────────────────

    def _record_error(self, state: dict, message: str) -> None:
        state = dict(state)
        state["last_error"] = str(message)[:500]
        with self._lock:
            save_state(state)

    def _mark_invalid(self, state: dict, message: str) -> None:
        state = dict(state)
        state["session_invalid"] = True
        state["last_error"] = message
        org_id = str((state.get("org") or {}).get("id") or "")
        if org_id:
            # 保留工作区文件与索引，只清 Git 凭证。
            disconnect_workspaces(org_id)
        if restore_config(state):
            # 配置已恢复；清掉托管引用，重新登录时按首次绑定重新选择角色。
            for key in ("provider_key", "roles_applied", "roles_before",
                        "model_label", "model_protocol", "model_keys"):
                state.pop(key, None)
        else:
            state["last_error"] = message + "；恢复 setting.json 失败"
        with self._lock:
            save_state(state)


_service: OrgService | None = None
_service_lock = threading.Lock()


def get_org_service() -> OrgService:
    global _service
    with _service_lock:
        if _service is None:
            _service = OrgService()
        return _service
