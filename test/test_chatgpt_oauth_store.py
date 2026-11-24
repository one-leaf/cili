"""SIWC 凭证文件测试（路径重定向到 tmp）。"""

import pytest

from core.llm.chatgpt_oauth import auth, store


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "CHATGPT_OAUTH_ROOT", tmp_path / "chatgpt")
    return tmp_path / "chatgpt"


def _creds() -> auth.Credentials:
    return auth.Credentials(
        client_id="oaiapp_abc",
        sub="sub-1",
        access_token="access",
        refresh_token="refresh",
        email="user@example.com",
        expires_at=auth.utcnow(),
    )


def test_host_id_stable(isolated_store):
    first = store.host_id()
    second = store.host_id()
    assert first == second
    assert first.startswith("urn:uuid:")


def test_save_load_delete(isolated_store):
    credential = store.save(_creds())
    assert credential.startswith("chatgpt-oauth:")
    loaded = store.load(credential)
    assert loaded is not None
    assert loaded.refresh_token == "refresh"
    assert loaded.email == "user@example.com"

    assert store.delete(credential) is True
    assert store.load(credential) is None


def test_account_id_deterministic(isolated_store):
    first = store.save(_creds())
    second = store.save(_creds())
    assert first == second


def test_list_accounts_hides_tokens(isolated_store):
    store.save(_creds())
    items = store.list_accounts()
    assert len(items) == 1
    assert "access_token" not in items[0]
    assert "refresh_token" not in items[0]
    assert items[0]["email"] == "user@example.com"


def test_mark_reauth_clears_tokens(isolated_store):
    credential = store.save(_creds())
    loaded = store.load(credential)
    store.mark_reauth(loaded)
    after = store.load(credential)
    assert after.reauth_required is True
    assert after.access_token == ""
    assert after.refresh_token == ""
    assert after.client_id == "oaiapp_abc"


def test_invalid_credential_rejected(isolated_store):
    assert store.load("chatgpt-oauth:../etc/passwd") is None
    assert store.load("") is None
