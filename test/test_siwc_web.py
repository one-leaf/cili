"""SIWC Web 路由的 provider 自动保存测试。"""


def _patch(monkeypatch, routes, state, save_result=True):
    import core.config as config_mod

    saved = {"n": 0}
    monkeypatch.setattr(config_mod, "load_global_config", lambda: state)
    monkeypatch.setattr(config_mod, "migrate_legacy_config", lambda raw: raw)

    def fake_save(config):
        saved["n"] += 1
        return save_result

    monkeypatch.setattr(config_mod, "save_global_config", fake_save)
    return saved


def test_upsert_creates_provider(monkeypatch):
    from core.llm.siwc import store as routes

    state = {"providers": {}}
    saved = _patch(monkeypatch, routes, state)
    config = {"backend": "chatgpt-siwc", "headers": {}, "models": {"m": {"name": "gpt-x"}}}

    assert routes.upsert_oauth_provider("gpt", "siwc:abc", config) == "written"
    entry = state["providers"]["gpt"]
    assert entry["auth_type"] == "oauth"
    assert entry["interface_type"] == "openai-responses"
    assert entry["base_url"] == "https://api.openai.com/v1"
    assert entry["api_path"] == ""
    assert entry["credential"] == "siwc:abc"
    assert entry["models"]["m"]["name"] == "gpt-x"
    assert "api_key" not in entry
    assert saved["n"] == 1


def test_upsert_updates_existing_non_oauth(monkeypatch):
    from core.llm.siwc import store as routes

    state = {"providers": {"p": {"auth_type": "auto", "api_key": "sk-x", "models": {"old": {}}}}}
    _patch(monkeypatch, routes, state)
    config = {"models": {"new": {}}}

    assert routes.upsert_oauth_provider("p", "siwc:def", config) == "written"
    entry = state["providers"]["p"]
    assert entry["auth_type"] == "oauth"
    assert "new" in entry["models"]
    assert "api_key" not in entry


def test_upsert_without_config_creates_default_model(monkeypatch):
    from core.llm.siwc import store as routes

    state = {"providers": {}}
    _patch(monkeypatch, routes, state)

    assert routes.upsert_oauth_provider("gpt", "siwc:x", None) == "written"
    assert state["providers"]["gpt"]["models"] == {"default": {}}


def test_upsert_save_failure(monkeypatch):
    from core.llm.siwc import store as routes

    state = {"providers": {}}
    _patch(monkeypatch, routes, state, save_result=False)

    assert routes.upsert_oauth_provider("gpt", "siwc:x", None) == "failed"


def test_complete_passes_provider_config_to_hook(monkeypatch):
    """换码成功后把登录请求里的 provider 表单快照传给写回回调。"""
    import time

    import core.llm.siwc.login as login
    from core.llm.siwc import auth

    received = {}

    def hook(provider_key, credential, provider_config):
        received.update(provider_key=provider_key, credential=credential, config=provider_config)
        return "written"

    monkeypatch.setattr(login, "_ON_AUTHORIZED", hook)

    request = auth.create_authorization_request(
        redirect_uri="http://127.0.0.1:1/auth/callback",
        ext_agent_host_id="urn:uuid:x",
    )
    attempt = login.LoginAttempt(
        attempt_id="test-attempt",
        provider_key="gpt",
        credential="",
        auth_url=request.url,
        redirect_uri=request.redirect_uri,
        request=request,
        provider_config={"backend": "chatgpt-siwc", "models": {"m": {}}},
        expires_at=time.time() + 60,
    )
    login._ATTEMPTS[attempt.attempt_id] = attempt
    monkeypatch.setattr(
        login.auth,
        "parse_callback_url",
        lambda *a, **k: {"code": "c", "client_id": "oaiapp_abc", "state": request.state},
    )
    monkeypatch.setattr(
        login.auth,
        "exchange_code",
        lambda **k: auth.Credentials(
            client_id="oaiapp_abc", sub="sub", access_token="a", refresh_token="r"
        ),
    )
    monkeypatch.setattr(login.manager, "save_login_result", lambda creds: "siwc:abc")

    try:
        result = login._complete(
            attempt.attempt_id,
            f"http://127.0.0.1:1/auth/callback?code=c&state={request.state}",
            from_callback=True,
        )
    finally:
        login._ATTEMPTS.pop(attempt.attempt_id, None)

    assert result["status"] == "authorized"
    assert result["saved"] is True
    assert received == {
        "provider_key": "gpt",
        "credential": "siwc:abc",
        "config": {"backend": "chatgpt-siwc", "models": {"m": {}}},
    }
